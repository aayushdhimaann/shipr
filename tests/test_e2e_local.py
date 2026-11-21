"""End-to-end deploys through the real engine into a local folder acting as the server."""

import json
import os
import sys
from pathlib import Path

import pytest

from shipr import admin, engine
from shipr.errors import Aborted, DeployError, LockedError
from tests.helpers import RecordingUI, deploy, load_env


def run(project, ui=None, **opts):
    return deploy(project.repo, ui, **opts)


def manifest(project):
    return json.loads((project.state / "manifest.json").read_text())


def test_full_then_incremental(project):
    project.write("index.php", "v1")
    project.write("css/app.css", "body{}")
    project.write(".env", "SECRET=1")
    sha = project.commit()
    rec = run(project)
    assert rec.status == "success"
    assert project.remote_files() == {"index.php", "css/app.css"}  # .env never uploaded
    assert manifest(project)["deployed_commit"] == sha
    assert not (project.site / ".shipr").exists()  # records live outside the web root

    project.write("index.php", "v2")
    project.commit()
    rec = run(project)
    assert [u.path for u in rec.uploaded] == ["index.php"]
    assert (project.site / "index.php").read_text() == "v2"

    rec = run(project)
    assert rec.status == "up-to-date"


def test_dry_run_changes_nothing(project):
    project.write("a.txt", "a")
    project.commit()
    ui = RecordingUI()
    rec = run(project, ui=ui, dry_run=True)
    assert rec.status == "dry-run" and "upload  a.txt" in ui.text
    assert not project.site.exists()


def test_uncommitted_changes_not_deployed(project):
    project.write("a.txt", "committed")
    project.commit()
    project.write("a.txt", "dirty")
    ui = RecordingUI()
    run(project, ui=ui)
    assert (project.site / "a.txt").read_text() == "committed"
    assert "uncommitted" in ui.text


def test_hotfix_is_backed_up_then_overwritten(project):
    project.write("a.php", "v1")
    project.commit()
    run(project)
    (project.site / "a.php").write_text("hotfix on server!")
    rec = run(project)  # same commit, but server drifted -> restore with backup
    up = rec.uploaded[0]
    assert up.reason == "changed on server since last deploy" and up.backup
    assert (project.site / "a.php").read_text() == "v1"
    backup = project.server / up.backup.lstrip("/")
    assert backup.read_text() == "hotfix on server!"


def test_deletion_asks_and_respects_no(project):
    project.write("keep.txt", "k")
    project.write("old/gone.txt", "g")
    project.commit()
    run(project)
    (project.repo / "old" / "gone.txt").unlink()
    project.commit()

    ui = RecordingUI(answers=[False])
    rec = run(project, ui=ui)
    assert "old/gone.txt" in ui.prompts[0] and rec.kept == ["old/gone.txt"]
    assert (project.site / "old" / "gone.txt").exists()

    rec = run(project, ui=RecordingUI(answers=[True]))  # asked again next time
    assert [d.path for d in rec.deleted] == ["old/gone.txt"]
    assert not (project.site / "old").exists()  # emptied dir removed
    assert (project.server / rec.deleted[0].backup.lstrip("/")).read_text() == "g"


def test_rename_and_untracked_remote_files_untouched(project):
    project.write("a.txt", "a")
    project.commit()
    project.site.mkdir(parents=True)
    (project.site / "uploads").mkdir()
    (project.site / "uploads" / "photo.jpg").write_text("user data")
    run(project)
    project.repo.joinpath("a.txt").rename(project.repo / "b.txt")
    project.commit()
    ui = RecordingUI()
    rec = run(project, ui=ui, yes=True)
    assert "renamed to b.txt" in ui.text
    assert project.remote_files() == {"b.txt", "uploads/photo.jpg"}
    assert [d.path for d in rec.deleted] == ["a.txt"]


def test_first_deploy_over_existing_site_adopts_identical_files(project):
    project.write("same.txt", "same")
    project.write("diff.txt", "new!")
    project.commit()
    project.site.mkdir(parents=True)
    (project.site / "same.txt").write_text("same")
    (project.site / "diff.txt").write_text("old!")  # same size, different content
    rec = run(project)
    assert [u.path for u in rec.uploaded] == ["diff.txt"]
    assert rec.uploaded[0].backup
    assert set(manifest(project)["files"]) == {"same.txt", "diff.txt"}


