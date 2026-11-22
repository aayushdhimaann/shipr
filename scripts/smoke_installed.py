"""Release check: inspect the built artifacts and smoke-test an *installed* shipr.

    python scripts/smoke_installed.py --dist dist/            # wheel and sdist contents
    python scripts/smoke_installed.py --venv /tmp/env          # the CLI installed in /tmp/env

The CLI test runs `shipr` from that environment in a throwaway git repo (never this checkout):
init, validate, deploy, an incremental deploy, status, history, rollback, unlock, dry-run, all
against a local folder. With --sftp-* options it also deploys to a real SFTP server; the
password is read from $SHIPR_SMOKE_PASSWORD and never printed. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

REQUIRES_PYTHON = "!=3.11.0,!=3.11.1,!=3.11.2,!=3.11.3,>=3.10.12"
DEPENDENCIES = {"click", "paramiko", "pathspec", "pydantic", "python-dotenv", "pyyaml"}
COMMANDS = {"init", "validate", "test", "deploy", "status", "history", "rollback", "unlock"}


def check(ok: bool, what: str) -> None:
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        sys.exit(1)


# --- artifacts -------------------------------------------------------------------------------
def check_dist(dist: Path) -> str:
    wheels, sdists = sorted(dist.glob("shipr-*.whl")), sorted(dist.glob("shipr-*.tar.gz"))
    check(len(wheels) == 1 and len(sdists) == 1, f"one wheel and one sdist in {dist}")
    version = wheels[0].name.split("-")[1]
    check(sdists[0].name == f"shipr-{version}.tar.gz", f"sdist version matches ({version})")

    with zipfile.ZipFile(wheels[0]) as whl:
        names = whl.namelist()
        info = f"shipr-{version}.dist-info"
        check(all(n.startswith(("shipr/", info + "/")) for n in names),
              "wheel holds only the shipr package and its metadata (no tests, docs)")
        check("shipr/providers/sftp.py" in names and "shipr/cli.py" in names, "wheel modules")
        check(f"{info}/licenses/LICENSE" in names, "wheel carries the LICENSE")
        meta = whl.read(f"{info}/METADATA").decode()
        entry = whl.read(f"{info}/entry_points.txt").decode()
    check(f"Requires-Python: {REQUIRES_PYTHON}" in meta,
          "Requires-Python excludes versions without tarfile extraction filters")
    deps = {re.split(r"[<>=!; ]", line.split(":", 1)[1].strip())[0].lower()
            for line in meta.splitlines() if line.startswith("Requires-Dist:")}
    check(deps == DEPENDENCIES, f"runtime dependencies: {sorted(deps)}")
    check("License-Expression: MIT" in meta, "license metadata")
    check("shipr = shipr.cli:main" in entry, "console script entry point")
    check("[shipr.providers]" in entry and "sftp = shipr.providers.sftp:SFTPProvider" in entry
          and "local = shipr.providers.local:LocalProvider" in entry, "provider entry points")

    with tarfile.open(sdists[0]) as sd:
        names = {n.split("/", 1)[1] for n in sd.getnames() if "/" in n}
    for needed in ("pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md", "uv.lock",
                   "src/shipr/cli.py", "tests/conftest.py", "docs/security.md"):
        check(needed in names, f"sdist contains {needed}")
    check(not any(n.startswith((".venv", ".git/", "graphify-out", "dist/")) for n in names),
          "sdist has no virtualenv, git data or build output")
    return version


# --- installed CLI ---------------------------------------------------------------------------
class Installed:
    def __init__(self, venv: Path, home: Path):
        bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        suffix = ".exe" if os.name == "nt" else ""
        self.shipr, self.python = bin_dir / f"shipr{suffix}", bin_dir / f"python{suffix}"
        self.env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
                    "XDG_CONFIG_HOME": str(home / ".config"), "APPDATA": str(home / "AppData"),
                    "GIT_AUTHOR_NAME": "smoke", "GIT_AUTHOR_EMAIL": "smoke@example.com",
                    "GIT_COMMITTER_NAME": "smoke", "GIT_COMMITTER_EMAIL": "smoke@example.com"}

    def run(self, *args: str, cwd: Path, ok: bool = True) -> str:
        r = subprocess.run([str(self.shipr), *args], cwd=cwd, env=self.env, text=True,
                           capture_output=True, stdin=subprocess.DEVNULL)
        out = r.stdout + r.stderr
        if (r.returncode == 0) != ok:
            print(out)
            check(False, f"shipr {' '.join(args)} exited {r.returncode}")
        return out


def git(cwd: Path, *args: str, env: dict) -> None:
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def smoke(venv: Path, version: str | None, sftp: dict | None) -> None:
    with tempfile.TemporaryDirectory(prefix="shipr-smoke-") as tmp:
        tmp = Path(tmp)
        cli = Installed(venv, tmp / "home")
        probe = subprocess.run(
            [str(cli.python), "-I", "-c",
             "import importlib.metadata as m, shipr, sys; d = m.distribution('shipr'); "
             "eps = {e.name for e in d.entry_points if e.group == 'shipr.providers'}; "
             "print(d.version, shipr.__version__, sorted(eps), shipr.__file__, "
             "sys.version.split()[0], sep='|')"],
            cwd=tmp, env=cli.env, text=True, capture_output=True, check=True).stdout.strip()
        dist_version, mod_version, eps, location, pyver = probe.split("|")
        print(f"      python {pyver}, shipr imported from {location}")
        check(Path(location).resolve().is_relative_to(venv.resolve()),
              "shipr is imported from the installed environment, not a source checkout")
        check(dist_version == mod_version and (version is None or dist_version == version),
              f"installed version {dist_version} (metadata = shipr.__version__)")
        check(eps == "['local', 'sftp']", "provider entry points are registered")
        check(cli.run("--version", cwd=tmp).strip() == f"shipr, version {dist_version}",
              "shipr --version")
        helptext = cli.run("--help", cwd=tmp)
        check(all(re.search(rf"^\s+{c}\s", helptext, re.M) for c in COMMANDS),
              f"shipr --help lists {', '.join(sorted(COMMANDS))}")

        repo, server = tmp / "repo", tmp / "server"
        repo.mkdir()
        server.mkdir()
        git(repo, "init", "-q", "-b", "main", env=cli.env)
        cli.run("init", cwd=repo)
        check((repo / "shipr.yml").is_file(), "shipr init writes shipr.yml")
        (repo / "shipr.yml").write_text(
            "version: 1\nproject: {name: smoke}\n"
            f"defaults:\n  provider: local\n  local: {{root: '{server.as_posix()}'}}\n"
            "environments:\n  prod:\n    remote_path: /site\n")
        (repo / "index.html").write_text("v1")
        (repo / "old.txt").write_text("old")
        (repo / ".env").write_text("SECRET=never-uploaded\n")
        git(repo, "add", "-A", env=cli.env)
        git(repo, "commit", "-qm", "v1", env=cli.env)
        check("prod: ok" in cli.run("validate", cwd=repo), "shipr validate")
        check("upload  index.html" in cli.run("deploy", "--dry-run", cwd=repo),
              "shipr deploy --dry-run")
        check(not (server / "site").exists(), "dry run changed nothing")
        check("success" in cli.run("deploy", cwd=repo), "shipr deploy (git export + upload)")
        site = server / "site"
        check((site / "index.html").read_text() == "v1" and not (site / ".env").exists(),
              "files deployed, .env excluded")
        (repo / "index.html").write_text("v2")
        (repo / "old.txt").unlink()
        git(repo, "commit", "-qam", "v2", env=cli.env)
        out = cli.run("deploy", "--yes", cwd=repo)
        check("1 uploaded, 1 deleted" in out and (site / "index.html").read_text() == "v2"
              and not (site / "old.txt").exists(), "incremental deploy with a deletion")
        check("no drift" in cli.run("status", cwd=repo), "shipr status")
        check(cli.run("history", cwd=repo).count("success") == 2, "shipr history")
        cli.run("rollback", "--yes", cwd=repo)
        check((site / "index.html").read_text() == "v1" and (site / "old.txt").exists(),
              "shipr rollback restores the previous deploy")
        check("no lock was held" in cli.run("unlock", cwd=repo), "shipr unlock")
        out = cli.run("deploy", "-e", "nope", cwd=repo, ok=False)
        check("unknown environment" in out and "Traceback" not in out, "clean error, no traceback")

        if sftp:
            remote = f"{sftp['root']}/smoke-{os.getpid()}"
            (repo / "shipr.yml").write_text(
                "version: 1\nproject: {name: smoke}\nenvironments:\n  prod:\n"
                f"    remote_path: {remote}\n    sftp:\n      host: {sftp['host']}\n"
                f"      port: {sftp['port']}\n      username: {sftp['user']}\n"
                "      password: ${SHIPR_SMOKE_PASSWORD}\n"
                f"      host_key: {sftp['host_key']}\n")
            git(repo, "commit", "-qam", "sftp", env=cli.env)
            check("write access" in cli.run("test", cwd=repo), "shipr test over SFTP")
            out = cli.run("deploy", cwd=repo)
            check("success" in out and os.environ["SHIPR_SMOKE_PASSWORD"] not in out,
                  "shipr deploy over SFTP (password never printed)")
            check("no drift" in cli.run("status", cwd=repo), "shipr status over SFTP")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dist", type=Path, help="check the wheel and sdist in this folder")
    ap.add_argument("--venv", type=Path, help="smoke-test the shipr installed in this env")
    for opt in ("host", "port", "user", "root", "host-key"):
        ap.add_argument(f"--sftp-{opt}")
    a = ap.parse_args()
    version = check_dist(a.dist) if a.dist else None
    sftp = None
    if a.sftp_host:
        sftp = {"host": a.sftp_host, "port": a.sftp_port or "22", "user": a.sftp_user,
                "root": a.sftp_root, "host_key": a.sftp_host_key}
    if a.venv:
        smoke(a.venv, version, sftp)
    print("all checks passed")


if __name__ == "__main__":
    main()
