import pytest

from shipr.config import Config, ConfigError, deep_merge


def make(tmp_path, raw, global_raw=None):
    p = tmp_path / "shipr.yml"
    p.write_text("x")
    return Config(p, raw, global_raw)


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
    assert env.effective_state_path == "/var/www/.shipr"


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
           "environments": {"prod": {"remote_path": "/w"}}}
    env = make(tmp_path, raw).env("prod")
    assert env.build.commands == ["curl -H ${TOKEN}"]
    assert env.hooks.remote_after == ["echo ${TOKEN}"]


def test_dotenv_values_are_taken_literally(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PW='a${B}c'\n")
    monkeypatch.delenv("PW", raising=False)
    raw = {"environments": {"prod": {"remote_path": "/w", "sftp": {"password": "${PW}"}}}}
    assert make(tmp_path, raw).env("prod").provider_settings["password"] == "a${B}c"