def test_lock_blocks_concurrent_deploy(project):
    project.write("a.txt", "a")
    project.commit()
    project.state.mkdir(parents=True)
    (project.state / "lock").write_text('{"by": "someone"}')
    with pytest.raises(LockedError, match="someone"):
        run(project)


def test_interrupted_deploy_recovers(project, monkeypatch):
    for i in range(30):
        project.write(f"f{i:02}.txt", str(i))
    project.commit()
    from shipr.providers.local import LocalProvider

    real = LocalProvider.upload
    calls = {"n": 0}

    def flaky(self, local, remote, mode=None):
        if "/.shipr/" not in remote:  # count site files only, not shipr's own records
            calls["n"] += 1
            if calls["n"] == 27:
                raise KeyboardInterrupt
        return real(self, local, remote, mode)

    monkeypatch.setattr(LocalProvider, "upload", flaky)
    with pytest.raises(KeyboardInterrupt):
        run(project)
    m = manifest(project)
    assert m.get("in_progress") and len(m["files"]) == 26  # progress saved
    assert not (project.state / "lock").exists()  # lock released

    monkeypatch.setattr(LocalProvider, "upload", real)
    ui = RecordingUI()
    rec = run(project, ui=ui)
    assert "interrupted" in ui.text and rec.status == "success"
    assert len(rec.uploaded) == 4 and len(project.remote_files()) == 30
    assert "in_progress" not in manifest(project)


def test_partial_failure_reported_and_rerun_converges(project, monkeypatch):
    project.write("good.txt", "g")
    project.write("bad.txt", "b")
    project.commit()
    from shipr.providers.local import LocalProvider

    real = LocalProvider.upload

    def deny(self, local, remote, mode=None):
        if remote.endswith("bad.txt"):
            raise PermissionError("denied")
        return real(self, local, remote, mode)

    monkeypatch.setattr(LocalProvider, "upload", deny)
    rec = run(project)
    assert rec.status == "partial" and rec.failed[0].path == "bad.txt"
    monkeypatch.setattr(LocalProvider, "upload", real)
    rec = run(project)
    assert [u.path for u in rec.uploaded] == ["bad.txt"]


def test_build_output_and_hooks(project):
    script = project.repo / "build.py"
    script.write_text("import os\nos.makedirs('dist', exist_ok=True)\n"
                      "open('dist/app.js', 'w').write(os.environ['SHIPR_ENV'])\n")
    project.write_config(
        f"build:\n  commands: ['\"{Path(sys.executable).as_posix()}\" build.py']\n"
        "  output: dist\n"
        "hooks:\n  remote_after: [\"echo done > after.txt\"]\n")
    project.write("src/app.src", "source")
    project.commit()
    run(project)
    assert (project.site / "app.js").read_text() == "prod"
    assert (project.site / "after.txt").exists()  # remote hook ran in remote_path


def test_rollback_restores_previous_commit(project):
    project.write("a.txt", "v1")
    project.commit()
    run(project)
    project.write("a.txt", "v2")
    project.write("new.txt", "n")
    project.commit()
    run(project)
    cfg, env = load_env(project.repo)
    rec = engine.rollback(cfg, env, options=engine.DeployOptions(yes=True), ui=RecordingUI())
    assert rec.kind == "rollback"
    assert (project.site / "a.txt").read_text() == "v1"
    assert project.remote_files() == {"a.txt"}
    hist = admin.history(env)
    assert [h.kind for h in hist] == ["deploy", "deploy", "rollback"]


