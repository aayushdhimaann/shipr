"""P-2: the git export is extracted with tarfile's "data" filter, which Python only has from
3.10.12 / 3.11.4. Older Pythons must get a clear error, never an unfiltered extraction."""

import io
import tarfile

import pytest

from shipr import gitops
from shipr.errors import GitError


def make_tar(path, *members):
    with tarfile.open(path, "w") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def entry(name, data=b"x", **kw):
    info = tarfile.TarInfo(name)
    info.size = len(data) if data is not None else 0
    for k, v in kw.items():
        setattr(info, k, v)
    return info, data


@pytest.mark.parametrize("member", [
    entry("../escaped.txt"),
    entry("link", None, type=tarfile.SYMTYPE, linkname="/etc/passwd"),
    entry("up", None, type=tarfile.SYMTYPE, linkname="../../outside"),
    entry("hard", None, type=tarfile.LNKTYPE, linkname="../outside"),
    entry("dev", None, type=tarfile.CHRTYPE, devmajor=1, devminor=3),
])
def test_unsafe_archive_members_are_refused(tmp_path, member):
    if not hasattr(tarfile, "data_filter"):
        pytest.skip("this Python has no extraction filter (covered below)")
    archive = make_tar(tmp_path / "a.tar", entry("ok.txt"), member)
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(GitError):
        gitops.extract(archive, dest)
    assert not (tmp_path / "escaped.txt").exists() and not (tmp_path / "outside").exists()


def test_absolute_names_stay_inside_the_destination(tmp_path):
    if not hasattr(tarfile, "data_filter"):
        pytest.skip("this Python has no extraction filter (covered below)")
    dest = tmp_path / "out"
    dest.mkdir()
    gitops.extract(make_tar(tmp_path / "a.tar", entry(f"{tmp_path}/abs.txt")), dest)
    assert not (tmp_path / "abs.txt").exists()  # the filter strips the leading "/"
    assert (dest / str(tmp_path).lstrip("/") / "abs.txt").exists()


def test_safe_archive_is_extracted(tmp_path):
    if not hasattr(tarfile, "data_filter"):
        pytest.skip("this Python has no extraction filter (covered below)")
    archive = make_tar(tmp_path / "a.tar", entry("dir/ok.txt", b"hi"),
                       entry("dir/rel", None, type=tarfile.SYMTYPE, linkname="ok.txt"))
    dest = tmp_path / "out"
    dest.mkdir()
    gitops.extract(archive, dest)
    assert (dest / "dir" / "ok.txt").read_bytes() == b"hi"


@pytest.fixture
def old_python(monkeypatch):
    """Python 3.10.0-3.10.11 / 3.11.0-3.11.3: no data_filter, and extractall() has no
    `filter` argument (it raised TypeError: unexpected keyword argument 'filter')."""
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    calls = []

    def extractall(self, path=".", members=None, *, numeric_owner=False):
        calls.append(path)  # an unfiltered extraction: must never happen
    monkeypatch.setattr(tarfile.TarFile, "extractall", extractall)
    return calls


def test_old_python_gets_a_clear_error_and_no_unfiltered_extraction(tmp_path, old_python,
                                                                   monkeypatch):
    from tests.conftest import git

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("a")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "c")
    sha = gitops.resolve("HEAD", repo)
    with pytest.raises(GitError, match=r"3\.10\.12.*3\.11\.4"):  # a ShiprError: no traceback
        gitops.export(sha, repo, tmp_path / "out")
    assert old_python == []  # never extracted without the filter
