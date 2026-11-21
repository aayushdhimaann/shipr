"""P-5: a file being uploaded is private (0600) from the moment it is created until it is
complete; only then does it get its final mode. Checked on the local provider (POSIX) and the
fake SFTP server; the real OpenSSH checks are in tests/integration."""

import os
import stat
import sys

import pytest

from shipr.providers.local import LocalProvider
from tests.fake_sftp import FakeServer, make_provider

posix_only = pytest.mark.skipif(sys.platform == "win32",
                                reason="Windows has no POSIX permission bits (ACLs instead)")


@pytest.fixture
def src(tmp_path):
    f = tmp_path / "secret.php"
    f.write_text("<?php $db_password = 'hunter2';")
    return f


def watch_local_tmp(monkeypatch):
    """Permission bits of each temp file at the moment it is fully written (just before shipr
    sets its final mode, or replaces the target)."""
    import shipr.providers.local as local_mod

    seen = []
    real_chmod, real_replace = local_mod.os.chmod, local_mod.os.replace

    def chmod(path, mode, *a, **k):
        seen.append(("before chmod", stat.S_IMODE(os.lstat(path).st_mode)))
        return real_chmod(path, mode, *a, **k)

    def replace(src, dst, *a, **k):
        seen.append(("before replace", stat.S_IMODE(os.lstat(src).st_mode)))
        return real_replace(src, dst, *a, **k)
    monkeypatch.setattr(local_mod.os, "chmod", chmod)
    monkeypatch.setattr(local_mod.os, "replace", replace)
    return seen


@posix_only
def test_local_temp_file_is_private_until_complete(tmp_path, src, monkeypatch):
    (tmp_path / "srv" / "w").mkdir(parents=True)
    seen = watch_local_tmp(monkeypatch)
    with LocalProvider({"root": str(tmp_path / "srv")}) as p:
        p.upload(src, "/w/config.php", 0o600)
    assert seen[0] == ("before chmod", 0o600), seen  # was the umask default (0644)
    assert stat.S_IMODE((tmp_path / "srv" / "w" / "config.php").stat().st_mode) == 0o600


@posix_only
def test_local_final_modes_are_unchanged(tmp_path, src, monkeypatch):
    """Only the temp file changes: final files keep the requested mode, or the server's
    default (the umask) when no mode is given, as before."""
    (tmp_path / "srv" / "w").mkdir(parents=True)
    umask = os.umask(0o022)
    try:
        with LocalProvider({"root": str(tmp_path / "srv")}) as p:
            p.upload(src, "/w/a.sh", 0o755)
            p.write_text("/w/manifest.json", "{}")
    finally:
        os.umask(umask)
    w = tmp_path / "srv" / "w"
    assert stat.S_IMODE((w / "a.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((w / "manifest.json").stat().st_mode) == 0o644


@posix_only
def test_local_temp_file_left_by_a_crash_is_recreated_privately(tmp_path, src, monkeypatch):
    w = tmp_path / "srv" / "w"
    w.mkdir(parents=True)
    leftover = w / ".config.php.shipr-tmp"
    leftover.write_text("partial")
    leftover.chmod(0o666)  # created before, with loose permissions
    seen = watch_local_tmp(monkeypatch)
    with LocalProvider({"root": str(tmp_path / "srv")}) as p:
        p.upload(src, "/w/config.php", 0o600)
    assert seen[0] == ("before chmod", 0o600), seen


def test_local_temp_files_live_in_private_directories(tmp_path):
    """Downloads (hashing, backups) and manifest writes go through a TemporaryDirectory, which
    is created 0700 on POSIX and inside the user's own profile on Windows."""
    w = tmp_path / "w"
    w.mkdir()
    (w / "f").write_text("x")
    with LocalProvider({"root": str(tmp_path)}) as p, p.local_copy("/w/f") as f:
        if sys.platform != "win32":
            assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700


def test_sftp_temp_file_is_created_private_before_any_byte(tmp_path, src):
    srv = FakeServer()
    srv.file("/w/config.php", "old", mode=0o600)
    p = make_provider(srv)
    p.upload(src, "/w/config.php", 0o600)
    assert srv.modes_at_write and all(m == 0o600 for _, m in srv.modes_at_write), \
        srv.modes_at_write  # put() wrote into a file created with the umask default (0644)
    assert srv.nodes["/w/config.php"][2] == 0o600
    p.upload(src, "/w/app.js", 0o644)
    assert srv.nodes["/w/app.js"][2] == 0o644  # final mode only once complete


def test_sftp_state_files_keep_the_servers_default_mode(tmp_path):
    srv = FakeServer()
    srv.mkdirs("/w/.shipr")
    p = make_provider(srv)
    p.write_text("/w/.shipr/manifest.json", "{}")
    assert all(m == 0o600 for _, m in srv.modes_at_write)
    assert srv.nodes["/w/.shipr/manifest.json"][2] == 0o644  # the fake's umask default