def test_production_confirmation(project):
    project.write_config()
    (project.repo / "shipr.yml").write_text(
        (project.repo / "shipr.yml").read_text() + "    confirm: true\n")
    project.write("a.txt", "a")
    project.commit()
    with pytest.raises(Aborted):
        run(project, ui=RecordingUI(answers=[False]))
    assert not (project.site / "a.txt").exists()
    assert run(project, yes=True).status == "success"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_permissions_new_exec_kept_and_backup_private(project):
    import os
    import stat

    project.write("run.sh", "#!/bin/sh\n")
    os.chmod(project.repo / "run.sh", 0o755)
    project.write("config.php", "v1")
    project.commit()
    run(project)
    assert stat.S_IMODE((project.site / "run.sh").stat().st_mode) == 0o755
    os.chmod(project.site / "config.php", 0o600)
    (project.site / "config.php").write_text("hotfix")
    rec = run(project)
    backup = project.server / rec.uploaded[0].backup.lstrip("/")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE((project.site / "config.php").stat().st_mode) == 0o600


def test_local_hooks_see_dotenv_without_interpolation(project):
    (project.repo / ".env").write_text("SHIPR_TEST_SECRET=from-dotenv\n")
    script = project.repo / "b.py"
    script.write_text("import os\nos.makedirs('out', exist_ok=True)\n"
                      "open('out/v.txt', 'w').write(os.environ['SHIPR_TEST_SECRET'])\n")
    project.write_config(
        f"build:\n  commands: ['\"{Path(sys.executable).as_posix()}\" b.py']\n"
        "  output: out\n")
    project.commit()
    ui = RecordingUI()
    run(project, ui=ui)
    assert (project.site / "v.txt").read_text() == "from-dotenv"
    assert "from-dotenv" not in ui.text  # command echoed un-expanded


def test_lock_release_only_removes_own_lock(project):
    from shipr.providers.local import LocalProvider
    from shipr.state import RemoteState

    with LocalProvider({"root": str(project.server)}) as p:
        st = RemoteState(p, "/site/.shipr", "/site")
        st.acquire_lock("deploy-1")
        st.release_lock("deploy-2")  # someone else's id: must not remove it
        assert st.read_lock() and "deploy-1" in st.read_lock()
        st.release_lock("deploy-1")
        assert st.read_lock() is None


# --- filesystem safety (C-1, C-2, C-7, C-21) ----------------------------------------------------
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks/permissions")


def add_env_option(project, line):
    cfg = project.repo / "shipr.yml"
    cfg.write_text(cfg.read_text() + f"    {line}\n")


@posix_only
def test_symlinked_server_dir_is_never_written_through(project):
    project.write_config("files:\n  protect: ['storage/**']\n")
    shared = project.server / "shared"
    shared.mkdir()
    (shared / "data.txt").write_text("PRODUCTION USER DATA")
    project.site.mkdir(parents=True)
    (project.site / "storage").symlink_to(shared)
    project.write("storage/data.txt", "placeholder from git")
    project.write("storage/deep/new.txt", "n")
    project.write("index.php", "x")
    project.commit()
    ui = RecordingUI()
    rec = run(project, ui=ui)
    assert (shared / "data.txt").read_text() == "PRODUCTION USER DATA"
    assert not (shared / "deep").exists()
    assert [u.path for u in rec.uploaded] == ["index.php"]
    assert rec.blocked == ["storage/data.txt", "storage/deep/new.txt"]
    assert "skip    storage/data.txt  (server has a symlink at storage" in ui.text


@posix_only
def test_symlinked_server_file_is_never_replaced(project):
    project.write_config("files:\n  protect: ['config.php']\n")
    real = project.server / "shared.php"
    real.write_text("REAL DB PASSWORD")
    project.site.mkdir(parents=True)
    (project.site / "config.php").symlink_to(real)
    project.write("config.php", "sample config")
    project.commit()
    rec = run(project)
    assert (project.site / "config.php").is_symlink()
    assert real.read_text() == "REAL DB PASSWORD" and rec.blocked == ["config.php"]


@posix_only
def test_nested_and_inward_symlinks_are_left_alone(project):
    project.site.mkdir(parents=True)
    (project.site / "a" / "b").mkdir(parents=True)
    (project.site / "real").mkdir()
    (project.site / "a" / "b" / "c").symlink_to(project.site / "real")  # points back inside
    project.write("a/b/c/x.txt", "x")
    project.write("a/b/y.txt", "y")
    project.commit()
    rec = run(project)
    assert rec.blocked == ["a/b/c/x.txt"] and [u.path for u in rec.uploaded] == ["a/b/y.txt"]
    assert not (project.site / "real" / "x.txt").exists()


