class KeelError(Exception):
    """Base class for errors safe to show in the local console."""


class ConfigError(KeelError):
    pass


class ConfirmationError(KeelError):
    pass


class GcloudError(KeelError):
    pass


class AuthError(GcloudError):
    pass


class MonitoringError(KeelError):
    pass
