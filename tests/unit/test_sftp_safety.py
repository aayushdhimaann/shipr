"""SFTPProvider never writes, reads or deletes through a symlink below the deploy/state roots,
and checks again immediately before every mutating request (fake server, no network)."""

import pytest

from shipr.errors import ProviderError, UnsafePath
from tests.fake_sftp import FakeServer, make_provider


@pytest.fixture
def srv():
    s = FakeServer()
    s.mkdirs("/w")
    s.mkdirs("/elsewhere")
    return s


@pytest.fixture
def src(tmp_path):
    f = tmp_path / "f"
    f.write_text("new")
    return f


# --- walk (C-1, C-2, C-7) ---------------------------------------------------------------------
def test_walk_reports_symlinks_special_and_unreadable_as_opaque(srv):
    srv.file("/w/a.txt", "a")
    srv.link("/w/storage", "/elsewhere")
    srv.file("/elsewhere/secret.txt", "s")
    srv.link("/w/config.php", "/elsewhere/secret.txt")
    srv.nodes["/w/fifo"] = ("special",)
    srv.mkdirs("/w/cache/sub")
    srv.unreadable.add("/w/cache")
    got = make_provider(srv).walk("/w")
    assert got["a.txt"].kind == "file"
    assert {k: v.kind for k, v in got.items() if v.kind != "file"} == {
        "storage": "symlink", "config.php": "symlink", "fifo": "special", "cache": "unreadable"}
    assert "storage/secret.txt" not in got  # never descended


def test_walk_unreadable_root_is_a_clean_error(srv):
    srv.unreadable.add("/w")
    with pytest.raises(ProviderError, match="cannot list /w"):
        make_provider(srv).walk("/w")


def test_walk_missing_root_is_empty(srv):
    assert make_provider(srv).walk("/w/nothing-here") == {}


# --- write-time guard (C-1, C-2) ---------------------------------------------------------------
def test_upload_refuses_symlinked_parent(srv, src):
    srv.link("/w/storage", "/elsewhere")
    with pytest.raises(UnsafePath, match="/w/storage is a symlink"):
        make_provider(srv).upload(src, "/w/storage/data.txt")
    assert "/elsewhere/data.txt" not in srv.nodes


def test_upload_refuses_symlink_target(srv, src):
    srv.file("/elsewhere/real.php", "REAL")
    srv.link("/w/config.php", "/elsewhere/real.php")
    with pytest.raises(UnsafePath, match="config.php: it is a symlink"):
        make_provider(srv).upload(src, "/w/config.php")
    assert srv.nodes["/w/config.php"] == ("link", "/elsewhere/real.php")
    assert srv.data("/elsewhere/real.php") == b"REAL"


def test_symlink_at_tmp_name_is_unlinked_not_followed(srv, src):
    srv.file("/elsewhere/victim", "keep")
    srv.link("/w/.a.txt.shipr-tmp", "/elsewhere/victim")
    make_provider(srv).upload(src, "/w/a.txt")
    assert srv.data("/w/a.txt") == b"new"
    assert srv.data("/elsewhere/victim") == b"keep"


def test_directory_swapped_for_symlink_right_after_creation_is_caught(srv, src):
    def swap(path):
        if path == "/w/new":
            srv.nodes.pop("/w/new", None)
            srv.link("/w/new", "/elsewhere")
    # mkdir runs, then the attacker swaps it before shipr's re-check
    orig = srv.mkdir

    def mkdir_then_swap(path):
        orig(path)
        swap(path)
    srv.mkdir = mkdir_then_swap
    with pytest.raises(UnsafePath, match="/w/new is a symlink"):
        make_provider(srv).upload(src, "/w/new/f.txt")
    assert "/elsewhere/f.txt" not in srv.nodes


def test_parent_swapped_between_put_and_rename_is_caught(srv, src):
    srv.mkdirs("/w/assets")

    def swap_after_put(path):
        if path.endswith(".shipr-tmp"):
            srv.swap_for_link("/w/assets", "/elsewhere/assets")
    srv.before["chmod"] = swap_after_put  # runs after put, before the final rename
    with pytest.raises(UnsafePath, match="/w/assets is a symlink"):
        make_provider(srv).upload(src, "/w/assets/a.css", mode=0o644)
    assert "/elsewhere/assets/a.css" not in srv.nodes


def test_parent_swapped_before_delete_to_backup_rename_is_caught(srv):
    srv.file("/w/old/gone.txt", "g")
    done = []

    def swap_while_backup_dirs_are_made(path):  # after the source was checked once
        if path == "/w/.shipr/backups/id/old" and not done:
            done.append(path)
            srv.swap_for_link("/w/old", "/elsewhere/old")
    srv.before["lstat"] = swap_while_backup_dirs_are_made
    with pytest.raises(UnsafePath, match="/w/old is a symlink"):
        make_provider(srv).rename("/w/old/gone.txt", "/w/.shipr/backups/id/old/gone.txt")
    assert srv.data("/elsewhere/old/gone.txt") == b"g"  # not moved


