"""C-19: a deploy lock is only ever removed after checking it is exactly the one meant."""

import json

import pytest

from shipr import state as state_mod
from shipr.providers.local import LocalProvider
from shipr.state import RemoteState
from tests.fake_sftp import FakeServer, lose_reply, make_provider, reconnecting

LOCK = "/w/.shipr/lock"


def other_lock(deploy_id="deploy-B"):
    return json.dumps({"by": "b@ci", "since": "2026-11-21T10:00:00Z", "deploy_id": deploy_id,
                       "token": "f" * 16})


@pytest.fixture
def local(tmp_path):
    with LocalProvider({"root": str(tmp_path)}) as p:
        yield p, RemoteState(p, "/w/.shipr", "/w"), tmp_path / "w" / ".shipr" / "lock"


def test_two_locks_for_the_same_deploy_id_are_never_identical(local, tmp_path):
    _, st, path = local
    st.acquire_lock("deploy-A")
    first = path.read_text()
    path.unlink()
    RemoteState(st.provider, "/w/.shipr", "/w").acquire_lock("deploy-A")
    assert path.read_text() != first  # a random token: equal text means "this very lock"


def test_release_removes_only_this_deploys_own_lock(local):
    _, st, path = local
    st.acquire_lock("deploy-A")
    assert st.release_lock() == "deleted" and not path.exists()


def test_release_never_removes_a_lock_taken_over_by_another_deploy(local):
    _, st, path = local
    st.acquire_lock("deploy-1")
    path.write_text(other_lock("deploy-10"))  # `shipr unlock`, then another deploy started
    assert st.release_lock() == "changed"  # (an id check by substring matched "deploy-1")
    assert json.loads(path.read_text())["deploy_id"] == "deploy-10"


def test_release_reports_a_lock_removed_by_someone_else(local):
    _, st, path = local
    st.acquire_lock("deploy-A")
    path.unlink()
    assert st.release_lock() == "missing"


def test_release_after_a_lost_reply_never_deletes_a_newer_lock():
    srv = FakeServer()
    srv.mkdirs("/w/.shipr")
    p = reconnecting(make_provider(srv))
    st = RemoteState(p, "/w/.shipr", "/w")
    st.acquire_lock("deploy-A")
    lose_reply(srv, "remove")  # our lock is gone, but the reply never arrived
    p._connect_once = lambda: srv.file(LOCK, other_lock())  # meanwhile a new deploy locks
    # shipr can't tell this from a takeover, so it reports what it sees, and never deletes it
    assert st.release_lock() == "changed"
    assert json.loads(srv.data(LOCK))["deploy_id"] == "deploy-B"  # theirs is untouched


def test_release_retry_after_a_request_that_never_arrived_still_removes_ours():
    srv = FakeServer()
    srv.mkdirs("/w/.shipr")
    p = reconnecting(make_provider(srv))
    st = RemoteState(p, "/w/.shipr", "/w")
    st.acquire_lock("deploy-A")
    calls = lose_reply(srv, "remove", before=True)
    assert st.release_lock() == "deleted" and LOCK not in srv.nodes and len(calls) == 2


def test_lock_description_names_holder_and_age():
    info = state_mod.LockInfo.parse(other_lock())
    text = info.describe(now="2026-11-21T13:05:00Z")
    assert text == "deploy deploy-B by b@ci, since 2026-11-21T10:00:00Z (3h 5m ago)"
    assert "unrecognised" in state_mod.LockInfo.parse("garbage").describe()
    assert state_mod.LockInfo.parse("garbage").deploy_id is None


def test_locked_error_names_the_holder_and_the_exact_remedy(local):
    _, st, path = local
    path.parent.mkdir(parents=True)
    path.write_text(other_lock())
    with pytest.raises(state_mod.LockedError) as e:
        RemoteState(st.provider, "/w/.shipr", "/w", env_name="prod").acquire_lock("deploy-A")
    assert "deploy deploy-B by b@ci" in str(e.value)
    assert "shipr unlock -e prod --id deploy-B" in str(e.value)
