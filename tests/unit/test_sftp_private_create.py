"""P-5 relies on paramiko internals: SFTPClient._request, SFTPAttributes packing and the
paramiko.sftp constants, because paramiko's public open() can't send a file mode. These tests
pin exactly what shipr uses, so a paramiko upgrade that changes them fails here, in every CI job,
instead of silently creating world-readable temp files (the real-server check is in
tests/integration: test_uploads_are_private_from_their_first_byte)."""

import stat

import paramiko
import pytest
from paramiko.message import Message
from paramiko.sftp import (
    CMD_ATTRS,
    CMD_CLOSE,
    CMD_FSETSTAT,
    CMD_FSTAT,
    CMD_HANDLE,
    CMD_OPEN,
    CMD_STATUS,
    CMD_WRITE,
    SFTP_FLAG_CREATE,
    SFTP_FLAG_EXCL,
    SFTP_FLAG_WRITE,
)

from shipr.providers.sftp import SFTPProvider


class Wire:
    """Stands in for paramiko.SFTPClient's request layer and records every request."""

    def __init__(self, server_mode):
        self.server_mode, self.sent = server_mode, []

    def _log(self, *a):
        pass

    def _request(self, t, *args):
        self.sent.append((t, args))
        msg = Message()
        if t == CMD_OPEN:
            msg.add_string(b"handle-1")
            msg.rewind()
            return CMD_HANDLE, msg
        if t == CMD_FSTAT:
            attrs = paramiko.SFTPAttributes()
            attrs.st_mode = stat.S_IFREG | self.server_mode
            attrs._pack(msg)
            msg.rewind()
            return CMD_ATTRS, msg
        return CMD_STATUS, msg

    def _async_request(self, fileobj, t, *args):
        self.sent.append((t, args))
        return len(self.sent)

    def _finish_responses(self, fileobj):
        pass


def on_the_wire(attrs):
    """Pack and parse back, as the server would see the attributes."""
    msg = Message()
    attrs._pack(msg)
    msg.rewind()
    return paramiko.SFTPAttributes._from_msg(msg)


def test_paramiko_internals_shipr_relies_on_exist():
    assert callable(getattr(paramiko.SFTPClient, "_request", None))
    assert paramiko.SFTPFile.__init__.__code__.co_varnames[1:3] == ("sftp", "handle")


@pytest.mark.parametrize("server_mode", [0o600, 0o644], ids=["honoured", "ignored"])
def test_temp_file_is_opened_with_mode_0600_in_the_request_itself(server_mode):
    wire = Wire(server_mode)
    f = SFTPProvider._create_private(wire, "/w/.config.php.shipr-tmp")
    f.set_pipelined(True)
    f.write(b"<?php $pw = 'x';")
    f.close()
    kinds = [t for t, _ in wire.sent]
    t, (path, flags, attrs) = wire.sent[0]
    assert t == CMD_OPEN and path == "/w/.config.php.shipr-tmp"
    assert flags == SFTP_FLAG_WRITE | SFTP_FLAG_CREATE | SFTP_FLAG_EXCL  # never TRUNC/follow
    sent = on_the_wire(attrs)
    assert sent._flags & paramiko.SFTPAttributes.FLAG_PERMISSIONS and sent.st_mode == 0o600
    if server_mode == 0o600:
        assert CMD_FSETSTAT not in kinds
    else:  # the server ignored the mode: fixed before the first byte
        assert kinds.index(CMD_FSETSTAT) < kinds.index(CMD_WRITE)
        assert on_the_wire(wire.sent[kinds.index(CMD_FSETSTAT)][1][1]).st_mode == 0o600
    assert kinds[-1] == CMD_CLOSE
