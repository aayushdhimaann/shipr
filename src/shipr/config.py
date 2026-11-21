"""Load shipr.yml: global defaults < project sections < project `defaults` < environment."""

from __future__ import annotations

import copy
import os
import posixpath
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import ConfigError
from .paths import app_dir
from .providers import provider_names
from .providers.base import is_within

__all__ = ["CONFIG_NAME", "Config", "ConfigError", "Environment", "deep_merge", "find_config",
           "load"]

CONFIG_NAME = "shipr.yml"
TOP_LEVEL_KEYS = {"version", "project", "source", "build", "files", "hooks", "defaults",
                  "environments"}
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class _Model(BaseModel):
    # hide_input_in_errors: validation errors must never echo passwords back to the terminal
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Build(_Model):
    commands: list[str] = []
    output: str = "."


class Files(_Model):
    include: list[str] = ["**"]
    exclude: list[str] = []
    protect: list[str] = []
    default_excludes: bool = True


class Hooks(_Model):
    local_before: list[str] = []
    local_after: list[str] = []
    remote_before: list[str] = []
    remote_after: list[str] = []


class Environment(BaseModel):
    """Effective settings for one environment. Extra keys hold provider settings (e.g. `sftp:`)."""

    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)

    name: str
    provider: str = "sftp"
    branch: str | None = None
    remote_path: str
    state_path: str | None = None
    confirm: bool = False
    ssh_exec: bool | Literal["auto"] = "auto"
    source: str = "."
    build: Build = Field(default_factory=Build)
    files: Files = Field(default_factory=Files)
    hooks: Hooks = Field(default_factory=Hooks)

    @field_validator("remote_path", "state_path")
    @classmethod
    def _sane_dir(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.rstrip("/") or "/"
        if v == "/" or not v.startswith("/") or posixpath.normpath(v) != v:
            raise ValueError("must be a normalised absolute directory like /var/www/html "
                             "(not '/', relative, or with '.', '..' or '//'); `shipr test` "
                             "prints your login directory")
        return v

    @model_validator(mode="after")
    def _state_has_its_own_dir(self) -> Environment:
        if self.state_path and is_within(self.remote_path, self.state_path):
            raise ValueError(
                "state_path must be a directory of its own, not remote_path itself or one of "
                "its parents: shipr's records (manifest.json, lock, history/, backups/) would "
                "mix with your site's files. Use a dedicated directory, ideally outside the "
                "web root (e.g. /home/<user>/.shipr/<project>)")
        return self

    @property
    def provider_settings(self) -> dict[str, Any]:
        return dict((self.model_extra or {}).get(self.provider) or {})

    @property
    def effective_state_path(self) -> str:
        return self.state_path or f"{self.remote_path}/.shipr"

    @property
    def state_rel(self) -> str | None:
        """The state dir relative to remote_path when it lives inside it (else None)."""
        prefix = self.remote_path + "/"
        sp = self.effective_state_path
        return sp[len(prefix):] if sp.startswith(prefix) else None


class Config:
    def __init__(self, path: Path, raw: dict[str, Any], global_raw: dict[str, Any] | None = None):
        self.path = path
        self.dir = path.parent
        self.raw = raw
        self.global_raw = global_raw or {}
        if raw.get("version", 1) != 1:
            raise ConfigError(f"{path}: unsupported config version {raw.get('version')!r}")
        unknown = set(raw) - TOP_LEVEL_KEYS
        if unknown:
            raise ConfigError(f"{path}: unknown top-level keys: {', '.join(sorted(unknown))}")
        self.project_name = str(_mapping(path, "project", raw.get("project")).get("name")
                                or self.dir.name)
        self.environments: dict[str, Any] = _mapping(path, "environments", raw.get("environments"))
        if not self.environments:
            raise ConfigError(f"{path}: no environments defined")

    def env(self, name: str, interpolate: bool = True) -> Environment:
        """Effective, validated settings for one environment."""
        if name not in self.environments:
            raise ConfigError(
                f"unknown environment {name!r} (have: {', '.join(self.environments)})"
            )
        project = {k: self.raw[k] for k in ("source", "build", "files", "hooks") if k in self.raw}
        merged: dict[str, Any] = {}
        for where, layer in (("global config defaults", self.global_raw.get("defaults")),
                             ("project settings", project),
                             ("defaults", self.raw.get("defaults")),
                             (f"environments.{name}", self.environments[name])):
            merged = deep_merge(merged, _mapping(self.path, where, layer))
        merged["name"] = name
        # checked before the commands split below, which reaches into build as a dict
        _mapping(self.path, f"environment {name!r}: build", merged.get("build"))
        if interpolate:
            # Commands are not interpolated: an expanded secret would be echoed and stored in
            # history. Commands read variables from their environment instead.
            commands = {k: merged.pop(k) for k in ("hooks",) if k in merged}
            build_cmds = (merged.get("build") or {}).pop("commands", None)
            merged = _interpolate(merged, self.variables())
            merged.update(commands)
            if build_cmds is not None:
                merged.setdefault("build", {})["commands"] = build_cmds
        try:
            env = Environment.model_validate(merged)
        except ValidationError as e:
            raise ConfigError(f"{self.path}: environment {name!r} is invalid:\n{e}") from None
        extra = set(env.model_extra or {})
        bad = extra - provider_names()
        if bad:
            raise ConfigError(f"environment {name!r}: unknown keys: {', '.join(sorted(bad))}")
        if env.provider not in provider_names():
            raise ConfigError(f"environment {name!r}: unknown provider {env.provider!r}")
        return env

    def state_overlaps(self, name: str) -> list[str]:
        """Check `name` against every other environment for shared or overlapping state dirs.

        Raises ConfigError when both certainly deploy to the same server (their deploy records
        would overwrite each other). Returns warnings when shipr can't tell: different host
        names may still be one server (aliases, IP vs name), so that is never assumed safe.
        """
        me = self.env(name)
        warnings = []
        for other_name in self.environments:
            if other_name == name:
                continue
            other = self._env_if_valid(other_name)
            clash = other and _state_clash(me, other)
            if not clash:
                continue
            a, b = sorted((me, other), key=lambda e: e.name)  # same text from either side
            same = _same_target(a, b)
            if same:
                raise ConfigError(
                    f"environments {a.name!r} and {b.name!r} deploy to the same server "
                    f"({_describe(a)}) and {clash}: their deploy records would overwrite "
                    "each other. Give each environment its own state_path, outside the other's "
                    "remote_path.")
            if same is None:
                warnings.append(
                    f"shipr can't tell whether {a.name!r} ({_describe(a)}) and {b.name!r} "
                    f"({_describe(b)}) are different servers, and {clash}. If they are the "
                    "same server, deploys would overwrite each other's records. Give each "
                    "environment its own state_path.")
        return warnings

    def _env_if_valid(self, name: str) -> Environment | None:
        """Interpolated when its variables are set, else raw; None if invalid (reported when
        that environment itself is validated)."""
        for interpolate in (True, False):
            try:
                return self.env(name, interpolate=interpolate)
            except ConfigError:
                continue
        return None

    def variables(self) -> dict[str, str]:
        """Values for ${VAR}: the process environment, falling back to the project's .env."""
        dotenv = dotenv_values(self.dir / ".env", interpolate=False)
        values = {k: v for k, v in dotenv.items() if v is not None}
        values.update(os.environ)  # real environment wins over .env
        return values


def _real(env: Environment, path: str) -> str:
    """Where `path` really is: for the local provider, inside its root on this machine."""
    if env.provider != "local":
        return path
    root = Path(str(env.provider_settings.get("root", "/"))).expanduser().resolve()
    return (root / path.lstrip("/")).as_posix()


def _state_clash(a: Environment, b: Environment) -> str | None:
    sa, sb = _real(a, a.effective_state_path), _real(b, b.effective_state_path)
    if is_within(sa, sb) or is_within(sb, sa):
        return (f"both keep deploy records in {sa}" if sa == sb
                else f"their state dirs {sa} and {sb} are nested")
    for x, sx, y in ((a, sa, b), (b, sb, a)):
        ry = _real(y, y.remote_path)
        if is_within(sx, ry):
            return f"{x.name!r} keeps its records in {sx}, inside {y.name!r}'s remote_path {ry}"
    return None


def _host(env: Environment) -> str | None:
    host = env.provider_settings.get("host")
    if not isinstance(host, str) or "${" in host:
        return None
    return host.lower().rstrip(".")


def _same_target(a: Environment, b: Environment) -> bool | None:
    """True: certainly the same server. None: can't tell. (Never "certainly different" for
    remote hosts: two names can be one machine.)"""
    if a.provider == b.provider == "local":
        return True  # same machine; _real already compared the actual paths
    if a.provider == b.provider == "sftp":
        ha, hb = _host(a), _host(b)
        pa, pb = (str(e.provider_settings.get("port", 22)) for e in (a, b))  # may be raw ${..}
        if ha and ha == hb and pa == pb:
            return True
    return None


def _describe(env: Environment) -> str:
    host = env.provider_settings.get("host")
    return f"{env.provider} host {host}" if host else env.provider


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Dicts merge recursively; lists and scalars from `over` replace."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _mapping(path: Path, where: str, value: Any) -> dict[str, Any]:
    """`value` as a dict ({} when empty), or a clean ConfigError instead of a traceback later."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        # type only, never the value: it may hold a literal secret (see hide_input_in_errors)
        raise ConfigError(f"{path}: {where} must be a mapping of keys, not a "
                          f"{type(value).__name__}")
    return value


def _interpolate(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: _interpolate(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, variables) for v in value]
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            if m.group(1) not in variables:
                raise ConfigError(f"environment variable ${{{m.group(1)}}} is not set "
                                  "(set it in your shell or the project's .env)")
            return variables[m.group(1)]
        return _VAR.sub(sub, value)
    return value


def find_config(start: Path) -> Path:
    for d in (start, *start.parents):
        if (d / CONFIG_NAME).is_file():
            return d / CONFIG_NAME
    raise ConfigError(f"no {CONFIG_NAME} found in {start} or its parents (run `shipr init`)")


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at top level")
    return data


def load(path: Path | None = None, cwd: Path | None = None) -> Config:
    path = path or find_config(cwd or Path.cwd())
    global_path = app_dir() / "config.yml"
    global_raw = _read_yaml(global_path) if global_path.is_file() else {}
    return Config(path.resolve(), _read_yaml(path), global_raw)
