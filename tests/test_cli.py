import json

from click.testing import CliRunner

from shipr.cli import cli


def test_init_detects_vite_and_validates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "package.json").write_text(json.dumps(
        {"scripts": {"build": "vite build"}, "devDependencies": {"vite": "5"}}))
    r = CliRunner().invoke(cli, ["init"])
    assert r.exit_code == 0, r.output
    text = (tmp_path / "shipr.yml").read_text()
    assert "npm run build" in text and "output: dist" in text
    assert CliRunner().invoke(cli, ["init"]).exit_code != 0  # no silent overwrite
    r = CliRunner().invoke(cli, ["validate"])
    assert r.exit_code == 0 and "production: ok" in r.output


def test_deploy_dry_run_and_history_via_cli(project, monkeypatch):
    monkeypatch.chdir(project.repo)
    project.write("a.txt", "a")
    project.commit()
    runner = CliRunner()
    r = runner.invoke(cli, ["deploy", "--dry-run"])
    assert r.exit_code == 0 and "upload  a.txt" in r.output, r.output
    r = runner.invoke(cli, ["deploy"])
    assert r.exit_code == 0 and "success" in r.output, r.output
    r = runner.invoke(cli, ["history"])
    assert "success" in r.output
    r = runner.invoke(cli, ["status"])
    assert "no drift" in r.output and "(up to date)" in r.output


def test_noninteractive_deletion_is_not_guessed(project, monkeypatch):
    monkeypatch.chdir(project.repo)
    project.write("a.txt", "a")
    project.commit()
    CliRunner().invoke(cli, ["deploy"])
    (project.repo / "a.txt").unlink()
    project.write("b.txt", "b")
    project.commit()
    r = CliRunner().invoke(cli, ["deploy"])  # stdin is not a TTY
    assert "pass --yes" in r.output and (project.site / "a.txt").exists()


def test_errors_are_clean_messages_with_exit_codes(project, monkeypatch):
    monkeypatch.chdir(project.repo)
    r = CliRunner().invoke(cli, ["deploy", "-e", "nope"])
    assert r.exit_code == 1 and "unknown environment 'nope'" in r.output
    assert "Traceback" not in r.output


def test_validate_reports_state_overlaps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shipr.yml").write_text(
        "version: 1\nenvironments:\n"
        "  prod: {remote_path: /var/www/html, sftp: {host: a.example.com, username: u}}\n"
        "  staging: {remote_path: /var/www/html, sftp: {host: b.example.com, username: u}}\n")
    r = CliRunner().invoke(cli, ["validate"])
    assert r.exit_code == 0 and r.output.count("can't tell whether") == 1, r.output
    (tmp_path / "shipr.yml").write_text(
        "version: 1\nenvironments:\n"
        "  prod: {remote_path: /var/www/html, sftp: {host: a.example.com, username: u}}\n"
        "  staging: {remote_path: /var/www/html/staging, sftp: {host: a.example.com, "
        "username: u}}\n")
    for args in (["validate"], ["status", "-e", "prod"], ["deploy", "-e", "staging"]):
        r = CliRunner().invoke(cli, args)
        assert r.exit_code == 1 and "same server" in r.output, (args, r.output)


def test_host_key_trust_is_never_assumed_without_a_terminal():
    from shipr.cli import make_ui

    ui = make_ui()  # CliRunner/pytest stdin is not a TTY
    assert ui.trust_host("h", "ssh-ed25519 SHA256:x") is False
