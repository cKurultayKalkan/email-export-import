class MigrationError(Exception):
    """Base for all migration errors."""


class ConnectionFailed(MigrationError):
    """Could not reach the server or negotiate TLS."""


class AuthFailed(MigrationError):
    """Server rejected the credentials."""


class QuotaExceeded(MigrationError):
    """Destination refused APPEND because the mailbox is full."""
