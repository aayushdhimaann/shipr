# Writing a provider

A provider is how shipr reaches a target (SFTP, FTP, rsync, S3, ...). The engine handles Git, the
build, planning, locking, history and backups. A provider only moves files.

## The interface

Subclass `shipr.providers.base.Provider` and implement these methods. Paths are absolute POSIX
paths on the target.

| Method | Contract |
|---|---|
| `connect()` / `close()` | Open and close the connection. Raise `ProviderError` with a clear message on failure. |
| `stat(path) -> RemoteStat \| None` | Size, mtime and mode of a regular file; `None` if it doesn't exist. |
| `walk(root, skip) -> {rel: RemoteStat}` | Every file under `root`, recursively, keyed by relative POSIX path. Leave out the `skip` directory. |
| `upload(local, remote, mode) -> RemoteStat` | **Atomic** replace: write to `tmp_name(remote)`, then rename. Create parent dirs. Apply `mode` if given. Return the stat after the write. |
| `download(remote, local)` | Copy a file to the local machine. |
| `delete(path)` | Remove a file. |
| `rename(src, dst)` | Move a file, creating parent dirs. Fail if `dst` exists. |
| `rmdir_if_empty(path) -> bool` | Remove an empty directory. |
| `create_exclusive(path, text) -> bool` | Create only if the file is absent. This backs the deploy lock, so it must be atomic. |
| `supports_exec()` / `exec(cmd, cwd, timeout)` | Optional: can the target run shell commands, and run one. The base `can_exec()` already applies the user's `ssh_exec: false`. |
| `read_text`, `write_text`, `copy`, `sha256` | Optional. The base class implements them on top of `upload`/`download` (atomic, mode-preserving); override them only if you have something faster. |
| `describe()` / `diagnostics()` | A short target name for output, and extra lines for `shipr test`. |

Import the error types from `shipr.providers.base` (they are defined in `shipr.errors`).
Raise `ConnectionLost` when the connection dropped and your retries are exhausted. The engine then
stops instead of trying the remaining files. For other per-file errors, raise `OSError` or
`ProviderError`; the engine records the failure and continues.

## Registering it

Ship your provider as its own package and declare an entry point:

```toml
[project.entry-points."shipr.providers"]
ftp = "shipr_ftp:FTPProvider"
```

Users then write:

```yaml
environments:
  production:
    provider: ftp
    remote_path: /public_html
    ftp: {host: example.com, username: me, password: "${FTP_PASSWORD}"}
```

Your provider receives the `ftp:` mapping as `self.settings`. Validate it in `__init__`; using a
pydantic model with `hide_input_in_errors=True` keeps passwords out of error messages.

## Testing

`shipr.providers.local.LocalProvider` is the simplest complete implementation; start from it. Then
run the end-to-end tests in `tests/test_e2e_local.py` against your provider. The SFTP integration
tests in `tests/integration/` show how to test against a real server in Docker.
