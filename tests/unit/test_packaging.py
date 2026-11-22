"""The declared Python support must match what the code needs: tarfile's extraction filters
(3.10.12+, 3.11.4+, 3.12+), which the git export relies on (P-2)."""

import sys
import tarfile
from importlib.metadata import entry_points, metadata, version

from packaging.specifiers import SpecifierSet

import shipr

WITHOUT_FILTER = [f"3.10.{i}" for i in range(12)] + [f"3.11.{i}" for i in range(4)]
WITH_FILTER = ["3.10.12", "3.10.22", "3.11.4", "3.11.18", "3.12.0", "3.13.0", "3.14.0"]


def test_requires_python_matches_the_extraction_filter_requirement():
    spec = SpecifierSet(metadata("shipr")["Requires-Python"])
    assert [v for v in WITHOUT_FILTER if v in spec] == []
    assert [v for v in WITH_FILTER if v not in spec] == []
    assert sys.version.split()[0] in spec and hasattr(tarfile, "data_filter")


def test_version_has_one_source():
    assert version("shipr") == shipr.__version__


def test_entry_points_are_registered():
    assert {e.value for e in entry_points(group="console_scripts") if e.name == "shipr"} == \
        {"shipr.cli:main"}
    assert {e.name: e.value for e in entry_points(group="shipr.providers")
            if e.value.startswith("shipr.")} == {
        "sftp": "shipr.providers.sftp:SFTPProvider",
        "local": "shipr.providers.local:LocalProvider"}
