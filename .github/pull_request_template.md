## What and why

<!-- What this changes, and the issue it fixes or the problem it solves. -->

## How it was tested

<!-- Tests added, and anything you checked by hand (which SFTP server, which OS). -->

## Checklist

- [ ] `uv run ruff check src tests scripts` and `uv run pytest` pass
- [ ] `uv run pytest -m integration` passes, or this change doesn't touch providers
- [ ] A bug fix comes with a test that fails without it
- [ ] User-facing changes are in `CHANGELOG.md` and the README
- [ ] Nothing new overwrites or deletes server files outside the planner, path checks and backups
- [ ] Nothing logs credentials
- [ ] If AI tools wrote part of this change, I've said so here, and I've reviewed and can explain every line
