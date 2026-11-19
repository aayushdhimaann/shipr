# Changelog

## 0.1.0 (unreleased)

First version.

- Commands: `init`, `validate`, `test`, `deploy`, `status`, `history`, `rollback` and `unlock`.
- SFTP provider:
  - password, key and key-passphrase authentication, plus ssh-agent
  - host key verification, with trust on first use through `test --trust`
  - retries with backoff, and reconnecting after a dropped connection
  - atomic uploads
  - optional shell hooks over SSH
- Local-folder provider. Third-party providers can be added through entry points.
- Incremental deploys that compare file contents against a manifest kept on the server.
- Safety:
  - dry-run, and confirmation for production and for deletions
  - backups of files edited on the server
  - protected paths and built-in secret excludes
  - a lock against concurrent deploys
  - resuming after an interrupted deploy
  - path checks so nothing outside `remote_path` is touched
- Deploy history on the server, and rollback to an earlier commit.
