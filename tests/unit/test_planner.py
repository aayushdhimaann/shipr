from pathlib import Path

import pytest

from shipr.config import Files
from shipr.errors import UnsafePath
from shipr.files import LocalFile, Rules
from shipr.models import FileEntry
from shipr.planner import make_plan, safe_join
from shipr.providers import RemoteStat


def lf(sha, size=1):
    return LocalFile(Path("x"), sha, size)


def m(sha, size=1, mtime=100):
    return FileEntry(sha256=sha, size=size, mtime=mtime)


def rs(size=1, mtime=100):
    return RemoteStat(size, mtime)


def plan(local, manifest, remote, hashes=None, **files):
    return make_plan(local, manifest, remote, Rules(Files(**files)),
                     (lambda rels: hashes or {}))


def reasons(p):
    return {o.path: (o.reason, o.backup) for o in p.uploads}


def test_decision_table():
    p = plan(
        local={"same": lf("a"), "changed": lf("b"), "hotfix": lf("a"), "gone": lf("a"),
               "new": lf("a"), "untracked_diff": lf("a", 5), "untracked_same": lf("a"),
               "untracked_samesize": lf("a")},
        manifest={"same": m("a"), "changed": m("a"), "hotfix": m("a"), "gone": m("a"),
                  "removed": m("a"), "removed_gone": m("a")},
        remote={"same": rs(), "changed": rs(), "hotfix": rs(mtime=999), "untracked_diff": rs(9),
                "untracked_same": rs(), "untracked_samesize": rs(), "removed": rs(),
                "not_ours": rs()},
        hashes={"untracked_same": "a", "untracked_samesize": "zzz"},
    )
    assert reasons(p) == {
        "changed": ("changed", False),
        "hotfix": ("changed on server since last deploy", True),
        "gone": ("missing on server", False),
        "new": ("new", False),
        "untracked_diff": ("differs from server copy", True),
        "untracked_samesize": ("differs from server copy", True),
    }
    assert p.adopt == {"untracked_same": "a"}
    assert [o.path for o in p.deletes] == ["removed"]  # not_ours is never touched
    assert p.forget == ["removed_gone"]
    assert p.unchanged == 1


def test_protected_files_never_overwritten_or_deleted():
    p = plan({"config.php": lf("new"), "fresh.php": lf("a")}, {"old.php": m("a")},
             {"config.php": rs(), "old.php": rs()}, protect=["config.php", "old.php"])
    assert reasons(p) == {"fresh.php": ("new", False)}
    assert not p.deletes and sorted(p.protected) == ["config.php", "old.php"]


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", "a/../../b", "a//b", "a\\b",
                                 "", ".", "a/./b"])
def test_safe_join_rejects_escapes(bad):
    with pytest.raises(UnsafePath):
        safe_join("/var/www", bad)


def test_safe_join_ok():
    assert safe_join("/var/www", "a/b.php") == "/var/www/a/b.php"


def test_paths_are_validated_before_any_decision():
    with pytest.raises(UnsafePath):  # tampered manifest on the server
        plan({"a": lf("a")}, {"../../etc/passwd": m("a")}, {})
    with pytest.raises(UnsafePath):  # odd local file name
        plan({"a\\b": lf("a")}, {}, {})


def test_local_files_cannot_overwrite_state_dir():
    with pytest.raises(UnsafePath, match="state dir"):
        make_plan({".shipr/manifest.json": lf("a")}, {}, {}, Rules(Files()), reserved=".shipr")


# --- remote symlinks, special files and unreadable dirs are never gone through (C-1, C-2) ------
def opaque(kind):
    return RemoteStat(0, 0, kind=kind)


def test_paths_at_or_under_opaque_server_entries_are_blocked():
    p = plan(
        local={"storage/data.txt": lf("a"), "config.php": lf("a"), "cache/a/b.txt": lf("a"),
               "fifo": lf("a"), "ok.txt": lf("a")},
        manifest={"storage/old.txt": m("a"), "gone/x.txt": m("a")},
        remote={"storage": opaque("symlink"), "config.php": opaque("symlink"),
                "cache": opaque("unreadable"), "fifo": opaque("special"),
                "gone": opaque("symlink")},
        protect=["config.php"],
    )
    assert reasons(p) == {"ok.txt": ("new", False)}
    assert not p.deletes and not p.forget and not p.protected
    assert dict(p.blocked) == {
        "cache/a/b.txt": "server has an unreadable directory at cache",
        "config.php": "server path is a symlink",
        "fifo": "server path is a special file",
        "gone/x.txt": "server has a symlink at gone",
        "storage/data.txt": "server has a symlink at storage",
        "storage/old.txt": "server has a symlink at storage",
    }


def test_nested_symlink_deep_in_the_tree_blocks_only_its_subtree():
    p = plan({"a/b/c/d.txt": lf("a"), "a/b/e.txt": lf("a")}, {},
             {"a/b/c": opaque("symlink")})
    assert [r for r, _ in p.blocked] == ["a/b/c/d.txt"]
    assert reasons(p) == {"a/b/e.txt": ("new", False)}


def test_opaque_entries_are_not_adopted_or_hashed():
    asked = []
    make_plan({"x": lf("a")}, {}, {"x": opaque("symlink")}, Rules(Files()),
              lambda rels: asked.extend(rels) or {})
    assert asked == []


# --- recovering an interrupted deploy (C-22) ---------------------------------------------------
def test_recovery_hash_checks_stat_mismatches_before_calling_them_hotfixes():
    p = make_plan(
        {"ours": lf("new"), "touched": lf("new"), "touched_same": lf("a"), "hotfix": lf("new")},
        {"ours": m("old"), "touched": m("old"), "touched_same": m("a"), "hotfix": m("old")},
        {r: rs(mtime=999) for r in ("ours", "touched", "touched_same", "hotfix")},
        Rules(Files()),
        lambda rels: {"ours": "new", "touched": "old", "touched_same": "a", "hotfix": "edited"},
        recovering=True)
    assert reasons(p) == {"touched": ("changed", False),
                          "hotfix": ("changed on server since last deploy", True)}
    assert p.adopt == {"ours": "new", "touched_same": "a"}


def test_without_recovery_stat_mismatch_is_still_a_hotfix():
    p = plan({"x": lf("new")}, {"x": m("old")}, {"x": rs(mtime=999)}, hashes={"x": "new"})
    assert reasons(p) == {"x": ("changed on server since last deploy", True)}
