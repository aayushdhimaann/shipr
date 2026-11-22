"""P-1: when state_path is on another filesystem than remote_path, the server can't rename a
deleted file into the backup folder (OpenSSH answers EXDEV with a generic "Failure"). shipr
then copies it there, verifies the copy, and only then removes the original."""

import pytest

from shipr.errors import ConnectionLost
from shipr.executor import PlanExecutor
from shipr.models import DeployRecord, FileEntry, Manifest
from shipr.planner import Op, Plan
from shipr.providers.base import RemoteStat
from shipr.state import RemoteState
from shipr.ui import UI
from tests.fake_sftp import FakeServer, give_shell, lose_reply, make_provider, reconnecting

SITE, STATE = "/w", "/state/w"


@pytest.fixture
def srv():
    s = FakeServer()
    s.file("/w/old/gone.php", "<?php // removed from git", mode=0o640)
    s.mkdirs(STATE)
    s.mounts = ["/state"]  # a separate filesystem
    return s


def run_delete(p, rel="old/gone.php"):
    lines = []
    record = DeployRecord(id="d1", project="p", environment="e", commit="c", by="u", target="t",
                          started="s")
    manifest = Manifest(files={rel: FileEntry(sha256="x", size=1, mtime=1)})
    ex = PlanExecutor(p, RemoteState(p, STATE, SITE), manifest, record, {},
                      {rel: RemoteStat(1, 1)}, SITE, UI(echo=lines.append))
    ex.run(Plan(), [Op(rel, "deleted from repo")])
    return record, manifest, "\n".join(lines)


@pytest.mark.parametrize("shell", [True, False], ids=["sha256sum", "download-hash"])
def test_deletion_across_filesystems_is_copied_verified_then_removed(srv, shell):
    p = make_provider(srv, roots=(SITE, STATE))
    if shell:
        give_shell(p, srv)
    record, manifest, out = run_delete(p)
    backup = f"{STATE}/backups/d1/old/gone.php"
    assert not record.failed, record.failed  # used to fail: every deletion, forever
    assert [d.backup for d in record.deleted] == [backup]
    assert "/w/old/gone.php" not in srv.nodes and "old/gone.php" not in manifest.files
    assert srv.data(backup) == b"<?php // removed from git"
    assert srv.nodes[backup][2] == 0o640  # the backup keeps the original's mode
    assert "copied" in out


def test_original_kept_when_the_copy_does_not_match(srv):
    p = make_provider(srv, roots=(SITE, STATE))
    real_hash = p.sha256

    def changed_on_server(paths):  # someone edited the file while it was being copied
        srv.file("/w/old/gone.php", "<?php // edited on the server", mode=0o640)
        return real_hash(paths)
    p.sha256 = changed_on_server
    record, manifest, _ = run_delete(p)
    assert record.failed and not record.deleted and "old/gone.php" in manifest.files
    assert srv.data("/w/old/gone.php") == b"<?php // edited on the server"


def test_interrupted_copy_never_loses_the_original(srv):
    p = reconnecting(make_provider(srv, roots=(SITE, STATE)), retries=0)
    lose_reply(srv, "create_private", times=99, before=True)  # the copy never gets written
    with pytest.raises(ConnectionLost):
        run_delete(p)
    assert srv.data("/w/old/gone.php") == b"<?php // removed from git"


def test_existing_backup_is_never_overwritten(srv):
    srv.file(f"{STATE}/backups/d1/old/gone.php", "an older backup")
    record, _, _ = run_delete(make_provider(srv, roots=(SITE, STATE)))
    assert record.failed and srv.data("/w/old/gone.php") == b"<?php // removed from git"
    assert srv.data(f"{STATE}/backups/d1/old/gone.php") == b"an older backup"


def test_same_filesystem_still_uses_a_plain_rename(srv):
    srv.mounts = []
    p = make_provider(srv, roots=(SITE, STATE))
    record, _, out = run_delete(p)
    assert record.deleted and not record.failed and "copied" not in out
    assert ("create", f"{STATE}/backups/d1/old/.gone.php.shipr-tmp") not in srv.calls


# --- Phase 3G: only a cross-filesystem failure falls back to copying ----------------------------
import errno  # noqa: E402

from shipr.errors import AmbiguousOperation, ProviderError, UnsafePath  # noqa: E402

NOT_CROSS_FS = [
    PermissionError(errno.EACCES, "Permission denied"),  # what paramiko raises for FX_PERMISSION
    OSError(errno.EACCES, "Permission denied"),  # becomes PermissionError too
    FileNotFoundError(errno.ENOENT, "No such file"),
    IsADirectoryError(errno.EISDIR, "Is a directory"),
    OSError(errno.EIO, "Input/output error"),  # a plain OSError, but not EXDEV
    OSError(errno.ENOSPC, "No space left on device"),
    ProviderError("dst already exists"),
    UnsafePath("refusing: a symlink"),
    AmbiguousOperation("could not confirm whether moving ... completed"),
]


@pytest.mark.parametrize("error", NOT_CROSS_FS, ids=lambda e: type(e).__name__ + str(
    getattr(e, "errno", "") or ""))
def test_other_rename_failures_never_fall_back_to_copying(srv, error):
    p = make_provider(srv, roots=(SITE, STATE))
    copies = []
    p.rename = lambda src, dst: (_ for _ in ()).throw(error)
    p.copy = lambda src, dst: copies.append(dst)
    record, manifest, _ = run_delete(p)
    assert not copies and not record.deleted and len(record.failed) == 1
    assert "old/gone.php" in manifest.files
    assert srv.data("/w/old/gone.php") == b"<?php // removed from git"


def test_lost_connection_during_rename_stops_without_copying(srv):
    p = make_provider(srv, roots=(SITE, STATE))
    copies = []
    p.rename = lambda src, dst: (_ for _ in ()).throw(ConnectionLost("gone"))
    p.copy = lambda src, dst: copies.append(dst)
    with pytest.raises(ConnectionLost):
        run_delete(p)
    assert not copies and srv.data("/w/old/gone.php") == b"<?php // removed from git"


@pytest.mark.parametrize("error", [OSError("Failure"), OSError(errno.EXDEV, "cross-device"),
                                   OSError("Operation unsupported")],
                         ids=["sftp-failure", "exdev", "sftp-unsupported"])
def test_cross_filesystem_failures_fall_back(srv, error):
    p = make_provider(srv, roots=(SITE, STATE))
    p.rename = lambda src, dst: (_ for _ in ()).throw(error)
    record, _, _ = run_delete(p)
    assert record.deleted and not record.failed