class SwapUI(RecordingUI):
    """Says yes, but first replaces a server directory with a symlink (a race after planning)."""

    def __init__(self, swap):
        super().__init__()
        self.swap = swap

    def _confirm(self, msg):
        self.swap()
        return True


def swap_dir_for_link(directory: Path, target: Path) -> None:
    directory.rename(target)
    directory.symlink_to(target)


@posix_only
def test_symlink_swapped_in_after_planning_blocks_the_upload(project):
    project.write("assets/a.css", "v1")
    project.commit()
    run(project)
    project.write("assets/a.css", "v2")
    project.commit()
    add_env_option(project, "confirm: true")
    moved = project.server / "moved-assets"
    rec = run(project, ui=SwapUI(lambda: swap_dir_for_link(project.site / "assets", moved)))
    assert rec.status == "partial" and rec.failed[0].path == "assets/a.css"
    assert "symlink" in rec.failed[0].error
    assert (moved / "a.css").read_text() == "v1"  # nothing written through the link


@posix_only
def test_symlink_swapped_in_before_deletion_blocks_it(project):
    project.write("keep.txt", "k")
    project.write("old/gone.txt", "g")
    project.commit()
    run(project)
    (project.repo / "old" / "gone.txt").unlink()
    project.commit()
    moved = project.server / "moved-old"
    rec = run(project, ui=SwapUI(lambda: swap_dir_for_link(project.site / "old", moved)))
    assert rec.status == "partial" and rec.failed[0].path == "old/gone.txt"
    assert (moved / "gone.txt").read_text() == "g"


unreadable_ok = pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                                   reason="needs POSIX permissions and a non-root user")


@unreadable_ok
def test_unreadable_server_dir_does_not_break_the_deploy(project):
    project.site.mkdir(parents=True)
    locked = project.site / "cache"
    locked.mkdir()
    (locked / "sess").write_text("s")
    project.write("cache/x.txt", "x")
    project.write("index.php", "i")
    project.commit()
    locked.chmod(0)
    try:
        rec = run(project)
    finally:
        locked.chmod(0o755)
    assert rec.status == "success" and [u.path for u in rec.uploaded] == ["index.php"]
    assert rec.blocked == ["cache/x.txt"]


def park(project, rel, text):
    d, name = os.path.split(rel)
    p = project.site / d / f".{name}.0123abcd.shipr-old"
    p.write_text(text)
    return p


def test_unmanaged_leftover_lookalikes_are_never_deleted(project):
    project.write("a.txt", "a")
    project.commit()
    (project.site / "uploads").mkdir(parents=True)
    keep = [project.site / name for name in (
        "uploads/report.shipr-old", ".notes.shipr-tmp", "uploads/.x.0123abcd.shipr-old",
        "a.txt.shipr-old.bak")]
    for f in keep:
        f.write_text("user data")
    run(project)
    assert all(f.read_text() == "user data" for f in keep)


def test_interrupted_swap_is_recovered_and_verified_park_removed(project):
    project.write("a.txt", "v1")
    project.commit()
    run(project)
    p = park(project, "a.txt", "v1")  # crash after parking: a.txt missing, park holds v1
    (project.site / "a.txt").unlink()
    (project.site / ".a.txt.shipr-tmp").write_text("partial")
    project.write("a.txt", "v2")
    project.commit()
    rec = run(project)
    assert rec.status == "success" and (project.site / "a.txt").read_text() == "v2"
    assert not p.exists() and not (project.site / ".a.txt.shipr-tmp").exists()
    # the stale temp file was reused (and renamed into place) by the new upload of a.txt
    assert rec.cleaned == [".a.txt.0123abcd.shipr-old"]


