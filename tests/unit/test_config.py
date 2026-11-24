import pytest

from shipr.config import Config, ConfigError, deep_merge


def make(tmp_path, raw, global_raw=None):
    p = tmp_path / "shipr.yml"
    p.write_text("x")
    return Config(p, raw, global_raw)


SFTP = {"host": "h", "username": "u"}  # the least valid sftp settings
BASE = {"environments": {"prod": {"remote_path": "/var/www", "sftp": {"host": "h"}}}}


def test_deep_merge_dicts_merge_lists_replace():
    assert deep_merge({"a": {"x": 1, "l": [1]}}, {"a": {"y": 2, "l": [2]}}) == \
        {"a": {"x": 1, "y": 2, "l": [2]}}


def test_layering_global_project_defaults_env(tmp_path):
    raw = {
        "files": {"exclude": ["a"]},
        "defaults": {"sftp": {"username": "u", "port": 2222}, "confirm": True},
        "environments": {"prod": {"remote_path": "/var/www", "sftp": {"host": "h"},
                                  "files": {"exclude": ["b"]}}},
    }
    env = make(tmp_path, raw, {"defaults": {"sftp": {"port": 22, "timeout": 5}}}).env("prod")
    assert env.provider_settings == {"username": "u", "port": 2222, "timeout": 5, "host": "h"}
    assert env.confirm is True
    assert env.files.exclude == ["b"]
    assert env.effective_state_path == "/var/.shipr/www"  # beside the web root


def test_interpolation_env_beats_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PW=fromfile\nUSER=fileuser\n")
    monkeypatch.setenv("PW", "fromshell")
    monkeypatch.delenv("USER", raising=False)
    raw = {"environments": {"prod": {"remote_path": "/w",
                                     "sftp": {"host": "h", "password": "${PW}",
                                              "username": "${USER}"}}}}
    s = make(tmp_path, raw).env("prod").provider_settings
    assert s["password"] == "fromshell" and s["username"] == "fileuser"


def test_missing_variable_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE_X", raising=False)
    raw = {"environments": {"prod": {"remote_path": "/w", "sftp": {"password": "${NOPE_X}"}}}}
    with pytest.raises(ConfigError, match="NOPE_X"):
        make(tmp_path, raw).env("prod")


@pytest.mark.parametrize("bad", ["/", "relative/path", "/var/../etc", "", "~/www",
                                 "/var/./www", "/var//www"])
def test_remote_path_must_be_safe(tmp_path, bad):
    raw = {"environments": {"prod": {"remote_path": bad}}}
    with pytest.raises(ConfigError, match="remote_path"):
        make(tmp_path, raw).env("prod")


@pytest.mark.parametrize("raw, global_raw, where", [
    ({**BASE, "build": "npm run build"}, None, "build"),
    ({"environments": {"prod": {"remote_path": "/w", "build": ["npm ci"]}}}, None, "build"),
    ({**BASE, "defaults": "sftp"}, None, "defaults"),
    ({"environments": {"prod": "/var/www"}}, None, "environments.prod"),
    (BASE, {"defaults": ["x"]}, "global config defaults"),
])
def test_non_mapping_sections_are_config_errors(tmp_path, raw, global_raw, where):
    # used to escape as AttributeError/TypeError tracebacks from deep_merge or build.pop
    with pytest.raises(ConfigError, match=f"{where} must be a mapping"):
        make(tmp_path, raw, global_raw).env("prod")


@pytest.mark.parametrize("raw, where", [
    ({**BASE, "project": "demo"}, "project"),
    ({"environments": ["prod", "staging"]}, "environments"),
])
def test_non_mapping_top_level_sections_are_config_errors(tmp_path, raw, where):
    with pytest.raises(ConfigError, match=f"{where} must be a mapping"):
        make(tmp_path, raw)


def test_unknown_keys_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown top-level"):
        make(tmp_path, {**BASE, "deploy": {}})
    raw = {"environments": {"prod": {"remote_path": "/w", "sfpt": {}}}}
    with pytest.raises(ConfigError, match="sfpt"):
        make(tmp_path, raw).env("prod")


def test_validation_errors_do_not_leak_secrets(tmp_path):
    raw = {"environments": {"prod": {"remote_path": "/w", "confirm": "hunter2-secret"}}}
    with pytest.raises(ConfigError) as e:
        make(tmp_path, raw).env("prod")
    assert "hunter2" not in str(e.value)


def test_state_path_validated(tmp_path):
    raw = {"environments": {"prod": {"remote_path": "/w", "state_path": "rel/dir"}}}
    with pytest.raises(ConfigError, match="state_path"):
        make(tmp_path, raw).env("prod")


def test_commands_are_not_interpolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN", "s3cret")
    raw = {"build": {"commands": ["curl -H ${TOKEN}"]},
           "hooks": {"remote_after": ["echo ${TOKEN}"]},
           "environments": {"prod": {"remote_path": "/w", "sftp": SFTP}}}
    env = make(tmp_path, raw).env("prod")
    assert env.build.commands == ["curl -H ${TOKEN}"]
    assert env.hooks.remote_after == ["echo ${TOKEN}"]


