"""Phase 3D: configuration and secret hygiene (C-14, C-15, C-17, C-18)."""

import textwrap

import pytest

from shipr import config, files, providers, workspace
from shipr.config import Config, ConfigError
from shipr.providers.base import Provider

SECRET = "hunter2-very-secret"


def write(tmp_path, text, name="shipr.yml"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(text))
    return p


def make(tmp_path, raw, global_raw=None):
    p = tmp_path / "shipr.yml"
    p.write_text("x")
    return Config(p, raw, global_raw)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


# --- C-17: duplicate YAML keys -------------------------------------------------------------------
def test_duplicate_environment_is_rejected_with_its_line(tmp_path, home):
    p = write(tmp_path, """\
        version: 1
        environments:
          production:
            remote_path: /var/www/html
          production:
            remote_path: /var/www/staging
        """)
    with pytest.raises(ConfigError) as e:
        config.load(p)
    msg = str(e.value)
    assert "duplicate key 'production'" in msg and "line 5" in msg and "line 3" in msg, msg


def test_duplicate_secret_key_never_shows_the_value(tmp_path, home):
    p = write(tmp_path, f"""\
        version: 1
        environments:
          prod:
            remote_path: /w
            sftp:
              host: h
              password: {SECRET}
              password: {SECRET}-2
        """)
    with pytest.raises(ConfigError) as e:
        config.load(p)
    assert "duplicate key 'password'" in str(e.value) and "line 8" in str(e.value)
    assert SECRET not in str(e.value)


def test_duplicate_key_in_global_config(tmp_path, home):
    from shipr.paths import app_dir
    app_dir().mkdir(parents=True)
    write(app_dir(), "defaults:\n  confirm: true\n  confirm: false\n", "config.yml")
    p = write(tmp_path, "version: 1\nenvironments:\n  prod: {remote_path: /w}\n")
    with pytest.raises(ConfigError) as e:
        config.load(p)
    assert "config.yml" in str(e.value) and "line 3" in str(e.value)


ANCHORS = """\
    version: 1
    defaults:
      sftp: &base {host: h, username: u, port: 22}
    environments:
      prod:
        remote_path: /var/www/prod
        sftp:
          <<: *base
          port: 2222              # overrides the merged value: legitimate
      staging:
        remote_path: /var/www/staging
        sftp: *base
      qa:
        remote_path: /var/www/qa
        hooks: &qa_hooks
          remote_after: [echo qa]
        sftp:
          <<: [*base, {username: qa}]
    """


def test_anchors_and_merge_keys_still_work(tmp_path, home):
    cfg = config.load(write(tmp_path, ANCHORS))
    assert cfg.env("prod").provider_settings == {"host": "h", "username": "u", "port": 2222}
    assert cfg.env("staging").provider_settings["port"] == 22
    # which source of a merge list wins on a shared key is PyYAML's call (6.0.4 changed it):
    # shipr's loader must agree with plain yaml.safe_load, whichever it is
    import yaml
    plain = yaml.safe_load(textwrap.dedent(ANCHORS))["environments"]["qa"]["sftp"]["username"]
    assert cfg.env("qa").provider_settings["username"] == plain
    assert cfg.env("qa").hooks.remote_after == ["echo qa"]


def test_explicit_duplicate_next_to_a_merge_is_still_rejected(tmp_path, home):
    p = write(tmp_path, ANCHORS.replace("port: 2222              # overrides the merged value:"
                                        " legitimate",
                                        "port: 2222\n          port: 2200"))
    with pytest.raises(ConfigError, match="line 10.*duplicate key 'port'"):
        config.load(p)


def test_invalid_yaml_error_does_not_quote_the_line(tmp_path, home):
    # PyYAML's own message prints the offending source line, which here holds a password
    p = write(tmp_path, f"version: 1\nenvironments:\n  prod:\n    password: {SECRET}: x\n")
    with pytest.raises(ConfigError) as e:
        config.load(p)
    assert "invalid YAML" in str(e.value) and "line 4" in str(e.value)
    assert SECRET not in str(e.value)


# --- C-15: provider settings are validated without connecting -----------------------------------
def env_with(sftp, **extra):
    return {"environments": {"prod": {"remote_path": "/w", "sftp": sftp, **extra}}}