@pytest.mark.parametrize("dst_state", ["missing", "modified"])
def test_unproven_park_stops_the_deploy_before_any_change(project, dst_state):
    project.write("a.txt", "v1")
    project.commit()
    run(project)
    p = park(project, "a.txt", "server-side edit nobody backed up")
    if dst_state == "missing":
        (project.site / "a.txt").unlink()
    else:
        (project.site / "a.txt").write_text("manually modified")
    project.write("a.txt", "v2")
    project.commit()
    before = manifest(project)
    with pytest.raises(DeployError, match="cannot prove"):
        run(project)
    assert p.read_text() == "server-side edit nobody backed up"
    assert manifest(project) == before
    assert not (project.state / "lock").exists()


def test_park_matching_a_backup_is_removed(project):
    project.write("a.txt", "v1")
    project.commit()
    run(project)
    (project.site / "a.txt").write_text("hotfix!")  # drift: backed up by the next deploy
    rec = run(project)
    assert rec.uploaded[0].backup
    p = park(project, "a.txt", "hotfix!")
    project.write("b.txt", "b")
    project.commit()
    rec = run(project)
    assert not p.exists() and rec.cleaned == [".a.txt.0123abcd.shipr-old"]


def test_legacy_park_name_matching_manifest_is_removed(project):
    project.write("a.txt", "v1")
    project.commit()
    run(project)
    legacy = project.site / "a.txt.shipr-old"
    legacy.write_text("v1")
    project.write("b.txt", "b")
    project.commit()
    run(project)
    assert not legacy.exists()


# --- state location and consistency (Phase 2B: C-3, C-5, C-6, C-9, C-22, C-23) ------------------
from shipr.errors import StateError  # noqa: E402
from shipr.state import RemoteState as _RemoteState  # noqa: E402


def history_records(project):
    return [json.loads(p.read_text()) for p in sorted((project.state / "history").iterdir())]


def test_default_records_live_beside_the_web_root(project):
    project.write("a.txt", "a")
    project.commit()
    ui = RecordingUI()
    run(project, ui=ui)
    assert (project.state / "manifest.json").exists()
    assert not (project.site / ".shipr").exists() and "inside the web root" not in ui.text


def test_legacy_in_root_records_keep_working_with_a_warning(project):
    project.write("a.txt", "a")
    project.commit()
    add_env_option(project, "state_path: /site/.shipr")  # what 0.1 did by default
    run(project)
    project.write_config()  # back to the default: no state_path
    project.write("a.txt", "a2")
    project.commit()
    ui = RecordingUI()
    rec = run(project, ui=ui)
    assert [u.path for u in rec.uploaded] == ["a.txt"]  # incremental: old records were used
    assert (project.site / ".shipr" / "manifest.json").exists() and not project.state.exists()
    assert "inside the web root" in ui.text and "/.shipr/site" in ui.text  # where to move them


def test_explicit_in_root_state_warns_every_deploy(project):
    project.write("a.txt", "a")
    project.commit()
    add_env_option(project, "state_path: /site/.shipr")
    for _ in range(2):
        project.write("a.txt", project.repo.joinpath("a.txt").read_text() + "!")
        project.commit()
        ui = RecordingUI()
        run(project, ui=ui)
        assert "inside the web root" in ui.text


def test_uncreatable_default_state_dir_stops_before_any_change(project):
    project.write("a.txt", "a")
    project.commit()
    (project.server / ".shipr").write_text("not a directory")  # deploy user can't create it
    with pytest.raises(StateError, match="state_path"):
        run(project)
    assert not project.site.exists()


def deploy_a_b_then_c_with_failing_hook(project):
    project.write("v.txt", "A")
    project.commit()
    run(project)
    project.write("v.txt", "B")
    b = project.commit()
    run(project)
    project.write_config("hooks:\n  remote_after: ['exit 1']\n")
    project.write("v.txt", "C")
    c = project.commit()
    with pytest.raises(DeployError, match="remote command failed"):
        run(project)
    return b, c


