import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_user_dirs(tmp_path_factory, monkeypatch):
    """No test may read or write the real per-user config or known_hosts. click finds shipr's
    folder through XDG_CONFIG_HOME or HOME (Linux, macOS) and APPDATA (Windows), so all of them
    point into a temp folder; the assert proves it on every platform."""
    from shipr.paths import app_dir

    home = tmp_path_factory.mktemp("home")
    for var, value in (("HOME", home), ("USERPROFILE", home),
                       ("XDG_CONFIG_HOME", home / ".config"),
                       ("APPDATA", home / "AppData" / "Roaming"),
                       ("LOCALAPPDATA", home / "AppData" / "Local")):
        monkeypatch.setenv(var, str(value))
    assert app_dir().is_relative_to(home), app_dir()


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Project:
    """A throwaway git repo with a shipr.yml that deploys to a local 'server' folder."""

    def __init__(self, tmp: Path, extra_yaml: str = ""):
        self.repo = tmp / "repo"
        self.server = tmp / "server"
        self.repo.mkdir()
        self.server.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        self.write_config(extra_yaml)

    def write_config(self, extra_yaml: str = "") -> None:
        (self.repo / "shipr.yml").write_text(
            "version: 1\nproject: {name: demo}\n" + extra_yaml +
            "defaults:\n  provider: local\n  local: {root: '" + self.server.as_posix() + "'}\n"
            "environments:\n  prod:\n    remote_path: /site\n",
            encoding="utf-8")

    def write(self, rel: str, text: str) -> None:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def commit(self, msg: str = "c") -> str:
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True,
                              text=True).stdout.strip()

    @property
    def site(self) -> Path:
        return self.server / "site"

    @property
    def state(self) -> Path:
        """shipr's records: by default beside the web root, <parent>/.shipr/<name>."""
        return self.server / ".shipr" / "site"

    def remote_files(self) -> set[str]:
        return {p.relative_to(self.site).as_posix() for p in self.site.rglob("*")
                if p.is_file() and ".shipr" not in p.relative_to(self.site).parts}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # isolate global config / known_hosts
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    return Project(tmp_path)
