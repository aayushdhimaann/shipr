import stat
import subprocess
import uuid

import paramiko
import pytest

from shipr import admin, config, engine
from shipr.errors import ProviderError, UnsafePath
from shipr.providers.sftp import SFTPProvider
from tests import helpers
from tests.helpers import RecordingUI, load_env

from .conftest import DOCKER_ENV, PASSPHRASE, PASSWORD

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
    with provider(project, trust=True):  # confirm the test server's key once (like a user)
        pass
    return helpers.deploy(project.repo, ui, **opts)


def provider(project, trust=True) -> SFTPProvider:
    """trust=True: --trust plus a "yes" at the fingerprint prompt."""
    return engine.open_provider(load_env(project.repo)[1], trust_new_host=trust,
                                confirm_host_key=(lambda host, key: True) if trust else None)


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
    ui = RecordingUI(trust_hosts=True)
    assert admin.check_connection(load_env(project.repo)[1], trust=True, ui=ui)
    assert "shell commands: available" in ui.text and "write access" in ui.text
    assert "host key: ssh-" in ui.text and "SHA256:SHA256:" not in ui.text


# --- filesystem safety on a real OpenSSH server (C-1, C-2, C-7) ---------------------------------
def test_symlinked_dir_and_file_are_never_written_through(project, ssh_server):
    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n",
                       top="files:\n  protect: ['config.php']\n")
    shared = f"{remote}-shared"
    with provider(project) as p:
        p.exec(f"mkdir -p {remote} {shared}/storage && printf DATA > {shared}/storage/data.txt "
               f"&& printf REAL > {shared}/config.php && ln -s {shared}/storage {remote}/storage "
               f"&& ln -s {shared}/config.php {remote}/config.php")
    project.write("storage/data.txt", "from git")
    project.write("config.php", "sample")
    project.write("index.php", "i")
    project.commit()
    rec = deploy(project)
    assert [u.path for u in rec.uploaded] == ["index.php"]
    assert sorted(rec.blocked) == ["config.php", "storage/data.txt"]
    with provider(project) as p:
        _, out, _ = p.exec(f"cat {shared}/storage/data.txt {shared}/config.php; echo; "
                           f"test -L {remote}/config.php && echo LINK")
    assert out.split() == ["DATAREAL", "LINK"]


def test_unreadable_dir_is_reported_not_fatal(project, ssh_server):
    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n")
    with provider(project) as p:
        p.exec(f"mkdir -p {remote}/cache && chmod 000 {remote}/cache")
    project.write("cache/x.txt", "x")
    project.write("index.php", "i")
    project.commit()
    try:
        rec = deploy(project)
        assert rec.status == "success" and rec.blocked == ["cache/x.txt"]
    finally:
        with provider(project) as p:
            p.exec(f"chmod 755 {remote}/cache")


def test_symlink_swapped_in_after_planning(project, ssh_server):
    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n",
                       extra_env="    confirm: true\n")
    project.write("assets/a.css", "v1")
    project.commit()
    deploy(project, yes=True)
    project.write("assets/a.css", "v2")
    project.commit()

    class SwapUI(RecordingUI):
        def _confirm(self, msg):
            with provider(project) as p:
                p.exec(f"mv {remote}/assets {remote}-moved && ln -s {remote}-moved "
                       f"{remote}/assets")
            return True

    rec = deploy(project, ui=SwapUI())
    assert rec.status == "partial" and "symlink" in rec.failed[0].error
    with provider(project) as p:
        assert p.exec(f"cat {remote}-moved/a.css")[1] == "v1"


def test_guard_works_on_sftp_only_server(project, sftp_only_server, tmp_path):
    remote = configure(project, sftp_only_server, f"      password: {PASSWORD}\n")
    f = tmp_path / "x.txt"
    f.write_text("x")
    from shipr.errors import UnsafePath

    with provider(project) as p:
        p._sftp.mkdir(remote)
        p._sftp.mkdir(f"{remote}-other")
        p._sftp.symlink(f"{remote}-other", f"{remote}/linked")
        with pytest.raises(UnsafePath, match="is a symlink"):
            p.upload(f, f"{remote}/linked/x.txt")
        assert p.walk(remote)["linked"].kind == "symlink"
        assert p._sftp.listdir(f"{remote}-other") == []