def test_hook_failure_keeps_records_truthful_and_rollback_correct(project):
    """C-6: before, the manifest kept B while the server had C, and rollback skipped to A."""
    b, c = deploy_a_b_then_c_with_failing_hook(project)
    m = manifest(project)
    assert (project.site / "v.txt").read_text() == "C"
    assert m["deployed_commit"] == c and "in_progress" not in m and m["hooks_pending"]
    last = history_records(project)[-1]
    assert (last["status"], last["files_done"], last["hooks"]) == ("failed", True, "failed")
    project.write_config()  # drop the broken hook (not committed: rollback uses this config)
    cfg, env = load_env(project.repo)
    ui = RecordingUI()
    rec = engine.rollback(cfg, env, options=engine.DeployOptions(yes=True), ui=ui)
    assert rec.commit == b and (project.site / "v.txt").read_text() == "B"
    assert f"from {c[:7]} to {b[:7]}" in ui.text


def test_failed_hooks_run_again_on_the_next_deploy(project):
    deploy_a_b_then_c_with_failing_hook(project)
    project.write_config("hooks:\n  remote_after: ['echo ran > hook.txt']\n")
    rec = run(project)  # same commit, same files: only the pending hooks to do
    assert rec.status == "success" and rec.hooks == "ok" and not rec.uploaded
    assert (project.site / "hook.txt").exists() and not manifest(project).get("hooks_pending")
    assert run(project).status == "up-to-date"


def test_remote_before_failure_is_recorded(project):
    project.write_config("hooks:\n  remote_before: ['exit 3']\n")
    project.write("a.txt", "a")
    project.commit()
    project.site.mkdir(parents=True)
    with pytest.raises(DeployError):
        run(project)
    last = history_records(project)[-1]
    assert (last["status"], last["files_done"], last["hooks"]) == ("failed", False, "failed")
    assert not (project.site / "a.txt").exists()


@pytest.mark.parametrize("broken", ["save_history", "save_manifest"])
def test_unsaved_records_are_never_reported_as_success(project, monkeypatch, broken):
    project.write("a.txt", "a")
    project.commit()

    real = getattr(_RemoteState, broken)
    calls = {"n": 0}

    def fail_final_save(self, *a):  # the manifest is saved once before any file changes
        calls["n"] += 1
        if broken == "save_manifest" and calls["n"] == 1:
            return real(self, *a)
        raise OSError("disk quota exceeded")
    monkeypatch.setattr(_RemoteState, broken, fail_final_save)
    with pytest.raises(StateError, match="could not be saved"):
        run(project)
    assert (project.site / "a.txt").exists()  # the files did go out
    if broken == "save_manifest":
        last = history_records(project)[-1]
        assert last["status"] == "failed" and last["manifest_saved"] is False


def test_unsaved_records_exit_nonzero_on_the_cli(project, monkeypatch):
    from click.testing import CliRunner

    from shipr.cli import cli
    monkeypatch.chdir(project.repo)
    project.write("a.txt", "a")
    project.commit()
    monkeypatch.setattr(_RemoteState, "save_history",
                        lambda self, r: (_ for _ in ()).throw(OSError("disk full")))
    r = CliRunner().invoke(cli, ["deploy"])
    assert r.exit_code == 1 and "could not be saved" in r.output


def test_records_of_another_deploy_target_are_refused(project):
    """C-5: prod and staging pointed at one state dir (bypassing config validation)."""
    project.write("a.txt", "a")
    project.write("old.txt", "old")
    project.commit()
    cfg_text = (project.repo / "shipr.yml").read_text()
    (project.repo / "shipr.yml").write_text(
        cfg_text + "    state_path: /state\n  staging:\n    remote_path: /staging\n"
        "    state_path: /state\n")
    from shipr import config
    cfg = config.load(project.repo / "shipr.yml")
    opts = engine.DeployOptions(yes=True)
    engine.deploy(cfg, cfg.env("prod"), None, opts, RecordingUI())
    with pytest.raises(StateError, match="belong to remote_path /site"):
        engine.deploy(cfg, cfg.env("staging"), None, opts, RecordingUI())


def test_records_unwritable_before_any_change_is_a_clean_error(project, monkeypatch):
    project.write("a.txt", "a")
    project.commit()
    monkeypatch.setattr(_RemoteState, "save_manifest",
                        lambda self, m: (_ for _ in ()).throw(OSError("read-only filesystem")))
    with pytest.raises(StateError, match="nothing on the server was changed"):
        run(project)
    assert not (project.site / "a.txt").exists()


