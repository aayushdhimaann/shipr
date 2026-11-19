"""End-to-end deploys through the real engine into a local folder acting as the server."""

import json
import sys
from pathlib import Path

import pytest

from shipr import admin, engine
from shipr.errors import Aborted, LockedError
from tests.helpers import RecordingUI, deploy, load_env


def run(project, ui=None, **opts):
    return deploy(project.repo, ui, **opts)


def manifest(project):
    return json.loads((project.site / ".shipr" / "manifest.json").read_text())


def test_full_then_incremental(project):
    project.write("index.php", "v1")
    project.write("css/app.css", "body{}")
    project.write(".env", "SECRET=1")
    sha = project.commit()
    rec = run(project)
    assert rec.status == "success"
    assert project.remote_files() == {"index.php", "css/app.css"}  # .env never uploaded
    assert manifest(project)["deployed_commit"] == sha
    assert (project.site / ".shipr" / ".htaccess").exists()

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
    (project.site / ".shipr").mkdir(parents=True)
    (project.site / ".shipr" / "lock").write_text('{"by": "someone"}')
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
    assert not (project.site / ".shipr" / "lock").exists()  # lock released

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
        st = RemoteState(p, "/site/.shipr", inside_webroot=True)
        st.acquire_lock("deploy-1")
        st.release_lock("deploy-2")  # someone else's id: must not remove it
        assert st.read_lock() and "deploy-1" in st.read_lock()
        st.release_lock("deploy-1")
        assert st.read_lock() is None
