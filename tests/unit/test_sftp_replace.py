"""The non-atomic rename fallback must never lose the destination file."""

import pytest

from shipr.providers.sftp import SFTPProvider


class FakeSFTP:
    def __init__(self, files, fail_move=None):
        self.files = dict(files)
        self.fail_move = fail_move  # (src, dst) pair that fails

    def posix_rename(self, src, dst):
        raise OSError("operation unsupported")

    def stat(self, p):
        if p not in self.files:
            raise FileNotFoundError(p)

    def rename(self, src, dst):
        if src not in self.files:
            raise FileNotFoundError(src)
        if dst in self.files or (src, dst) == self.fail_move:
            raise OSError("failure")
        self.files[dst] = self.files.pop(src)

    def remove(self, p):
        del self.files[p]


def test_fallback_swaps_and_cleans_up():
    s = FakeSFTP({"tmp": "new", "dst": "old"})
    SFTPProvider._replace(s, "tmp", "dst")
    assert s.files == {"dst": "new"}


def test_fallback_restores_dst_when_swap_fails():
    s = FakeSFTP({"tmp": "new", "dst": "old"}, fail_move=("tmp", "dst"))
    with pytest.raises(OSError):
        SFTPProvider._replace(s, "tmp", "dst")
    assert s.files == {"dst": "old", "tmp": "new"}


def test_real_failure_is_not_treated_as_unsupported():
    s = FakeSFTP({"dst": "old"})  # tmp vanished
    with pytest.raises(FileNotFoundError):
        SFTPProvider._replace(s, "tmp", "dst")
    assert s.files == {"dst": "old"}