def test_crash_between_checkpoints_does_not_fake_hotfixes(project, monkeypatch):
    """C-22: a hard crash loses up to 24 manifest updates; they must not become 'hotfixes'."""
    for i in range(30):
        project.write(f"f{i:02}.txt", "1")
    project.commit()
    run(project)
    for i in range(30):
        project.write(f"f{i:02}.txt", "22")  # different size: the stat surely changes
    project.commit()
    from shipr.providers.local import LocalProvider

    real = LocalProvider.upload
    n = {"site": 0}

    def crash(self, local, remote, mode=None):
        if "/.shipr/" not in remote:
            n["site"] += 1
            if n["site"] == 28:
                raise SystemExit("power cut")  # no graceful save after this
        return real(self, local, remote, mode)
    monkeypatch.setattr(LocalProvider, "upload", crash)
    monkeypatch.setattr(engine.Deployment, "_save", lambda self, m: None)
    with pytest.raises(SystemExit):
        run(project)
    monkeypatch.undo()
    rec = run(project)
    assert rec.status == "success"
    assert not [u for u in rec.uploaded if u.backup]  # shipr's own uploads aren't hotfixes
    assert sorted(u.path for u in rec.uploaded) == [f"f{i:02}.txt" for i in range(27, 30)]


def test_rollback_refuses_when_history_and_manifest_disagree(project, monkeypatch):
    for v in ("A", "B"):
        project.write("v.txt", v)
        project.commit()
        run(project)
    project.write("v.txt", "C")
    project.commit()
    real = _RemoteState.save_manifest
    calls = {"n": 0}

    def final_save_fails(self, m):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("disk full")
        real(self, m)
    monkeypatch.setattr(_RemoteState, "save_manifest", final_save_fails)
    with pytest.raises(StateError):
        run(project)  # files of C are out; history says so, the manifest doesn't
    monkeypatch.undo()
    cfg, env = load_env(project.repo)
    with pytest.raises(DeployError, match="disagree"):
        engine.rollback(cfg, env, options=engine.DeployOptions(yes=True), ui=RecordingUI())
    first = admin.history(env)[0]
    rec = engine.rollback(cfg, env, to=first.id, options=engine.DeployOptions(yes=True),
                          ui=RecordingUI())
    assert rec.commit == first.commit and (project.site / "v.txt").read_text() == "A"