@pytest.mark.parametrize("sftp, problem", [
    ({"hots": "h", "username": "u"}, "hots"),  # misspelled
    ({"host": "h", "username": "u", "pasword": "x"}, "pasword"),
    ({"host": "h"}, "username"),  # missing
    (None, "host"),  # no sftp section at all
    ({"host": "h", "username": "u", "host_key": "MD5:ab:cd"}, "host_key"),
    ({"host": "h", "username": "u", "host_key": []}, "host_key"),
    ({"host": "h", "username": "u", "port": 70000}, "port"),
    ({"host": "h", "username": "u", "timeout": 0}, "timeout"),
    ({"host": "h", "username": "u", "retries": -1}, "retries"),
])
def test_bad_sftp_settings_fail_validation(tmp_path, sftp, problem):
    with pytest.raises(ConfigError, match=f"(?s)invalid sftp settings.*{problem}"):
        make(tmp_path, env_with(sftp)).env("prod")


@pytest.mark.parametrize("sftp", [
    {"host": "h", "username": "u", "pasword": SECRET},  # misspelled key holding a secret
    {"host": "h", "username": "u", "password": 12345678},  # wrong type
    {"host": "h", "username": "u", "passphrase": [SECRET]},
    {"host": "h", "username": "u", "password": {"value": SECRET}},
    {"host": "h", "username": "u", "host_key": SECRET},
    {"host": "h", "password": SECRET},  # missing username next to a secret
])
def test_sftp_validation_errors_never_show_secrets(tmp_path, sftp):
    with pytest.raises(ConfigError) as e:
        make(tmp_path, env_with(sftp)).env("prod")
    assert SECRET not in str(e.value) and "12345678" not in str(e.value)