# --- state location (Phase 2B: C-3) ------------------------------------------------------------
def test_default_state_beside_web_root_and_clean_error_when_not_creatable(project, ssh_server):
    import posixpath

    from shipr.errors import StateError

    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n")
    project.write("a.txt", "a")
    project.commit()
    deploy(project)
    parent, name = posixpath.split(remote)
    with provider(project) as p:
        assert p.stat(f"{parent}/.shipr/{name}/manifest.json")
        assert set(p.walk(remote)) == {"a.txt"}  # nothing of shipr's inside the web root
        locked = f"{parent}/ro-{name}"
        p.exec(f"mkdir -p {locked}/site && chmod 555 {locked}")
    cfg = project.repo / "shipr.yml"
    cfg.write_text(cfg.read_text().replace(remote, f"{locked}/site"))
    try:
        with pytest.raises(StateError, match="state_path"):
            deploy(project)
        with provider(project) as p:
            assert p.walk(f"{locked}/site") == {}  # stopped before any change
    finally:
        with provider(project) as p:
            p.exec(f"chmod 755 {locked}")


# --- host keys (Phase 2C: C-10) -----------------------------------------------------------------
def server_fingerprint(project) -> str:
    with provider(project, trust=True) as p:
        fp = p.host_fingerprint.split()[1]
    (config.app_dir() / "known_hosts").unlink()  # forget it again: like a fresh CI runner
    return fp


def test_pinned_host_key_works_without_known_hosts(project, ssh_server):
    configure(project, ssh_server, f"      password: {PASSWORD}\n")
    fp = server_fingerprint(project)
    configure(project, ssh_server, f"      password: {PASSWORD}\n", host_key=fp)
    project.write("a.txt", "a")
    project.commit()
    rec = helpers.deploy(project.repo)  # no --trust, no known_hosts entry: the pin decides
    assert rec.status == "success"
    assert not (config.app_dir() / "known_hosts").exists()


def test_wrong_pin_is_refused_even_when_known_hosts_trusts_the_key(project, ssh_server):
    configure(project, ssh_server, f"      password: {PASSWORD}\n")
    with provider(project, trust=True):
        pass  # now in shipr's known_hosts
    configure(project, ssh_server, f"      password: {PASSWORD}\n",
              host_key="SHA256:" + "A" * 43)
    with pytest.raises(ProviderError, match="HOST KEY MISMATCH"):
        provider(project, trust=False).connect()


def test_noninteractive_trust_never_accepts_an_unknown_key(project, ssh_server, monkeypatch):
    from click.testing import CliRunner

    from shipr.cli import cli

    configure(project, ssh_server, f"      password: {PASSWORD}\n")
    project.write("a.txt", "a")
    project.commit()
    monkeypatch.chdir(project.repo)
    r = CliRunner().invoke(cli, ["deploy", "--trust", "--yes"])  # stdin is not a terminal
    assert r.exit_code == 1 and "host_key: SHA256:" in r.output, r.output
    assert not (config.app_dir() / "known_hosts").exists()


def test_real_sha256sum_output_is_accepted_without_downloading(project, ssh_server, tmp_path):
    """C-16: the strict parser must still accept genuine GNU sha256sum output."""
    import hashlib

    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n")
    names = ["plain.txt", "with space.txt", "ünïcode.txt", "a'quote.txt"]
    with provider(project) as p:
        for n in names:
            f = tmp_path / "src"
            f.write_text(n)
            p.upload(f, f"{remote}/{n}")
        p.download = lambda *a: pytest.fail("fell back to downloading")
        got = p.sha256([f"{remote}/{n}" for n in names])
    assert got == {f"{remote}/{n}": hashlib.sha256(n.encode()).hexdigest() for n in names}


def test_pin_wins_over_a_stale_known_hosts_entry(project, ssh_server):
    configure(project, ssh_server, f"      password: {PASSWORD}\n")
    fp = server_fingerprint(project)
    import paramiko

    stale = paramiko.ECDSAKey.generate()
    host = f"[127.0.0.1]:{ssh_server['port']}"
    known = config.app_dir() / "known_hosts"
    known.write_text(f"{host} {stale.get_name()} {stale.get_base64()}\n")  # old server key
    configure(project, ssh_server, f"      password: {PASSWORD}\n", host_key=fp)
    with provider(project, trust=False) as p:
        assert p.host_fingerprint.endswith(fp)


