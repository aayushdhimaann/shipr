"""Every expected failure is a ShiprError: the CLI prints its message without a traceback."""


class ShiprError(Exception):
    exit_code = 1


class ConfigError(ShiprError):
    pass


class GitError(ShiprError):
    pass


class DeployError(ShiprError):
    pass


class CommandTimeout(DeployError):
    """A hook command ran longer than `hooks.timeout`. It is never retried in the same run."""


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


class AmbiguousOperation(ProviderError):
    """The connection dropped before the server confirmed an operation, and shipr cannot prove
    whether it happened. Nothing was repeated or undone: the paths are left as they are."""


class ConnectionLost(ProviderError):
    """The connection dropped and retries were exhausted: stop, don't try further files."""
