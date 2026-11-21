"""Audit C-13: telling "this server refuses shell commands" apart from "the probe failed"."""

import paramiko
import pytest

from shipr.config import Environment
from shipr.errors import CommandTimeout, DeployError, ExecNotSupported, ProviderError
from shipr.hooks import remote_exec_available
from shipr.providers.sftp import SFTPProvider


def probed(result=None, raises=None):
    p = SFTPProvider({"host": "h", "username": "u"})

    def exec_(cmd, cwd=None, timeout=None, on_output=None):
        assert timeout  # the probe never waits forever
        if raises:
            raise raises
        return result
    p.exec = exec_
    return p


@pytest.mark.parametrize("p", [
    probed((1, "This service allows sftp connections only.\n", "")),  # OpenSSH internal-sftp
    probed((1, "This account is currently not available.\n", "")),  # nologin shell
    probed(raises=ExecNotSupported("refused")),  # session channel administratively prohibited
], ids=["internal-sftp", "nologin", "prohibited"])
def test_explicit_refusal_means_no_shell(p):
    assert p.supports_exec() is False and p.exec_probe_error() is None


@pytest.mark.parametrize("p", [
    probed(raises=CommandTimeout("timed out")),
    probed(raises=ProviderError("could not run remote command: socket closed")),
    probed((127, "sh: echo: not found\n", "")),
    probed((0, "something else\n", "")),
], ids=["timeout", "connection", "odd-exit", "odd-output"])
def test_unknown_failure_is_not_mistaken_for_sftp_only(p):
    assert p.supports_exec() is False  # hashing etc. fall back safely
    assert p.exec_probe_error()


def test_working_shell():
    p = probed((0, "shipr-ok\n", ""))
    assert p.supports_exec() is True and p.exec_probe_error() is None


@pytest.mark.parametrize("code, refused", [
    (paramiko.common.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED, True),
    (paramiko.common.OPEN_FAILED_CONNECT_FAILED, False),
    (paramiko.common.OPEN_FAILED_RESOURCE_SHORTAGE, False),
])
def test_only_an_administrative_channel_refusal_counts(code, refused):
    class Transport:
        def open_session(self, timeout=None):
            raise paramiko.ChannelException(code, "nope")

    class Client:
        def get_transport(self):
            return Transport()

    p = SFTPProvider({"host": "h", "username": "u"})
    p.client = Client()
    with pytest.raises(ExecNotSupported if refused else ProviderError) as e:
        p.exec("echo hi")
    assert isinstance(e.value, ExecNotSupported) is refused


def env(ssh_exec):
    return Environment(name="prod", remote_path="/w", ssh_exec=ssh_exec)


def test_auto_with_an_unknown_probe_result_stops():
    p = probed(raises=CommandTimeout("timed out"))
    with pytest.raises(DeployError, match="ssh_exec: false"):
        remote_exec_available(env("auto"), p)


def test_auto_with_an_explicit_refusal_skips_hooks_as_documented():
    p = probed((1, "This service allows sftp connections only.\n", ""))
    assert remote_exec_available(env("auto"), p) is False


def test_ssh_exec_false_never_probes():
    p = probed(raises=AssertionError("must not probe"))
    assert remote_exec_available(env(False), p) is False