# --- lost replies and dead connections (Phase 3A: C-8) ------------------------------------------
def drop_after(monkeypatch, p, method):
    """The next `method` call runs on the real server, then the connection dies before shipr
    sees the reply (as if the network failed at exactly that moment)."""
    real = getattr(paramiko.SFTPClient, method)
    done = []

    def lossy(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        if not done:
            done.append(args)
            p.client.get_transport().close()
            raise EOFError("connection dropped before the reply arrived")
        return result
    monkeypatch.setattr(paramiko.SFTPClient, method, lossy)
    return done


def backup_paths(remote):
    import posixpath

    parent, name = posixpath.split(remote)
    return f"{remote}/old.txt", f"{parent}/.shipr/{name}/backups/id/old.txt"


def test_lost_rename_reply_is_confirmed_by_hash(project, ssh_server, tmp_path, monkeypatch):
    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n")
    src, dst = backup_paths(remote)
    f = tmp_path / "f"
    f.write_text("content of a deleted file")
    with provider(project) as p:
        p.upload(f, src)
        dropped = drop_after(monkeypatch, p, "rename")
        p.rename(src, dst)  # used to fail: FileNotFoundError on the retry
        assert len(dropped) == 1
        assert p.stat(src) is None and p.read_text(dst) == "content of a deleted file"


def test_lost_rename_reply_without_sha256sum_is_ambiguous(project, sftp_only_server, tmp_path,
                                                          monkeypatch):
    from shipr.errors import AmbiguousOperation

    remote = configure(project, sftp_only_server, f"      password: {PASSWORD}\n")
    src, dst = backup_paths(remote)
    f = tmp_path / "f"
    f.write_text("x")
    with provider(project) as p:
        p.upload(f, src)
        drop_after(monkeypatch, p, "rename")
        with pytest.raises(AmbiguousOperation, match="can't run sha256sum"):
            p.rename(src, dst)
        assert p.stat(src) is None and p.read_text(dst) == "x"  # left exactly as found


def test_lost_delete_and_lock_release_replies(project, ssh_server, tmp_path, monkeypatch):
    from shipr.state import RemoteState

    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n")
    f = tmp_path / "f"
    f.write_text("x")
    with provider(project) as p:
        p.upload(f, f"{remote}/x.txt")
        drop_after(monkeypatch, p, "remove")
        p.delete(f"{remote}/x.txt")
        assert p.stat(f"{remote}/x.txt") is None
        monkeypatch.undo()
        state = RemoteState(p, load_env(project.repo)[1].effective_state_path, remote)
        state.acquire_lock("deploy-1")
        drop_after(monkeypatch, p, "remove")
        state.release_lock()  # used to raise on the retry
        assert state.read_lock() is None


def test_permanent_disconnect_mid_deploy_fails_safely(project, ssh_server, monkeypatch):
    from shipr import admin
    from shipr.errors import ConnectionLost, LockedError
    from shipr.providers.sftp import SFTPProvider

    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n")
    for i in range(5):
        project.write(f"f{i}.txt", f"v{i}")
    project.commit()
    with provider(project, trust=True):
        pass
    real_put, real_connect, puts = SFTPProvider._create_private, SFTPProvider._connect_once, []

    def put_then_die(sftp, path, *a, **k):  # creating the temp file of an upload
        if "/.shipr/" not in path:
            puts.append(path)
            if len(puts) == 3:  # the network goes away for good mid-deploy
                sftp.get_channel().get_transport().close()
                monkeypatch.setattr(SFTPProvider, "_connect_once",
                                    lambda prov: (_ for _ in ()).throw(OSError("unreachable")))
                raise EOFError("connection dropped")
        return real_put(sftp, path, *a, **k)
    monkeypatch.setattr(SFTPProvider, "_create_private", staticmethod(put_then_die))
    with pytest.raises(ConnectionLost):
        helpers.deploy(project.repo)
    monkeypatch.setattr(SFTPProvider, "_create_private", staticmethod(real_put))
    monkeypatch.setattr(SFTPProvider, "_connect_once", real_connect)

    env = load_env(project.repo)[1]
    with pytest.raises(LockedError) as e:  # the lock couldn't be released: never guessed stale
        helpers.deploy(project.repo)
    held = str(e.value).split("--id ")[1].split("`")[0]  # the message names the exact command
    assert admin.unlock(env, RecordingUI(), held).startswith("removed lock")
    ui = RecordingUI()
    rec = helpers.deploy(project.repo, ui)
    assert rec.status == "success" and "interrupted" in ui.text
    with provider(project) as p:
        assert {k: p.read_text(f"{remote}/{k}") for k in p.walk(remote)} == {
            f"f{i}.txt": f"v{i}" for i in range(5)}
    assert set(admin.status(env, "demo").manifest.files) == {f"f{i}.txt" for i in range(5)}


# --- remote command execution (Phase 3B: C-11, P-3) ---------------------------------------------
def run_with_watchdog(p, fn, seconds=60):
    """Run fn() in a thread; if it is still running after `seconds`, cut the connection and
    report a hang instead of hanging the test suite."""
    import threading

    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        p.client.close()
        t.join(10)
        pytest.fail(f"command still blocked after {seconds}s (deadlock)")
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_large_stderr_output_does_not_deadlock(project, ssh_server):
    """P-3: more than the 2 MiB SSH window on stderr while stdout is being read."""
    configure(project, ssh_server, f"      password: {PASSWORD}\n")
    with provider(project) as p:
        code, out, err = run_with_watchdog(p, lambda: p.exec(
            "head -c 6000000 /dev/zero | tr '\\0' e >&2; echo finished"))
    assert code == 0 and "finished" in out
    assert len(out) + len(err) > 6_000_000


def hook_config(timeout, command):
    import json

    return f"hooks:\n  timeout: {timeout}\n  remote_after: [{json.dumps(command)}]\n"


def test_remote_hook_timeout_then_next_deploy_reruns_hooks(project, ssh_server):
    import time

    from shipr.errors import CommandTimeout

    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n",
                       top=hook_config(2, "sleep 30"))
    project.write("a.txt", "a")
    project.commit()
    t0 = time.monotonic()
    with pytest.raises(CommandTimeout, match="may still be running on the server"):
        deploy(project)
    assert time.monotonic() - t0 < 20  # not the 30 s the command wanted
    cfg = project.repo / "shipr.yml"
    cfg.write_text(cfg.read_text().replace('"sleep 30"', '"echo ran > hook.txt"'))
    rec = deploy(project)  # the timed-out hook is pending: it runs again now
    assert rec.status == "success" and rec.hooks == "ok" and not rec.uploaded
    with provider(project) as p:
        assert p.read_text(f"{remote}/hook.txt").strip() == "ran"


