"""Every expected failure is a ShiprError: the CLI prints its message without a traceback."""


class ShiprError(Exception):
    exit_code = 1


class ConfigError(ShiprError):
    pass


class GitError(ShiprError):
    pass


class DeployError(ShiprError):
    pass


class Aborted(ShiprError):
    """The user declined a confirmation."""

    exit_code = 3


class LockedError(ShiprError):
    pass


class StateError(ShiprError):
    """shipr's records on the server are unreadable."""


class UnsafePath(ShiprError):
    pass


class ProviderError(ShiprError):
    pass


class ExecNotSupported(ProviderError):
    pass


class ConnectionLost(ProviderError):
    """The connection dropped and retries were exhausted: stop, don't try further files."""
