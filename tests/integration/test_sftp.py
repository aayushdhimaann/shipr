import uuid

import pytest

from shipr import admin, config, engine
from shipr.errors import ProviderError
from shipr.providers.sftp import SFTPProvider
from tests import helpers
from tests.helpers import RecordingUI, load_env

from .conftest import PASSPHRASE, PASSWORD

pytestmark = pytest.mark.integration


def configure(project, server, auth: str, extra_env: str = "", top: str = "", **sftp):
    remote = f"{server['root']}/site-{uuid.uuid4().hex[:6]}"
    sftp_yaml = "".join(f"      {k}: {v}\n" for k, v in sftp.items())
    (project.repo / "shipr.yml").write_text(
        f"version: 1\nproject: {{name: demo}}\n{top}"
        "environments:\n  prod:\n"
        f"    remote_path: {remote}\n{extra_env}"
        "    sftp:\n      host: 127.0.0.1\n"
        f"      port: {server['port']}\n      username: {server['user']}\n"
        f"      retries: 1\n      timeout: 5\n{auth}{sftp_yaml}")
    return remote


def deploy(project, ui=None, **opts):
    opts.setdefault("trust_new_host", True)
    return helpers.deploy(project.repo, ui, **opts)


def provider(project, trust=True) -> SFTPProvider:
    return engine.open_provider(load_env(project.repo)[1], trust_new_host=trust)


def test_password_auth_full_incremental_delete_and_hooks(project, ssh_server, monkeypatch):
    monkeypatch.setenv("SHIPR_TEST_PW", PASSWORD)
    remote = configure(project, ssh_server, '      password: "${SHIPR_TEST_PW}"\n',
                       top='hooks:\n  remote_after: ["echo deployed > hook.txt"]\n')
    project.write("index.php", "<?php echo 1;")
    project.write("old.txt", "old")
    project.write(".env", "DB_PASS=x")
    project.commit()
    rec = deploy(project)
    assert rec.status == "success" and len(rec.uploaded) == 2

    with provider(project) as p:
        files = p.walk(remote, skip=f"{remote}/.shipr")
        assert set(files) == {"index.php", "old.txt", "hook.txt"}
        assert p.read_text(f"{remote}/hook.txt").strip() == "deployed"

    project.write("index.php", "<?php echo 2;")
    (project.repo / "old.txt").unlink()
    project.commit()
    rec = deploy(project, yes=True)
    assert [u.path for u in rec.uploaded] == ["index.php"]
    assert [d.path for d in rec.deleted] == ["old.txt"]
    with provider(project) as p:
        assert p.read_text(f"{remote}/index.php") == "<?php echo 2;"
        assert p.stat(f"{remote}/old.txt") is None


def test_key_with_passphrase_and_hotfix_backup(project, ssh_server, keypair):
    remote = configure(project, ssh_server,
                       f"      key_file: {keypair[0].as_posix()}\n"
                       f"      passphrase: {PASSPHRASE}\n")
    project.write("a.php", "v1")
    project.commit()
    deploy(project)
    with provider(project) as p:
        p.exec("sleep 1 && echo hotfix >> a.php", cwd=remote)
    rec = deploy(project)
    assert rec.uploaded[0].backup
    with provider(project) as p:
        assert p.read_text(f"{remote}/a.php") == "v1"
        assert "hotfix" in p.read_text(rec.uploaded[0].backup)


def test_first_deploy_hashes_remote_with_sha256sum(project, ssh_server):
    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n")
    project.write("same.txt", "same")
    project.write("diff.txt", "new!")
    project.commit()
    with provider(project) as p:
        p.exec(f"mkdir -p {remote} && printf same > {remote}/same.txt && "
               f"printf old! > {remote}/diff.txt")
    rec = deploy(project)
    assert [u.path for u in rec.uploaded] == ["diff.txt"]


def test_wrong_password(project, ssh_server):
    configure(project, ssh_server, "      password: wrong\n")
    with pytest.raises(ProviderError, match="authentication failed"):
        provider(project).connect()


def test_unknown_host_rejected_then_trusted_then_mismatch(project, ssh_server):
    configure(project, ssh_server, f"      password: {PASSWORD}\n")
    with pytest.raises(ProviderError, match="--trust"):
        provider(project, trust=False).connect()
    with provider(project, trust=True):
        pass
    with provider(project, trust=False):  # now remembered
        pass
    known = config.app_dir() / "known_hosts"
    host, _, _ = known.read_text().split(" ", 2)
    import paramiko

    fake = paramiko.RSAKey.generate(1024)
    known.write_text(f"{host} {fake.get_name()} {fake.get_base64()}\n")
    with pytest.raises(ProviderError, match="HOST KEY MISMATCH"):
        provider(project, trust=False).connect()


def test_unreachable_host_retries_then_fails(project, ssh_server):
    from .conftest import free_port

    configure(project, {**ssh_server, "port": free_port()}, f"      password: {PASSWORD}\n")
    with pytest.raises(ProviderError, match="could not connect"):
        provider(project).connect()


def test_reconnects_after_dropped_connection(project, ssh_server, tmp_path):
    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n")
    f = tmp_path / "x.txt"
    f.write_text("x")
    with provider(project) as p:
        p.client.get_transport().close()  # simulate network drop
        p.upload(f, f"{remote}/x.txt")
        assert p.read_text(f"{remote}/x.txt") == "x"


def test_lock_is_exclusive_over_sftp(project, ssh_server):
    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n")
    with provider(project) as p:
        assert p.create_exclusive(f"{remote}/.shipr/lock", "a") is True
        assert p.create_exclusive(f"{remote}/.shipr/lock", "b") is False
        # same content = our own write whose reply was lost: recognised, not a lockout
        assert p.create_exclusive(f"{remote}/.shipr/lock", "a") is True


def test_sftp_only_server_uploads_and_skips_hooks(project, sftp_only_server):
    remote = configure(project, sftp_only_server, f"      password: {PASSWORD}\n",
                       top='hooks:\n  remote_after: ["echo hi"]\n')
    project.write("index.html", "<h1>hi</h1>")
    project.write("old.html", "old")
    project.commit()
    ui = RecordingUI()
    rec = deploy(project, ui=ui)
    assert rec.status == "success" and "remote_after hooks skipped" in ui.text
    (project.repo / "old.html").unlink()
    project.commit()
    rec = deploy(project, ui=RecordingUI(answers=[True]))
    assert [d.path for d in rec.deleted] == ["old.html"]
    with provider(project) as p:
        assert set(p.walk(remote, skip=f"{remote}/.shipr")) == {"index.html"}


def test_cli_test_command(project, ssh_server):
    configure(project, ssh_server, f"      password: {PASSWORD}\n")
    ui = RecordingUI()
    assert admin.check_connection(load_env(project.repo)[1], trust=True, ui=ui)
    assert "shell commands: available" in ui.text and "write access" in ui.text
