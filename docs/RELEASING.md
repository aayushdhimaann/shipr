# Releasing shipr

Publishing to PyPI is **not set up yet**. It waits until PyPI trusted publishing (OIDC from
GitHub Actions, no stored token) is configured for the project. Until then a release is a git tag
plus the wheel and sdist attached to a GitHub release, installed with
`uv tool install git+https://github.com/aayushdhimaann/shipr@vX.Y.Z` or from the wheel.

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

## Later: PyPI

When trusted publishing is configured on PyPI for this repository, add a tag-triggered workflow
job that builds with `uv build`, runs `scripts/smoke_installed.py`, and uploads with
`pypa/gh-action-pypi-publish` pinned to a commit SHA, in a protected `pypi` environment with
`id-token: write`. No API token is stored anywhere.
