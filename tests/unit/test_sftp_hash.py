"""C-16: remote sha256sum output is only trusted when it is exactly what was asked for;
anything else falls back to downloading and hashing locally."""

import hashlib

import pytest

from shipr.providers.sftp import SFTPProvider

REAL = {"/w/a.php": b"aaa", "/w/b.php": b"bbb"}


def h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def provider(stdout: str, code: int = 0):
    p = SFTPProvider({"host": "h", "username": "u"})
    p._exec_ok = True
    p.commands = []

    def exec_(cmd, cwd=None, timeout=None):
        p.commands.append(cmd)
        return code, stdout, ""
    p.exec = exec_
    p.downloads = []

    def download(remote, local):
        p.downloads.append(remote)
        local.write_bytes(REAL[remote])
    p.download = download
    return p


def good_output(paths):
    return "".join(f"{h(REAL[x])}  {x}\n" for x in paths)


def test_exact_output_is_used_without_downloading():
    p = provider(good_output(["/w/a.php", "/w/b.php"]))
    assert p.sha256(["/w/a.php", "/w/b.php"]) == {x: h(REAL[x]) for x in REAL}
    assert p.downloads == []


@pytest.mark.parametrize("stdout, code", [
    # BusyBox doesn't escape newlines: a crafted name forges another file's line
    (good_output(["/w/a.php", "/w/b.php"]) + f"{'f' * 64}  /w/b.php\n", 0),
    (f"{'f' * 64}  /w/b.php\n" + good_output(["/w/a.php"]), 0),  # out of order / injected
    (good_output(["/w/a.php"]), 0),  # truncated
    (good_output(["/w/a.php", "/w/b.php"]), 1),  # non-zero exit: some file unreadable
    ("\\" + good_output(["/w/a.php", "/w/b.php"]), 0),  # GNU-escaped name
    (f"{'z' * 64}  /w/a.php\n{h(b'bbb')}  /w/b.php\n", 0),  # not hex
    ("", 127),  # no sha256sum
])
def test_anything_unexpected_falls_back_to_downloading(stdout, code):
    p = provider(stdout, code)
    assert p.sha256(["/w/a.php", "/w/b.php"]) == {x: h(REAL[x]) for x in REAL}
    assert sorted(p.downloads) == ["/w/a.php", "/w/b.php"]


def test_names_that_sha256sum_would_escape_are_never_sent_to_it():
    REAL["/w/new\nline.php"] = b"n"
    try:
        p = provider(good_output(["/w/a.php"]))
        got = p.sha256(["/w/a.php", "/w/new\nline.php"])
        assert got["/w/new\nline.php"] == h(b"n") and p.downloads == ["/w/new\nline.php"]
        assert "new" not in p.commands[0]
    finally:
        del REAL["/w/new\nline.php"]
