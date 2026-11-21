import pytest

from shipr.engine import rollback_target
from shipr.errors import DeployError
from shipr.models import DeployRecord


def rec(commit, kind="deploy"):
    return DeployRecord(id=f"id-{commit}-{kind}", kind=kind, project="p", environment="e",
                        commit=commit, by="u", target="t", started="s", status="success")


def test_previous_commit():
    assert rollback_target([rec("A"), rec("B")], "B", None).commit == "A"


def test_repeated_rollback_keeps_going_back():
    history = [rec("A"), rec("B"), rec("C"), rec("B", "rollback")]
    assert rollback_target(history, "B", None).commit == "A"


def test_nothing_older():
    with pytest.raises(DeployError):
        rollback_target([rec("A"), rec("B"), rec("A", "rollback")], "A", None)


def test_explicit_target_by_commit_prefix():
    assert rollback_target([rec("abc123"), rec("def456")], "def456", "abc").commit == "abc123"


def test_interrupted_deploy_rolls_back_to_the_last_completed_one():
    history = [rec("A"), rec("B")]
    assert rollback_target(history, "B", None, interrupted=True).commit == "B"


def test_records_whose_files_finished_count_even_if_hooks_failed():
    c = rec("C")
    c.status, c.files_done = "failed", True
    assert c.completed
    assert rollback_target([rec("A"), rec("B"), c], "C", None).commit == "B"
