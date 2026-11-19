import os

import pytest

from shipr.config import Files
from shipr.files import Rules, collect


def tree(root, files):
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rel)


def test_default_excludes_keep_secrets_out(tmp_path):
    tree(tmp_path, ["index.php", ".env", ".env.production", "keys/server.pem", "app.log",
                    "node_modules/x/i.js", "shipr.yml", "src/a.js", "deep/node_modules/y.js"])
    got = collect(tmp_path, Rules(Files()))
    assert set(got) == {"index.php", "src/a.js"}
    assert got["index.php"].size == len("index.php")


def test_include_exclude_and_opt_out_of_defaults(tmp_path):
    tree(tmp_path, ["dist/a.js", "dist/a.js.map", "src/b.js", ".env"])
    got = collect(tmp_path, Rules(Files(include=["dist/"], exclude=["*.map"])))
    assert set(got) == {"dist/a.js"}
    got = collect(tmp_path, Rules(Files(default_excludes=False)))
    assert ".env" in got


def test_protect(tmp_path):
    r = Rules(Files(protect=["uploads/**", "config.php"]))
    assert r.protected("uploads/a/b.png") and r.protected("config.php")
    assert not r.protected("src/config.php.dist")


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_outside_tree_skipped(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("s")
    root = tmp_path / "root"
    tree(root, ["a.txt"])
    (root / "link.txt").symlink_to(outside)
    (root / "ok.txt").symlink_to(root / "a.txt")
    warnings = []
    got = collect(root, Rules(Files()), warn=warnings.append)
    assert set(got) == {"a.txt", "ok.txt"} and warnings
