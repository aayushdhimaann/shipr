# Writing a provider

A provider is how shipr reaches a target (SFTP, FTP, rsync, S3, ...). The engine handles Git, the
build, planning, locking, history and backups. A provider only moves files.

## The interface

Subclass `shipr.providers.base.Provider` and implement these methods. Paths are absolute POSIX
paths on the target.

| Method | Contract |
|---|---|
| `connect()` / `close()` | Open and close the connection. Raise `ProviderError` with a clear message on failure. |
| `stat(path) -> RemoteStat \| None` | Size, mtime and mode of a regular file; `None` if it doesn't exist or isn't a regular file. Don't follow symlinks. |
| `walk(root, skip) -> {rel: RemoteStat}` | Every file under `root`, recursively, keyed by relative POSIX path. Leave out the `skip` directory. Never descend into a symlink. Report symlinks, special files and directories you can't list with `RemoteStat(kind="symlink" \| "special" \| "unreadable")`: shipr then never touches anything at or under them. A provider that can't tell can leave `kind` at its default, `"file"`. |
| `upload(local, remote, mode) -> RemoteStat` | **Atomic** replace: write to `tmp_name(remote)`, then rename. Create parent dirs. Apply `mode` if given. Return the stat after the write. |
| `download(remote, local)` | Copy a file to the local machine. |
| `delete(path)` | Remove a file. |
| `rename(src, dst)` | Move a file, creating parent dirs. Fail if `dst` exists. |
| `rmdir_if_empty(path) -> bool` | Remove an empty directory. |
| `create_exclusive(path, text) -> bool` | Create only if the file is absent. This backs the deploy lock, so it must be atomic. |
| `supports_exec()` / `exec(cmd, cwd, timeout, on_output=None)` | Optional: can the target run shell commands, and run one. The base `can_exec()` already applies the user's `ssh_exec: false`. Raise `CommandTimeout` after `timeout` seconds. Pass each output line to `on_output` as it arrives, if given. Providers written before `on_output` existed still work: their output is shown when the command ends. Never retry a command yourself, because hooks aren't necessarily safe to repeat. Wrap transport errors in `ProviderError`. |
| `read_text`, `write_text`, `copy`, `sha256` | Optional. The base class implements them on top of `upload`/`download` (atomic, mode-preserving); override them only if you have something faster. |
| `describe()` / `diagnostics()` | A short target name for output, and extra lines for `shipr test`. |

### Confinement

The engine sets `provider.roots` to `(remote_path, state_path)`. `provider.confined(path)` returns
the directories between the outermost root and `path`. It compares path components, never string
prefixes, and refuses non-canonical or outside paths with `UnsafePath`.

If your backend can tell a symlink from a directory, implement `_lkind(path)` (lstat semantics:
`"file"`, `"dir"`, `"symlink"`, `"special"` or `None`), `_mkdir(path)` and `_mkdir_tree(path)`.
Then call the base class's `_ensure_dirs`, `_check_dirs` and `_check_target` immediately before
each request that writes, renames or deletes, as `sftp.py` and `local.py` do. Never cache the
result of a check.

### Retries and lost replies

A request can succeed on the server while its reply is lost. If your provider retries after
reconnecting, repeat only operations whose effect is the same when done twice: reads, uploads
via temp file and rename, mkdir. For `rename` and `delete`, check the target first. Raise
`AmbiguousOperation` (a `ProviderError`) when you can't prove what happened. Never guess,
and never undo. The executor records it as a failed file and the deploy continues.

`upload` should create its temporary file readable only by the deploy user from the start (not
chmod it after writing), and give it its final mode only once it is complete. `rename` may fail
across filesystems; raise a plain `OSError` then, and the engine falls back to a verified copy.

The deploy lock is removed with `delete_if_content(path, expected)`. The default reads the file
and deletes it only if it still holds `expected`. If your provider retries deletes after a
reconnect, override it to re-read before every attempt, as `sftp.py` does, so that a retry never
deletes a lock another deploy created in the meantime.

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

Your provider receives the `ftp:` mapping as `self.settings`. To have `shipr validate` check it
without connecting, override the optional classmethod `validate_settings(settings)` and raise
`ProviderError` naming the problem. Never include a setting's value: it may be a password. A
pydantic model with `hide_input_in_errors=True` does this for you (see `SFTPProvider`).
Providers without `validate_settings` keep working; their settings are checked when they
connect.

## Testing

`shipr.providers.local.LocalProvider` is the simplest complete implementation; start from it. Then
run the end-to-end tests in `tests/test_e2e_local.py` against your provider. The SFTP integration
tests in `tests/integration/` show how to test against a real server in Docker.