def test_remote_hook_finishing_just_before_its_timeout(project, ssh_server):
    configure(project, ssh_server, f"      password: {PASSWORD}\n",
              top=hook_config(5, "sleep 3; echo done"))
    project.write("a.txt", "a")
    project.commit()
    ui = RecordingUI()
    rec = deploy(project, ui=ui)
    assert rec.status == "success" and "  done" in ui.lines


def test_remote_output_is_streamed_while_the_command_runs(project, ssh_server):
    import time

    configure(project, ssh_server, f"      password: {PASSWORD}\n")
    seen = []
    with provider(project) as p:
        p.exec("echo first; sleep 3; echo second >&2",
               on_output=lambda line: seen.append((line, time.monotonic())))
    assert [line for line, _ in seen] == ["first", "second"]  # stderr merged in order
    assert seen[1][1] - seen[0][1] > 2  # the first line arrived before the command ended


def test_dropped_connection_during_a_hook_is_a_clean_error(project, ssh_server, monkeypatch):
    from click.testing import CliRunner

    from shipr import hooks
    from shipr.cli import cli

    configure(project, ssh_server, f"      password: {PASSWORD}\n",
              top=hook_config(60, "echo hi"))
    project.write("a.txt", "a")
    project.commit()
    with provider(project, trust=True):
        pass
    real = hooks.run_remote

    def drop_first(provider_, *a, **k):
        provider_.client.get_transport().close()  # the network dies just as the hook starts
        return real(provider_, *a, **k)
    monkeypatch.setattr(hooks, "run_remote", drop_first)
    monkeypatch.chdir(project.repo)
    r = CliRunner().invoke(cli, ["deploy"])
    assert r.exit_code == 1 and "Traceback" not in r.output, r.output
    assert "could not run remote command" in r.output


