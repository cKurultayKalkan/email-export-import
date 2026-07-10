from __future__ import annotations

import socket
import ssl as ssl_module
import time
from typing import Callable, TypeVar

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from .errors import AuthFailed, CertificateVerifyFailed, ConnectionFailed
from .models import Account

T = TypeVar("T")

# Per-socket-operation timeout. IMAPClient's default of None blocks forever on
# a half-dead connection; with a timeout the stall surfaces as an OSError that
# with_retry() turns into a reconnect. Long downloads are unaffected — the
# timeout applies per read, not per command.
SOCKET_TIMEOUT = 60


class MailConnection:
    """Owns one IMAP session; reconnects and retries transient failures.

    Long transfers outlive server idle timeouts (Gmail drops sessions after
    a few minutes), so every network operation should go through
    with_retry(), which transparently rebuilds the session — including
    re-selecting the folder that was active before the drop.
    """

    def __init__(self, account: Account, max_retries: int = 3) -> None:
        self.account = account
        self.max_retries = max_retries
        self._client: IMAPClient | None = None
        self._selected: tuple[str, bool] | None = None  # (folder, readonly)
        # Set after the first successful login. A later login rejection is
        # then a transient server condition (rate limit, per-IP connection
        # cap), not bad credentials, and with_retry() may retry it.
        self._authenticated_once = False

    def connect(self) -> IMAPClient:
        kwargs = {}
        if not self.account.verify_ssl:
            # Encrypted but unauthenticated TLS: accepts self-signed certs.
            ctx = ssl_module.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl_module.CERT_NONE
            kwargs["ssl_context"] = ctx
        try:
            client = IMAPClient(
                self.account.host,
                port=self.account.port,
                ssl=self.account.ssl,
                timeout=SOCKET_TIMEOUT,
                **kwargs,
            )
        except ssl_module.SSLCertVerificationError as exc:
            raise CertificateVerifyFailed(
                f"Could not verify the TLS certificate of "
                f"{self.account.host}:{self.account.port} — {exc}"
            ) from exc
        except (OSError, ssl_module.SSLError, socket.timeout) as exc:
            raise ConnectionFailed(
                f"Could not connect to {self.account.host}:{self.account.port} — {exc}"
            ) from exc
        try:
            client.login(self.account.email, self.account.password)
        except LoginError as exc:
            raise AuthFailed(
                f"{self.account.host} rejected the login for {self.account.email}. "
                "Check the email address and app password."
            ) from exc
        except IMAPClientError as exc:
            raise AuthFailed(
                f"{self.account.host} refused the login for {self.account.email}: {exc}"
            ) from exc
        except OSError as exc:
            raise ConnectionFailed(
                f"Connection to {self.account.host}:{self.account.port} failed during login — {exc}"
            ) from exc
        self._client = client
        self._authenticated_once = True
        if self._selected is not None:
            folder, readonly = self._selected
            client.select_folder(folder, readonly=readonly)
        return client

    @property
    def client(self) -> IMAPClient:
        if self._client is None:
            return self.connect()
        return self._client

    def select_folder(self, folder: str, readonly: bool = False) -> dict:
        self._selected = (folder, readonly)
        return self.with_retry(lambda c: c.select_folder(folder, readonly=readonly))

    def with_retry(self, fn: Callable[[IMAPClient], T]) -> T:
        last_exc: Exception = RuntimeError("unreachable")
        for attempt in range(self.max_retries):
            try:
                return fn(self.client)
            except AuthFailed as exc:
                # Only a reconnect's login can be rejected transiently; a
                # first-time rejection is bad credentials — no retry.
                if not self._authenticated_once:
                    raise
                last_exc = exc
                self._client = None
                time.sleep(min(2**attempt, 8))
            except (IMAPClientError, OSError) as exc:
                last_exc = exc
                self._client = None  # next .client access reconnects + reselects
                time.sleep(min(2**attempt, 8))
        raise last_exc

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            except Exception:
                pass
            self._client = None
