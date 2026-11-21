"""C-8: an operation whose reply was lost may already have happened on the server. A retry
must reconcile instead of repeating it blindly, and never guess (fake server, no network)."""

import pytest

from shipr.errors import AmbiguousOperation, ConnectionLost, UnsafePath
from shipr.state import RemoteState
from tests.fake_sftp import FakeServer, give_shell, lose_reply, make_provider, reconnecting

SRC, DST = "/w/old/gone.txt", "/w/.shipr/backups/id/old/gone.txt"


@pytest.fixture
def srv():
    s = FakeServer()
    s.file(SRC, "deleted file content")
    s.mkdirs("/elsewhere")
    return s


def shell_provider(srv):
    p = reconnecting(make_provider(srv))
    give_shell(p, srv)
    return p


# --- rename (delete-to-backup) ------------------------------------------------------------------
def test_rename_whose_reply_was_lost_is_confirmed_by_hash(srv):
    calls = lose_reply(srv, "rename")
    shell_provider(srv).rename(SRC, DST)
    assert len(calls) == 1  # never repeated
    assert SRC not in srv.nodes and srv.data(DST) == b"deleted file content"


def test_rename_lost_before_it_reached_the_server_is_simply_redone(srv):
    calls = lose_reply(srv, "rename", before=True)
    shell_provider(srv).rename(SRC, DST)
    assert len(calls) == 2 and srv.data(DST) == b"deleted file content"


def test_same_size_but_different_content_is_ambiguous_not_success(srv):
    def swap_content(src, dst):  # someone replaced the moved file with same-size content
        srv._move(src, dst, overwrite=False)
        srv.nodes[DST] = ("file", b"DELETED FILE CONTENT", 0o644)
        raise EOFError("reply lost")
    srv.rename = swap_content
    with pytest.raises(AmbiguousOperation, match="could not confirm"):
        shell_provider(srv).rename(SRC, DST)
    assert srv.data(DST) == b"DELETED FILE CONTENT"  # left exactly as found


def test_without_a_remote_hash_the_outcome_is_ambiguous(srv):
    lose_reply(srv, "rename")
    p = reconnecting(make_provider(srv))
    p._exec_ok = False  # SFTP-only server: no sha256sum
    with pytest.raises(AmbiguousOperation, match="could not confirm"):
        p.rename(SRC, DST)
    assert SRC not in srv.nodes and srv.data(DST) == b"deleted file content"  # untouched


@pytest.mark.parametrize("state", ["both", "neither"])
def test_both_or_neither_existing_is_ambiguous_and_untouched(srv, state):
    def odd(src, dst):
        if state == "both":
            srv.file(DST, "deleted file content")
        else:
            del srv.nodes[SRC]
        raise EOFError("reply lost")
    srv.rename = odd
    with pytest.raises(AmbiguousOperation):
        shell_provider(srv).rename(SRC, DST)
    assert (SRC in srv.nodes) == (state == "both")


def test_reconciliation_keeps_the_symlink_guard(srv):
    def move_then_swap(src, dst):
        srv._move(src, dst, overwrite=False)
        srv.swap_for_link("/w/.shipr/backups/id/old", "/elsewhere/old")
        raise EOFError("reply lost")
    srv.rename = move_then_swap
    with pytest.raises(UnsafePath, match="symlink"):
        shell_provider(srv).rename(SRC, DST)


# --- delete and lock release ----------------------------------------------------------------------
def test_delete_whose_reply_was_lost_succeeds(srv):
    calls = lose_reply(srv, "remove")
    shell_provider(srv).delete(SRC)
    assert len(calls) == 1 and SRC not in srv.nodes


def test_deleting_a_missing_file_is_still_an_error_on_the_first_attempt(srv):
    with pytest.raises(FileNotFoundError):
        shell_provider(srv).delete("/w/never-there.txt")


def test_lock_release_with_lost_reply_is_clean(srv):
    p = shell_provider(srv)
    state = RemoteState(p, "/w/.shipr", "/w")
    state.acquire_lock("deploy-1")
    lose_reply(srv, "remove")
    state.release_lock("deploy-1")  # used to raise: FileNotFoundError on the retry
    assert state.read_lock() is None


# --- a connection that never comes back ------------------------------------------------------
def test_permanent_disconnect_fails_safely_without_repeating(srv):
    calls = lose_reply(srv, "rename", times=99)
    p = shell_provider(srv)

    def dead():
        p._sftp = None  # every reconnect fails
    p._connect_once = dead
    with pytest.raises(ConnectionLost):
        p.rename(SRC, DST)
    assert len(calls) == 1  # one attempt reached the server; nothing was repeated
    assert srv.data(DST) == b"deleted file content"