def test_sha256sum_timeout_falls_back_to_downloading(project, ssh_server, tmp_path,
                                                     monkeypatch):
    import hashlib

    from shipr.providers import sftp as sftp_mod

    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n")
    f = tmp_path / "f"
    f.write_text("hash me")
    monkeypatch.setattr(sftp_mod, "SHA256_TIMEOUT", 1)
    with provider(project) as p:
        p.upload(f, f"{remote}/f.txt")
        real_exec, downloads = p.exec, []
        p.exec = lambda cmd, **k: real_exec(
            ("sleep 10; " + cmd) if cmd.startswith("sha256sum") else cmd, **k)
        real_download = p.download
        p.download = lambda r, loc: (downloads.append(r), real_download(r, loc))[1]
        got = p.sha256([f"{remote}/f.txt"])
    assert got == {f"{remote}/f.txt": hashlib.sha256(b"hash me").hexdigest()}
    assert downloads == [f"{remote}/f.txt"]


# --- shell capability probe (Phase 3C: audit C-13) ----------------------------------------------
def nothing_deployed(project, remote):
    import posixpath

    parent, name = posixpath.split(remote)
    with provider(project) as p:
        return p.walk(remote) == {} and p.stat(f"{parent}/.shipr/{name}/manifest.json") is None


def test_sftp_only_server_is_an_explicit_refusal(project, sftp_only_server):
    configure(project, sftp_only_server, f"      password: {PASSWORD}\n")
    with provider(project) as p:
        assert p.supports_exec() is False and p.exec_probe_error() is None


def test_probe_timeout_stops_before_any_change(project, ssh_server, monkeypatch):
    from shipr.errors import DeployError
    from shipr.providers import sftp as sftp_mod

    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n",
                       top="hooks:\n  remote_after: ['php artisan migrate']\n")
    project.write("a.txt", "a")
    project.commit()
    monkeypatch.setattr(sftp_mod, "PROBE_COMMAND", "sleep 30; echo shipr-ok")  # timeout: 5
    with pytest.raises(DeployError, match="could not tell whether"):
        deploy(project)
    assert nothing_deployed(project, remote)


def test_connection_failure_during_probe_stops_before_any_change(project, ssh_server,
                                                                 monkeypatch):
    from shipr.errors import DeployError

    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n",
                       top="hooks:\n  remote_after: ['echo migrated > hook.txt']\n")
    project.write("a.txt", "a")
    project.commit()
    real = SFTPProvider.supports_exec

    def drop_then_probe(self):
        self.client.get_transport().close()  # the network blips exactly during the probe
        return real(self)
    monkeypatch.setattr(SFTPProvider, "supports_exec", drop_then_probe)
    with pytest.raises(DeployError, match="could not tell whether"):
        deploy(project)
    monkeypatch.undo()
    assert nothing_deployed(project, remote)
    assert deploy(project).status == "success"  # a working probe: the deploy goes ahead


def test_ssh_exec_false_skips_hooks_deliberately(project, ssh_server):
    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n",
                       extra_env="    ssh_exec: false\n",
                       top="hooks:\n  remote_after: ['echo ran > hook.txt']\n")
    project.write("a.txt", "a")
    project.commit()
    ui = RecordingUI()
    rec = deploy(project, ui=ui)
    assert rec.status == "success" and rec.hooks == "skipped"
    assert "ssh_exec is false" in ui.text
    with provider(project) as p:
        assert p.stat(f"{remote}/hook.txt") is None


# --- deploy lock ownership (Phase 3E: C-19) -------------------------------------------------
def test_lock_release_after_lost_reply_keeps_a_newer_lock(project, ssh_server, monkeypatch):
    from shipr.providers.sftp import SFTPProvider
    from shipr.state import RemoteState

    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n")
    newer = '{"deploy_id": "deploy-B", "by": "b@ci", "since": "2026-11-21T10:00:00Z"}'
    with provider(project) as p:
        state = RemoteState(p, load_env(project.repo)[1].effective_state_path, remote)
        state.acquire_lock("deploy-A")
        lock = f"{state.root}/lock"
        real_connect = SFTPProvider._connect_once

        def reconnect_after_another_deploy_locked(prov):
            real_connect(prov)
            prov._sftp.open(lock, "wx").write(newer.encode())  # it found no lock, took it
        monkeypatch.setattr(SFTPProvider, "_connect_once", reconnect_after_another_deploy_locked)
        drop_after(monkeypatch, p, "remove")  # our remove happened; its reply was lost
        assert state.release_lock() == "changed"
        monkeypatch.setattr(SFTPProvider, "_connect_once", real_connect)
        assert p.read_text(lock) == newer  # the other deploy's lock was never deleted