def test_dotenv_values_are_taken_literally(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PW='a${B}c'\n")
    monkeypatch.delenv("PW", raising=False)
    raw = {"environments": {"prod": {"remote_path": "/w",
                                     "sftp": {**SFTP, "password": "${PW}"}}}}
    assert make(tmp_path, raw).env("prod").provider_settings["password"] == "a${B}c"


# --- state_path overlap (C-4) -------------------------------------------------------------------
def env_raw(remote, state=None, host="h", **extra):
    e = {"remote_path": remote, "sftp": {"host": host, "username": "u"}, **extra}
    if state:
        e["state_path"] = state
    return e


@pytest.mark.parametrize("remote, state", [("/var/www", "/var/www"), ("/var/www/html", "/var/www"),
                                           ("/var/www/html", "/")])
def test_state_path_must_not_be_remote_path_or_a_parent(tmp_path, remote, state):
    raw = {"environments": {"prod": env_raw(remote, state)}}
    with pytest.raises(ConfigError, match="state_path"):
        make(tmp_path, raw).env("prod")


@pytest.mark.parametrize("state", ["/var/www/html/.shipr", "/var/www/html/a/b", "/srv/shipr",
                                   "/var/www/htmlstate"])
def test_state_path_inside_or_beside_remote_path_is_fine(tmp_path, state):
    raw = {"environments": {"prod": env_raw("/var/www/html", state)}}
    assert make(tmp_path, raw).env("prod").effective_state_path == state


@pytest.mark.parametrize("a, b", [
    (env_raw("/var/www/a", "/srv/state"), env_raw("/var/www/b", "/srv/state")),  # shared
    (env_raw("/var/www/a", "/srv/state"), env_raw("/var/www/b", "/srv/state/b")),  # nested
    (env_raw("/var/www/html"), env_raw("/var/www/html/staging")),  # b's state in a's web root
    (env_raw("/var/www/a", "/srv/s", host="Example.COM."), env_raw("/var/www/b", "/srv/s",
                                                                   host="example.com")),
])
def test_same_server_overlapping_state_is_an_error(tmp_path, a, b):
    cfg = make(tmp_path, {"environments": {"prod": a, "staging": b}})
    for name in ("prod", "staging"):
        with pytest.raises(ConfigError, match="same server"):
            cfg.state_overlaps(name)


def test_unknown_identity_overlap_is_a_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("STAGING_HOST", raising=False)
    cfg = make(tmp_path, {"environments": {
        "prod": env_raw("/var/www/html", host="a.example.com"),
        "staging": env_raw("/var/www/html", host="b.example.com"),
        "qa": env_raw("/var/www/html", host="${STAGING_HOST}"),  # unresolvable: still compared
    }})
    warnings = cfg.state_overlaps("prod")
    assert len(warnings) == 2 and all("can't tell" in w for w in warnings)
    assert "/var/www/.shipr/html" in warnings[0]


def test_non_overlapping_environments_are_quiet(tmp_path):
    cfg = make(tmp_path, {"environments": {"prod": env_raw("/var/www/html"),
                                           "staging": env_raw("/var/www/staging")}})
    assert cfg.state_overlaps("prod") == []


def test_local_provider_compares_real_paths(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    local = {"provider": "local", "local": {"root": str(root)}}
    cfg = make(tmp_path, {"environments": {
        "a": {"remote_path": "/x", "state_path": "/s", **local},
        "b": {"remote_path": "/y", "state_path": "/s", **local}}})
    with pytest.raises(ConfigError, match="same server"):
        cfg.state_overlaps("a")
    other = tmp_path / "other"
    other.mkdir()
    cfg = make(tmp_path, {"environments": {
        "a": {"remote_path": "/x", "state_path": "/s", **local},
        "b": {"remote_path": "/y", "state_path": "/s", "provider": "local",
              "local": {"root": str(other)}}}})
    assert cfg.state_overlaps("a") == []


@pytest.mark.parametrize("remote, state", [("/var/www/html", "/var/www/.shipr/html"),
                                           ("/home/u1/public_html", "/home/u1/.shipr/public_html"),
                                           ("/srv", "/.shipr/srv")])
def test_default_state_dir_is_beside_the_web_root(tmp_path, remote, state):
    env = make(tmp_path, {"environments": {"prod": env_raw(remote)}}).env("prod")
    assert env.effective_state_path == state and env.state_rel is None


def test_hook_timeout_defaults_to_30_minutes_and_can_be_disabled(tmp_path):
    raw = {"environments": {"prod": {"remote_path": "/w", "sftp": SFTP}}}
    assert make(tmp_path, raw).env("prod").hooks.timeout == 1800
    raw["hooks"] = {"timeout": None}
    assert make(tmp_path, raw).env("prod").hooks.timeout is None
    for bad in (0, -5, "soon"):
        raw["hooks"] = {"timeout": bad}
        with pytest.raises(ConfigError, match="timeout"):
            make(tmp_path, raw).env("prod")


def test_server_check_and_server_commit(tmp_path):
    env = {"remote_path": "/w", "sftp": SFTP}
    assert make(tmp_path, {"environments": {"prod": env}}).env("prod").server_check == "full"
    assert make(tmp_path, {"environments": {"prod": env}}).env("prod").backup_replaced is True
    ok = make(tmp_path, {"environments": {"prod": {**env, "server_check": "changed",
                                                   "server_commit": "ee9df082f"}}})
    assert ok.env("prod").server_commit == "ee9df082f"
    assert make(tmp_path, {"environments": {"prod": {**env, "server_check": "changed"}}}) \
        .env("prod").server_commit is None  # first deploy then checks everything once
    for bad, msg in (({"server_commit": "ee9df082f"}, "server_check: changed"),
                     ({"server_check": "fast"}, "server_check")):
        with pytest.raises(ConfigError, match=msg):
            make(tmp_path, {"environments": {"prod": {**env, **bad}}}).env("prod")