@pytest.mark.parametrize("op", ["delete", "rmdir"])
def test_parent_swapped_before_remove_is_caught(srv, op):
    srv.file("/w/d/f.txt", "f")
    srv.mkdirs("/w/d/empty")
    srv.swap_for_link("/w/d", "/elsewhere/d")
    p = make_provider(srv)
    if op == "delete":
        with pytest.raises(UnsafePath):
            p.delete("/w/d/f.txt")
        assert srv.data("/elsewhere/d/f.txt") == b"f"
    else:
        assert p.rmdir_if_empty("/w/d/empty") is False
        assert "/elsewhere/d/empty" in srv.nodes


def test_every_mutating_request_is_preceded_by_a_fresh_check(srv, src):
    srv.mkdirs("/w/a/b")
    p = make_provider(srv)
    p.upload(src, "/w/a/b/f.txt")
    p.upload(src, "/w/a/b/g.txt")  # no cache: the second upload checks again
    for mut in ("put", "posix_rename"):
        idxs = [i for i, (op, _) in enumerate(srv.calls) if op == mut]
        assert len(idxs) == 2
        for i in idxs:
            checked = {path for op, path in srv.calls[max(0, i - 6):i] if op == "lstat"}
            assert {"/w/a", "/w/a/b"} <= checked


def test_reads_refuse_symlinks(srv):
    srv.file("/elsewhere/manifest.json", "{}")
    srv.mkdirs("/w/.shipr")
    srv.link("/w/.shipr/manifest.json", "/elsewhere/manifest.json")
    p = make_provider(srv)
    assert p.stat("/w/.shipr/manifest.json") is None
    with pytest.raises(UnsafePath):
        p.read_text("/w/.shipr/manifest.json")


def test_symlinked_state_dir_is_refused(srv):
    srv.link("/w/.shipr", "/elsewhere")
    with pytest.raises(UnsafePath, match="/w/.shipr is a symlink"):
        make_provider(srv).create_exclusive("/w/.shipr/lock", "x")
    assert "/elsewhere/lock" not in srv.nodes


def test_path_outside_roots_is_refused(srv, src):
    with pytest.raises(UnsafePath, match="outside"):
        make_provider(srv).upload(src, "/etc/passwd")


def test_no_roots_keeps_old_behaviour(srv, src):
    p = make_provider(srv, roots=())
    p.upload(src, "/w/x/y.txt")
    assert srv.data("/w/x/y.txt") == b"new"


# --- park files (C-21) ----------------------------------------------------------------------
def test_swap_with_leftover_legacy_park_succeeds_and_leaves_it(src):
    s = FakeServer(posix_rename=False)
    s.file("/w/a.txt", "old")
    s.file("/w/a.txt.shipr-old", "older, from a crash")
    make_provider(s).upload(src, "/w/a.txt")
    assert s.data("/w/a.txt") == b"new"
    assert s.data("/w/a.txt.shipr-old") == b"older, from a crash"
    assert not [k for k in s.nodes if k.endswith(".shipr-tmp")]


def test_interrupted_swap_leaves_park_and_next_upload_restores_dst(src):
    s = FakeServer(posix_rename=False)
    s.file("/w/a.txt", "old")

    def die_on_swap(path):
        if path.endswith(".shipr-tmp"):
            raise KeyboardInterrupt  # process killed after parking, before the swap
    s.before["rename"] = die_on_swap
    with pytest.raises(KeyboardInterrupt):
        make_provider(s).upload(src, "/w/a.txt")
    parks = [k for k in s.nodes if k.endswith(".shipr-old")]
    assert "/w/a.txt" not in s.nodes and len(parks) == 1 and s.data(parks[0]) == b"old"
    del s.before["rename"]
    make_provider(s).upload(src, "/w/a.txt")
    assert s.data("/w/a.txt") == b"new"
    assert s.data(parks[0]) == b"old"  # judged by the engine, never deleted here


def test_failed_restore_keeps_park_and_names_it(src):
    s = FakeServer(posix_rename=False)
    s.file("/w/a.txt", "old")
    real_move = s._move

    def move(a, b, overwrite):
        if a.endswith(".shipr-tmp") or a.endswith(".shipr-old"):
            raise OSError("Failure")  # neither the swap nor the restore works
        real_move(a, b, overwrite)
    s._move = move
    kept_at = r"previous content is kept at /w/\.a\.txt\.[0-9a-f]{8}\.shipr-old"
    with pytest.raises(ProviderError, match=kept_at):
        make_provider(s).upload(src, "/w/a.txt")
    assert [s.data(k) for k in s.nodes if k.endswith(".shipr-old")] == [b"old"]
