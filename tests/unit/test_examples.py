"""Every example config must stay valid."""

from pathlib import Path

import pytest

from shipr import config

EXAMPLES = sorted((Path(__file__).parents[2] / "examples").glob("*/shipr.yml"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.parent.name)
def test_example_is_valid(path, monkeypatch):
    for var in ("SHIPR_PROD_PASSWORD", "SHIPR_KEY_PASSPHRASE", "SHIPR_PASSWORD"):
        monkeypatch.setenv(var, "x")
    cfg = config.load(path)
    for name in cfg.environments:
        cfg.env(name)


def test_examples_exist():
    assert len(EXAMPLES) == 5
