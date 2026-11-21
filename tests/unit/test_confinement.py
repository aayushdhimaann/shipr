"""Provider.confined(): component-aware containment of remote paths in the configured roots."""

import os

import pytest

from shipr.errors import UnsafePath
from shipr.providers.base import is_within, leftover_of, park_name
from shipr.providers.local import LocalProvider


def prov(*roots):
    p = LocalProvider({})
    p.roots = roots
    return p


@pytest.mark.parametrize("path, dirs", [
    ("/var/www/app/index.php", []),
    ("/var/www/app/a/b/c.txt", ["/var/www/app/a", "/var/www/app/a/b"]),
    ("/var/www/app", []),  # the root itself
    # state dir inside the web root: checked from the outermost root, so .shipr is checked too
    ("/var/www/app/.shipr/lock", ["/var/www/app/.shipr"]),
    ("/srv/state/history/x.json", ["/srv/state/history"]),
])
def test_dirs_between_outermost_root_and_path(path, dirs):
    assert prov("/var/www/app", "/var/www/app/.shipr", "/srv/state").confined(path) == dirs


@pytest.mark.parametrize("bad", [
    "/var/www/app/../etc/passwd", "/var/www/app/./x", "/var/www/app//x", "//var/www/app/x",
    "var/www/app/x", "", "/var/www/app/x/", "/var/www/app/a\\b", "/var/www/app/a\0b",
])
def test_non_canonical_paths_are_refused_not_normalised(bad):
    with pytest.raises(UnsafePath):
        prov("/var/www/app").confined(bad)


def test_similar_prefix_is_not_inside():
    with pytest.raises(UnsafePath, match="outside"):
        prov("/var/www/app").confined("/var/www/application/x")


def test_root_slash_equal_and_duplicate_roots():
    assert prov("/").confined("/a/b/c") == ["/a", "/a/b"]
    assert prov("/w", "/w").confined("/w/a/b") == ["/w/a"]


def test_no_roots_means_no_confinement():
    assert prov().confined("/anything/at/all") == []


def test_is_within_is_component_aware():
    assert is_within("/var/www/app", "/var/www/app")
    assert is_within("/var/www/app/x", "/var/www/app")
    assert not is_within("/var/www/application", "/var/www/app")
    assert not is_within("/var/www", "/var/www/app")


@pytest.mark.parametrize("name, expected", [
    ("d/.a.txt.shipr-tmp", ("tmp", "d/a.txt")),
    ("d/.a.txt.0123abcd.shipr-old", ("park", "d/a.txt")),
    ("d/a.txt.shipr-old", ("park", "d/a.txt")),  # legacy name
    ("a.txt.shipr-old.bak", None),
    (".shipr-tmp", None),
    ("notes.txt", None),
])
def test_leftover_names(name, expected):
    assert leftover_of(name) == expected


def test_park_names_are_unique_and_recognised():
    a, b = park_name("/w/x.php"), park_name("/w/x.php")
    assert a != b and leftover_of(a) == ("park", "/w/x.php")


def test_local_paths_stay_inside_the_local_root(tmp_path):
    p = LocalProvider({"root": str(tmp_path)})
    p.connect()
    with pytest.raises(UnsafePath):
        p._p("/a\\..\\..\\x")
    if os.name == "nt":
        with pytest.raises(UnsafePath):
            p._p("/C:/Windows/x")
    assert p._p("/site/a.txt") == tmp_path.resolve() / "site" / "a.txt"
