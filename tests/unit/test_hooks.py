"""Remote hook execution: streaming, timeouts, error wrapping, old provider signatures."""

import paramiko
import pytest

from shipr.errors import CommandTimeout, DeployError, ProviderError
from shipr.hooks import run_remote
from shipr.providers.base import Provider
from shipr.providers.sftp import SFTPProvider
from tests.helpers import RecordingUI


class OldStyleProvider(Provider):
    """A third-party provider written before `exec` grew `on_output`."""

    name = "old"

    def __init__(self, code=0):
        super().__init__({})
        self.code = code

    def exec(self, command, cwd=None, timeout=None):  # the pre-0.2 signature
        return self.code, "line one\nline two\n", "a warning\n"

    # the abstract file methods are irrelevant here
    connect = close = stat = walk = upload = download = delete = rename = \
        rmdir_if_empty = create_exclusive = lambda *a, **k: None


def test_old_exec_signature_still_works():
    ui = RecordingUI()
    run_remote(OldStyleProvider(), ["do it"], "/w", ui, timeout=5)
    assert ui.lines == ["remote$ do it", "  line one", "  line two", "  a warning"]
    with pytest.raises(DeployError, match="failed \\(2\\)"):
        run_remote(OldStyleProvider(code=2), ["do it"], "/w", RecordingUI(), timeout=5)


def test_new_exec_signature_streams_and_gets_the_timeout():
    class Streaming(OldStyleProvider):
        def exec(self, command, cwd=None, timeout=None, on_output=None):
            self.timeout = timeout
            on_output("live")
            return 0, "", ""
    p, ui = Streaming(), RecordingUI()
    run_remote(p, ["x"], "/w", ui, timeout=42)
    assert p.timeout == 42 and ui.lines == ["remote$ x", "  live"]


def test_sftp_exec_wraps_transport_errors():
    class DeadTransport:
        def open_session(self, timeout=None):
            raise paramiko.SSHException("SSH session not active")

    class Client:
        def get_transport(self):
            return DeadTransport()

    p = SFTPProvider({"host": "h", "username": "u"})
    p.client = Client()
    with pytest.raises(ProviderError, match="could not run remote command"):
        p.exec("true")


def test_sha256sum_timeout_falls_back_to_download(tmp_path):
    p = SFTPProvider({"host": "h", "username": "u"})
    p._exec_ok = True

    def slow(cmd, cwd=None, timeout=None, on_output=None):
        assert timeout  # hashing never waits forever
        raise CommandTimeout("timed out")
    p.exec = slow
    p.download = lambda remote, local: local.write_bytes(b"x")
    import hashlib
    assert p.sha256(["/w/x"]) == {"/w/x": hashlib.sha256(b"x").hexdigest()}
    assert p._sha256sum(["/w/x"]) == {}  # no fallback there: the caller decides
