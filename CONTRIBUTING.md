# Contributing

Thanks for helping. Bug reports, docs fixes, providers and features are all welcome.

## Setup and checks

Setup, running from source, the test suites and the project layout are in the README's
[Development](README.md#development) section. Before opening a PR, run:

```bash
uv run ruff check src tests && uv run pytest && uv run pytest -m integration
```

## Guidelines

- **Keep the core provider-agnostic.** Anything SFTP-specific belongs in `providers/sftp.py`.
- **No framework-specific behaviour.** Support for a new stack should be an example config, not code.
- **Safety first.** Never add a code path that deletes or overwrites remote files without going
  through the planner, path checks and backups. Never log credentials.
- **Tests come with changes.** For a bug fix, add a test that fails without it.
- Keep dependencies minimal; discuss new ones in an issue first.
- Use small, focused commits with messages in the imperative mood ("Add FTP provider").

## Architecture rules

[docs/FLOW.md](docs/FLOW.md) diagrams every stage and names the function behind each step. If you
change how a step works, update its diagram in the same PR.

Dependencies point one way: `cli → engine/admin → workspace/planner/executor/state →
providers`. `planner.py` does no I/O, so its decision table is unit-tested directly. Shared
behaviour belongs in one place:
- generic provider operations (`read_text`, `write_text`, `copy`, `sha256`) live on the base
  `Provider`;
- error types live in `errors.py`;
- user output goes through `ui.UI` (`echo`, `warn`, `confirm`).

## Reporting bugs

Include `shipr --version`, your OS, the command, and the output (remove hostnames and secrets).
For security problems, see [SECURITY.md](SECURITY.md).