def test_lock_takeover_and_unlock_over_sftp(project, ssh_server):
    from shipr.errors import Aborted, LockedError

    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n",
                       extra_env="    confirm: true\n")
    project.write("a.txt", "a")
    project.commit()
    env = load_env(project.repo)[1]
    lock = f"{env.effective_state_path}/lock"
    other = '{"deploy_id": "deploy-B", "by": "b@ci", "since": "2026-11-21T10:00:00Z"}'
    refused = []

    def at_prompt():  # a second deploy is refused; then someone forces the lock over
        with pytest.raises(LockedError) as e:
            helpers.deploy(project.repo, yes=True)
        refused.append(str(e.value))
        with provider(project) as p:
            p.delete(lock)
            assert p.create_exclusive(lock, other)
    from shipr.ui import UI
    with pytest.raises(LockedError, match="nothing was changed"):
        deploy(project, ui=UI(echo=lambda s: None, confirm=lambda m: (at_prompt(), True)[1]))
    assert "--id " in refused[0]
    with provider(project) as p:
        assert p.stat(f"{remote}/a.txt") is None and p.read_text(lock) == other
    with pytest.raises(LockedError, match="not by deploy deploy-A"):
        admin.unlock(env, RecordingUI(), "deploy-A")
    with pytest.raises(Aborted):
        admin.unlock(env, RecordingUI(answers=[False]))
    assert admin.unlock(env, RecordingUI(), "deploy-B").startswith("removed lock")
    assert deploy(project, yes=True).status == "success"


# --- state on another filesystem (Phase 3F: P-1) ------------------------------------------------
def device(server, path):
    r = subprocess.run(["docker", "exec", server["name"], "stat", "-c", "%d", path],
                       capture_output=True, text=True, env=DOCKER_ENV)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.parametrize("server_fixture, xfs", [("ssh_server", "/xfs"),
                                                 ("sftp_only_server", "/home/deploy/xfs")])
def test_deletion_with_state_on_another_filesystem(project, request, server_fixture, xfs):
    server = request.getfixturevalue(server_fixture)
    state = f"/xfs/state-{uuid.uuid4().hex[:6]}"  # (inside the chroot for the SFTP-only server)
    remote = configure(project, server, f"      password: {PASSWORD}\n",
                       extra_env=f"    state_path: {state}\n")
    in_container = server["root"] if server_fixture == "ssh_server" else "/home/deploy/upload"
    assert device(server, in_container) != device(server, xfs)  # really two filesystems
    project.write("keep.txt", "k")
    project.write("old/gone.txt", "bye")
    project.commit()
    assert deploy(project).status == "success"
    (project.repo / "old" / "gone.txt").unlink()
    project.commit()
    ui = RecordingUI()
    rec = deploy(project, ui=ui, yes=True)
    assert rec.status == "success" and not rec.failed, rec.failed  # was: partial, "Failure"
    assert "another filesystem" in ui.text
    with provider(project) as p:
        assert p.stat(f"{remote}/old/gone.txt") is None
        assert p.read_text(rec.deleted[0].backup) == "bye"


# --- temp file permissions (Phase 3F: P-5) -------------------------------------------------------
def watch_writes(monkeypatch):
    """Permission bits of every uploaded file at its first write, as the server reports them.
    Not the lock (opened "wx" by create_exclusive): it holds no deployment data."""
    seen = []
    real_write, real_open = paramiko.SFTPFile.write, paramiko.SFTPClient.open

    def open_(self, path, mode="r", *a, **k):
        f = real_open(self, path, mode, *a, **k)
        f.shipr_watched = "x" in mode  # the lock: not watched
        return f

    def write(self, data):
        if not getattr(self, "shipr_watched", False):
            self.shipr_watched = True
            seen.append(stat.S_IMODE(self.stat().st_mode))
        return real_write(self, data)
    monkeypatch.setattr(paramiko.SFTPClient, "open", open_)
    monkeypatch.setattr(paramiko.SFTPFile, "write", write)
    return seen