def test_rollback_after_interrupted_deploy_restores_last_completed(project, monkeypatch):
    project.write("v.txt", "A")
    project.commit()
    run(project)
    project.write("v.txt", "B")
    b = project.commit()
    run(project)
    for i in range(3):
        project.write(f"n{i}.txt", "n")
    project.write("v.txt", "C")
    project.commit()
    from shipr.providers.local import LocalProvider

    real = LocalProvider.upload

    def interrupt(self, local, remote, mode=None):
        if remote.endswith("n2.txt"):
            raise KeyboardInterrupt
        return real(self, local, remote, mode)
    monkeypatch.setattr(LocalProvider, "upload", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(project)
    monkeypatch.undo()
    cfg, env = load_env(project.repo)
    ui = RecordingUI()
    rec = engine.rollback(cfg, env, options=engine.DeployOptions(yes=True), ui=ui)
    assert rec.commit == b and "interrupted" in ui.text
    assert (project.site / "v.txt").read_text() == "B" and not (project.site / "n0.txt").exists()


# --- legacy (shipr 0.1, in-root) records: migration safety ------------------------------------
def deploy_with_legacy_records(project, n=2):
    """n deploys that keep records at <remote>/.shipr, then the config goes back to default."""
    add_env_option(project, "state_path: /site/.shipr")
    for i in range(n):
        project.write("a.txt", "a" * (i + 1))
        project.commit()
        run(project)
    project.write_config()
    return project.site / ".shipr"


def test_legacy_records_keep_their_history(project):
    legacy = deploy_with_legacy_records(project)
    project.write("a.txt", "new")
    project.commit()
    run(project)
    assert len(list((legacy / "history").iterdir())) == 3 and not project.state.exists()


def test_records_in_both_locations_are_ambiguous(project):
    import shutil

    legacy = deploy_with_legacy_records(project)
    shutil.copytree(legacy, project.state)  # a migration that copied but hasn't removed yet
    project.write("a.txt", "new")
    project.commit()
    with pytest.raises(StateError, match="both") as e:
        run(project)
    assert str(legacy) in str(e.value).replace("\\", "/") or "/site/.shipr" in str(e.value)
    assert (legacy / "manifest.json").exists() and (project.state / "manifest.json").exists()
    assert (project.site / "a.txt").read_text() == "aa"  # nothing deployed
    shutil.rmtree(legacy)  # finishing the migration resolves it
    assert run(project).status == "success"
    assert len(list((project.state / "history").iterdir())) == 3  # history came along


def test_interrupted_migration_keeps_using_the_original(project):
    legacy = deploy_with_legacy_records(project)
    (project.state / "history").mkdir(parents=True)  # copy began: history only, no manifest yet
    project.write("a.txt", "new")
    project.commit()
    ui = RecordingUI()
    run(project, ui=ui)
    assert "inside the web root" in ui.text
    assert len(list((legacy / "history").iterdir())) == 3  # original records still in use


def test_legacy_records_of_another_target_are_refused(project):
    legacy = deploy_with_legacy_records(project)
    m = json.loads((legacy / "manifest.json").read_text())
    m["remote_path"] = "/other-site"
    (legacy / "manifest.json").write_text(json.dumps(m))
    project.write("a.txt", "new")
    project.commit()
    with pytest.raises(StateError, match="belong to remote_path /other-site"):
        run(project)


def test_first_deploy_to_default_location_warns_it_may_be_served(project):
    project.write("a.txt", "a")
    project.commit()
    ui = RecordingUI()
    run(project, ui=ui)
    assert "public_html/staging" in ui.text and "state_path" in ui.text
    project.write("a.txt", "b")
    project.commit()
    ui = RecordingUI()
    run(project, ui=ui)
    assert "public_html/staging" not in ui.text  # once, when the records are created


def test_explicit_state_path_outside_never_warns(project):
    add_env_option(project, "state_path: /records/site")
    project.write("a.txt", "a")
    project.commit()
    ui = RecordingUI()
    run(project, ui=ui)
    assert "warning" not in ui.text


# --- environment exposure of local commands (C-14) -----------------------------------------------
def env_dump_build(project, extra=""):
    script = project.repo / "dump.py"
    script.write_text("import json, os\nos.makedirs('out', exist_ok=True)\n"
                      "json.dump(sorted(os.environ), open('out/env.json', 'w'))\n")
    project.write_config(
        f"build:\n  commands: ['\"{Path(sys.executable).as_posix()}\" dump.py']\n"
        f"  output: out\n{extra}")
    (project.repo / ".env").write_text("APP_KEY=k\nSHIPR_PROD_PASSWORD=secret\n")
    project.commit()


def test_build_env_allowlist_hides_everything_else(project, monkeypatch):
    monkeypatch.setenv("SHIPR_KEY_PASSPHRASE", "pp")
    monkeypatch.setenv("NODE_ENV", "production")
    env_dump_build(project, "  env: [NODE_ENV, APP_KEY]\n")
    run(project)
    seen = set(json.loads((project.site / "env.json").read_text()))
    assert {"NODE_ENV", "APP_KEY", "SHIPR_ENV", "SHIPR_COMMIT"} <= seen
    assert "PATH" in seen or "Path" in seen  # basics so tools still work
    assert not {"SHIPR_PROD_PASSWORD", "SHIPR_KEY_PASSPHRASE"} & seen


def test_without_allowlist_commands_still_see_everything(project, monkeypatch):
    monkeypatch.setenv("SHIPR_KEY_PASSPHRASE", "pp")
    env_dump_build(project)
    run(project)
    seen = set(json.loads((project.site / "env.json").read_text()))
    assert {"SHIPR_PROD_PASSWORD", "SHIPR_KEY_PASSPHRASE", "APP_KEY"} <= seen  # documented