def test_secret_from_a_variable_never_shows_either(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIPR_T_PW", SECRET)
    with pytest.raises(ConfigError) as e:
        make(tmp_path, env_with({"host": "h", "pasword": "${SHIPR_T_PW}"})).env("prod")
    assert SECRET not in str(e.value)


def test_valid_sftp_settings_pass(tmp_path):
    env = make(tmp_path, env_with({"host": "h", "username": "u", "password": "p", "port": 2222,
                                   "host_key": "SHA256:" + "A" * 43})).env("prod")
    assert env.provider_settings["port"] == 2222


class _Plain(Provider):
    """A third-party provider written before validate_settings existed."""
    name = "plain"
    connect = close = stat = walk = upload = download = delete = rename = None
    rmdir_if_empty = create_exclusive = None


class _Strict(_Plain):
    @classmethod
    def validate_settings(cls, settings):
        if "bucket" not in settings:
            raise providers.ProviderError("bucket is required")


@pytest.fixture
def third_party(monkeypatch):
    class EP:
        def __init__(self, cls):
            self.cls = cls

        def load(self):
            return self.cls
    real = providers._registry
    monkeypatch.setattr(providers, "_registry", lambda: {
        **real(), "plain": EP(_Plain), "strict": EP(_Strict)})


def test_third_party_provider_without_validation_still_works(tmp_path, third_party):
    raw = {"environments": {"prod": {"remote_path": "/w", "provider": "plain",
                                     "plain": {"anything": 1}}}}
    assert make(tmp_path, raw).env("prod").provider_settings == {"anything": 1}


def test_third_party_provider_can_validate(tmp_path, third_party):
    raw = {"environments": {"prod": {"remote_path": "/w", "provider": "strict",
                                     "strict": {}}}}
    with pytest.raises(ConfigError, match="bucket is required"):
        make(tmp_path, raw).env("prod")


def test_unset_variables_still_compared_for_state_overlaps(tmp_path, monkeypatch):
    # an environment whose variables aren't set is compared raw, never validated raw
    monkeypatch.delenv("SHIPR_T_PORT", raising=False)
    cfg = make(tmp_path, {"environments": {
        "prod": {"remote_path": "/var/www/html", "sftp": {"host": "a", "username": "u"}},
        "qa": {"remote_path": "/var/www/html",
               "sftp": {"host": "b", "username": "u", "port": "${SHIPR_T_PORT}"}}}})
    assert len(cfg.state_overlaps("prod")) == 1


# --- C-14: which variables supply provider settings ----------------------------------------------
def test_credential_variables_follow_layers_nesting_and_interpolation(tmp_path, monkeypatch):
    for var in ("PW_A", "PW_B", "OLD_PW", "HOST", "PP", "KEYDIR", "PIN1", "PIN2"):
        monkeypatch.delenv(var, raising=False)  # identified without being set
    raw = {"defaults": {"sftp": {"password": "${OLD_PW}", "username": "u"}},
           "environments": {"prod": {"remote_path": "/w", "sftp": {
               "host": "${HOST}",
               "password": "${PW_A}${PW_B}",  # two in one value; replaces the default's
               "passphrase": "pre-${PP}-post",
               "host_key": ["${PIN1}", "SHA256:x", "${PIN2}"]}}}}
    cfg = make(tmp_path, raw, {"defaults": {"sftp": {"key_file": "${KEYDIR}/id_ed25519"}}})
    assert cfg.credential_variables("prod") == {
        "KEYDIR": ["sftp.key_file"], "HOST": ["sftp.host"], "PW_A": ["sftp.password"],
        "PW_B": ["sftp.password"], "PP": ["sftp.passphrase"], "PIN1": ["sftp.host_key[0]"],
        "PIN2": ["sftp.host_key[2]"]}


def test_credential_variables_of_another_provider(tmp_path):
    raw = {"environments": {"prod": {
        "remote_path": "/w", "provider": "local", "local": {"root": "${ROOT}"},
        "sftp": {"password": "${UNUSED}"}}}}  # not this environment's provider
    assert make(tmp_path, raw).credential_variables("prod") == {"ROOT": ["local.root"]}


def cred_env(tmp_path, monkeypatch, **sections):
    monkeypatch.setenv("SHIPR_T_PW", SECRET)
    raw = {**sections, "environments": {"prod": {"remote_path": "/w", "sftp": {
        "host": "h", "username": "u", "password": "${SHIPR_T_PW}"}}}}
    cfg = make(tmp_path, raw)
    return cfg, cfg.env("prod")


@pytest.mark.parametrize("sections", [
    {"build": {"commands": ["npm ci"]}},
    {"hooks": {"local_before": ["make"]}},
    {"hooks": {"local_after": ["notify"]}},
])
def test_local_commands_without_allowlist_warn_by_name(tmp_path, monkeypatch, sections):
    w = workspace.credential_warning(*cred_env(tmp_path, monkeypatch, **sections))
    assert w and "SHIPR_T_PW (sftp.password)" in w and "build.env" in w
    assert "next major" in w and SECRET not in w


@pytest.mark.parametrize("sections", [
    {"build": {"commands": ["npm ci"], "env": ["NODE_ENV"]}},  # explicit allowlist
    {"build": {"commands": ["npm ci"], "env": []}},
    {"hooks": {"remote_after": ["php artisan migrate"]}},  # runs on the server
    {},
])
def test_no_credential_warning_without_exposure(tmp_path, monkeypatch, sections):
    assert workspace.credential_warning(*cred_env(tmp_path, monkeypatch, **sections)) is None


def test_credential_warning_without_known_credential_variables(tmp_path):
    raw = {"build": {"commands": ["npm ci"]},
           "environments": {"prod": {"remote_path": "/w",
                                     "sftp": {"host": "h", "username": "u"}}}}
    cfg = make(tmp_path, raw)
    w = workspace.credential_warning(cfg, cfg.env("prod"))
    assert w and "whole environment" in w and "build.env" in w


# --- C-18: what default_excludes: false lets through ---------------------------------------------
def test_default_excluded_names_the_files():
    paths = ["index.php", ".env", ".env.production", "config/.env", "keys/deploy.pem",
             "id_rsa.pub", "shipr.yml", "app/environment.ts", "logs/app.log", "server.key",
             "envs/readme.md"]
    assert files.default_excluded(paths) == [
        ".env", ".env.production", "config/.env", "id_rsa.pub", "keys/deploy.pem",
        "logs/app.log", "server.key", "shipr.yml"]


def test_default_excludes_warning_lists_names_only():
    w = workspace.default_excludes_warning([".env", "a.pem"], "in this deploy")
    assert ".env, a.pem" in w and "default_excludes" in w
    many = workspace.default_excludes_warning([f"k{i}.pem" for i in range(30)], "x")
    assert "k19.pem" in many and "k20.pem" not in many and "10 more" in many
    assert "none" in workspace.default_excludes_warning([], "in this deploy")
    assert "--dry-run" in workspace.default_excludes_warning(None, "committed at HEAD")