@pytest.mark.parametrize("server_fixture", ["ssh_server", "sftp_only_server"])
def test_uploads_are_private_from_their_first_byte(project, request, server_fixture,
                                                   monkeypatch):
    server = request.getfixturevalue(server_fixture)
    remote = configure(project, server, f"      password: {PASSWORD}\n")
    project.write("config.php", "<?php $pw = 'v1';")
    project.write("index.php", "<?php echo 1;")
    project.commit()
    assert deploy(project).status == "success"
    with provider(project) as p:
        p._sftp.chmod(f"{remote}/config.php", 0o600)  # the server's copy is private
        default = stat.S_IMODE(p._sftp.lstat(f"{load_env(project.repo)[1].effective_state_path}"
                                             "/manifest.json").st_mode)
    project.write("config.php", "<?php $pw = 'v2';")
    project.commit()
    seen = watch_writes(monkeypatch)
    assert deploy(project).status == "success"
    assert seen and all(m == 0o600 for m in seen), seen  # was 0644 (the umask)
    monkeypatch.undo()
    env = load_env(project.repo)[1]
    with provider(project) as p:
        mode = lambda path: stat.S_IMODE(p._sftp.lstat(path).st_mode)  # noqa: E731
        assert mode(f"{remote}/config.php") == 0o600
        assert mode(f"{remote}/index.php") == 0o644
        assert mode(f"{env.effective_state_path}/manifest.json") == default  # unchanged


# --- parallel content comparison when the server has no shell ----------------------------------
def upload_numbered(p, remote, tmp_path, n=24):
    import hashlib

    want = {}
    for i in range(n):
        f = tmp_path / f"f{i}"
        f.write_bytes(f"content {i} ".encode() * (i * 500 + 1))  # up to ~150 KB each
        p.upload(f, f"{remote}/d{i % 3}/f{i}.txt")
        want[f"{remote}/d{i % 3}/f{i}.txt"] = hashlib.sha256(f.read_bytes()).hexdigest()
    return want


@pytest.mark.parametrize("server_fixture", ["ssh_server", "sftp_only_server"])
def test_hashing_by_download_uses_several_channels(project, request, server_fixture, tmp_path,
                                                   monkeypatch):
    import threading

    from shipr.providers.sftp import SFTPProvider

    remote = configure(project, request.getfixturevalue(server_fixture),
                       f"      password: {PASSWORD}\n")
    threads = set()
    real = SFTPProvider._hash_with

    def hash_with(self, sftp, path):
        threads.add(threading.get_ident())
        return real(self, sftp, path)
    monkeypatch.setattr(SFTPProvider, "_hash_with", hash_with)
    with provider(project) as p:
        want = upload_numbered(p, remote, tmp_path)
        p.exec_allowed = False  # as on a server without a shell: hash by reading each file
        assert p.sha256(list(want)) == want
    assert len(threads) > 1  # read on several channels at once


def test_hashing_falls_back_when_the_server_refuses_more_channels(project, ssh_server, tmp_path,
                                                                 monkeypatch):
    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n")
    with provider(project) as p:
        want = upload_numbered(p, remote, tmp_path, n=6)
        p.exec_allowed = False
        monkeypatch.setattr(paramiko.SSHClient, "open_sftp", lambda self: (_ for _ in ()).throw(
            paramiko.ChannelException(1, "administratively prohibited")))
        assert p.sha256(list(want)) == want  # one file at a time, on the main channel


def test_parallel_hashing_never_reads_through_a_symlink(project, ssh_server, tmp_path):
    remote = configure(project, ssh_server, f"      password: {PASSWORD}\n")
    with provider(project) as p:
        want = upload_numbered(p, remote, tmp_path, n=3)
        p.exec(f"mkdir -p {remote}/../outside && echo secret > {remote}/../outside/f.txt && "
               f"ln -s ../outside {remote}/linked")
        p.exec_allowed = False
        with pytest.raises(UnsafePath):
            p.sha256([*want, f"{remote}/linked/f.txt"])


def test_first_full_deploy_over_an_existing_site_without_shell(project, sftp_only_server):
    configure(project, sftp_only_server, f"      password: {PASSWORD}\n")
    for i in range(30):
        project.write(f"d{i % 4}/f{i}.php", f"<?php {i};")
    project.commit()
    assert deploy(project).status == "success"
    env = load_env(project.repo)[1]
    with provider(project) as p:  # forget shipr's records: the site now looks hand-uploaded
        for name in p.walk(env.effective_state_path):
            p.delete(f"{env.effective_state_path}/{name}")
    ui = RecordingUI()
    rec = deploy(project, ui=ui)
    assert rec.status == "success" and not rec.uploaded  # all 30 compared and found identical
