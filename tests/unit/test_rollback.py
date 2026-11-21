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
    # (prefixes of 1-3 digits are now refused: see the C-20 tests below)
    assert rollback_target([rec("abc123"), rec("def456")], "def456", "abc1").commit == "abc123"


def test_interrupted_deploy_rolls_back_to_the_last_completed_one():
    history = [rec("A"), rec("B")]
    assert rollback_target(history, "B", None, interrupted=True).commit == "B"


def test_records_whose_files_finished_count_even_if_hooks_failed():
    c = rec("C")
    c.status, c.files_done = "failed", True
    assert c.completed
    assert rollback_target([rec("A"), rec("B"), c], "C", None).commit == "B"


# --- C-20: --to never guesses -------------------------------------------------------------------
def test_ambiguous_commit_prefix_is_rejected_and_lists_the_candidates():
    history = [rec("abc1111"), rec("abc1222"), rec("def3333")]
    with pytest.raises(DeployError) as e:
        rollback_target(history, "def3333", "abc1")
    assert "abc1111" in str(e.value) and "abc1222" in str(e.value)


def test_exact_deploy_id_wins_over_a_commit_prefix():
    a = rec("1111aaa")
    a.id = "beef"
    history = [a, rec("beefcafe"), rec("2222bbb")]
    assert rollback_target(history, "2222bbb", "beef").commit == "1111aaa"


def test_one_commit_deployed_several_times_is_not_ambiguous():
    history = [rec("abc1111"), rec("def2222"), rec("abc1111", "rollback"), rec("fff3333")]
    assert rollback_target(history, "fff3333", "abc1").commit == "abc1111"


def test_commit_prefix_needs_four_characters_and_ignores_case():
    history = [rec("abc1111"), rec("def2222")]
    with pytest.raises(DeployError, match="at least 4"):
        rollback_target(history, "def2222", "a")
    assert rollback_target(history, "def2222", "ABC1").commit == "abc1111"


def test_unknown_target_is_an_error():
    with pytest.raises(DeployError, match="no completed deploy"):
        rollback_target([rec("abc1111"), rec("def2222")], "def2222", "9999")
