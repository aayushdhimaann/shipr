# Releasing shipr

A release is a git tag, a GitHub release with the wheel and sdist attached, and the same files
on PyPI. `.github/workflows/release.yml` uploads them with PyPI trusted publishing (GitHub Actions
proves its identity to PyPI; no API token is stored anywhere).

## Version

The version lives in one place: `src/shipr/__init__.py`. `pyproject.toml` reads it
(`[tool.hatch.version]`), and so does `shipr --version`.

## Checklist

1. **Version and changelog.** Set `__version__`. In `CHANGELOG.md`, rename the top section to
   `## X.Y.Z (YYYY-MM-DD)` and check that its breaking changes are complete.
2. **Lockfile.** `uv lock`, then `git diff uv.lock` (only intended changes).
3. **Checks** on a clean tree:

   ```bash
   uv run ruff check src tests scripts
   uv run pytest -rs
   uv run pytest -m integration -rs      # needs Docker
   ```

4. **CI is green** on the commit to tag, including Windows, macOS, the oldest supported patch
   releases (3.10.12, 3.11.4) and the `package` job.
5. **Build and inspect** the artifacts:

   ```bash
   rm -rf dist && uv build
   uv run --no-project python scripts/smoke_installed.py --dist dist
   ```

   This checks that the wheel holds only the `shipr` package, its LICENSE, the
   `Requires-Python` that matches the tarfile extraction-filter requirement, the runtime
   dependencies and both entry point groups, and that the sdist holds the sources, tests and docs
   without build output or a virtualenv.
6. **Smoke-test the installed CLI** in clean environments, never from the checkout:

   ```bash
   uv venv --python 3.10.12 /tmp/shipr-wheel
   uv pip install --python /tmp/shipr-wheel dist/*.whl
   uv run --no-project python scripts/smoke_installed.py --dist dist --venv /tmp/shipr-wheel
   ```

   Repeat with the newest Python, and with the sdist installed by pip
   (`python -m pip install dist/*.tar.gz`). To also deploy to a real SFTP server, add
   `--sftp-host --sftp-port --sftp-user --sftp-root --sftp-host-key` and export the password as
   `SHIPR_SMOKE_PASSWORD` (it is read through `${...}` and never printed).
7. **Tag and attach.** `git tag -s vX.Y.Z && git push origin vX.Y.Z`, then create a GitHub release
   for the tag with the changelog section and attach both files from `dist/`, with their SHA-256
   (`sha256sum dist/*`).
8. **Publish to PyPI.** Publishing the GitHub release starts the `release` workflow. It builds
   from the tag, checks that the tag matches the package version, smoke-tests the installed
   wheel, and checks that its files are byte-identical to the ones attached to the release (the
   build is reproducible). The `publish` job then waits in the `pypi` environment: approve it in
   the workflow run, and it uploads. A PyPI version can never be uploaded again, even after it is
   deleted, so a mistake means a new version number.

   For a tag that already has a GitHub release, run it by hand: Actions → release → Run workflow,
   with the tag (e.g. `v0.2.0`).

## Supported Python versions

`requires-python = ">=3.10.12, !=3.11.0, !=3.11.1, !=3.11.2, !=3.11.3"`. Earlier patch releases
lack tarfile's extraction filters, which the git export relies on (`gitops.extract`); shipr also
checks for the filter at runtime and refuses to run without it. Raising or lowering this bound
needs the matching change in CI (`test` matrix) and in `scripts/smoke_installed.py`.

## Dependency bounds

The lower bounds are tested: the suite (unit, e2e and real OpenSSH) passes with click 8.1.0,
paramiko 4.0.0, pathspec 0.12.0, pydantic 2.6.0, python-dotenv 1.0.0 and PyYAML 6.0. paramiko
3.x is excluded because key-plus-passphrase logins fail next to current `cryptography`. paramiko is
capped below 6 because uploads use its internals (see docs/security.md); before raising the cap,
run `tests/unit/test_sftp_private_create.py` and the integration suite against the new version.

## PyPI setup (once)

- pypi.org → Account → Publishing → add a (pending) trusted publisher: project `shipr`, owner
  `aayushdhimaann`, repository `shipr`, workflow `release.yml`, environment `pypi`.
- GitHub → Settings → Environments → `pypi`, with the maintainer as a required reviewer, so
  nothing reaches PyPI without an approval.
- Only the `publish` job has `id-token: write`; the build and its tests run without it.
