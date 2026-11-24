"""Run shell commands on this machine so that a timeout or Ctrl-C stops them completely."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable

from .errors import CommandTimeout

__all__ = ["run_shell"]


def run_shell(command: str, *, cwd: object = None, env: dict[str, str] | None = None,
              timeout: float | None = None,
              on_output: Callable[[str], None] | None = None,
              capture: bool = False) -> tuple[int, str]:
    """Run `command` through the shell in its own process group, with no stdin (hooks are
    non-interactive). Output goes straight to the terminal, unless `on_output` or `capture`:
    then stdout and stderr are merged, passed line by line to `on_output` as they arrive,
    and (with `capture`) returned. On timeout or interruption the whole group, the shell and
    everything it started, is stopped. Returns (exit code, captured output)."""
    piped = capture or on_output is not None
    group: dict[str, object] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"  # type: ignore[attr-defined]
        else {"start_new_session": True})
    proc = subprocess.Popen(
        command, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if piped else None,
        stderr=subprocess.STDOUT if piped else None,
        text=True, encoding="utf-8", errors="replace", **group)
    kept: list[str] = []
    reader = None
    if piped:
        reader = threading.Thread(target=_pump, args=(proc, on_output, capture, kept),
                                  daemon=True)
        reader.start()
    try:
        _wait(proc, reader, timeout, command)
    except subprocess.TimeoutExpired:
        _stop(proc)
        raise CommandTimeout(f"timed out after {timeout:g}s: {command}; shipr stopped it and "
                             "its child processes") from None
    except BaseException:
        _stop(proc)
        raise
    return proc.returncode, "".join(kept)


def _pump(proc: subprocess.Popen, on_output: Callable[[str], None] | None, capture: bool,
          kept: list[str]) -> None:
    assert proc.stdout is not None
    if on_output is None:  # capture only: keep the output exactly as printed
        kept.append(proc.stdout.read())
        return
    for line in proc.stdout:
        on_output(line.rstrip("\r\n"))
        if capture:
            kept.append(line)


def _wait(proc: subprocess.Popen, reader: threading.Thread | None, timeout: float | None,
          command: str) -> None:
    """Wait for the command, and for its output to be read, within one `timeout`."""
    deadline = None if timeout is None else time.monotonic() + timeout
    proc.wait(timeout=timeout)
    if reader:  # a background process may still hold the output open
        reader.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        if reader.is_alive():
            raise subprocess.TimeoutExpired(command, timeout or 0)


def _stop(proc: subprocess.Popen) -> None:
    """Stop the command's whole process group: politely first, then by force."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 3.0)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break  # the whole group is already gone
            try:
                proc.wait(timeout=grace)
                os.killpg(proc.pid, 0)  # the shell exited: are its children gone too?
            except ProcessLookupError:
                break
            except subprocess.TimeoutExpired:
                continue
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
