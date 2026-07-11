from __future__ import annotations

import random
import socket
import ssl as ssl_module
import threading
import time
from typing import Callable, TypeVar

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from .errors import AuthFailed, CertificateVerifyFailed, ConnectionFailed
from .models import Account

T = TypeVar("T")


class _RetryCancelled(Exception):
    """Internal signal: the cancel event fired during a retry backoff."""


# Per-socket-operation timeout. IMAPClient's default of None blocks forever on
# a half-dead connection; with a timeout the stall surfaces as an OSError that
# with_retry() turns into a reconnect. Long downloads are unaffected — the
# timeout applies per read, not per command.
SOCKET_TIMEOUT = 60

# Waits between retries when a reconnect's login is rejected. Servers that
# rate-limit logins (fail2ban-style) ban for minutes, so the ordinary
# exponential backoff (capped at 8s) never outlasts the ban — this schedule
# does, without hammering the server with further login attempts.
AUTH_RETRY_SLEEPS = (30, 60, 120, 300)


class MailConnection:
    """Owns one IMAP session; reconnects and retries transient failures.

    Long transfers outlive server idle timeouts (Gmail drops sessions after
    a few minutes), so every network operation should go through
    with_retry(), which transparently rebuilds the session — including
    re-selecting the folder that was active before the drop.
    """

    def __init__(
        self,
        account: Account,
        max_retries: int = 3,
        cancel: threading.Event | None = None,
        jitter: float = 0.0,
    ) -> None:
        self.account = account
        self._jitter = jitter
        self.max_retries = max_retries
        self._client: IMAPClient | None = None
        self._selected: tuple[str, bool] | None = None  # (folder, readonly)
        # Set after the first successful login. A later login rejection is
        # then a transient server condition (rate limit, per-IP connection
        # cap), not bad credentials, and with_retry() may retry it.
        self._authenticated_once = False
        # When set, retry backoffs abort immediately on cancellation instead
        # of sleeping out the full (up to 300s) interval — a frozen "Cancel"
        # button is a bad experience even though the retry is "working as
        # intended".
        self._cancel = cancel

    def set_cancel(self, event: threading.Event) -> None:
        """Route retry backoffs through *event* so the planning pass and the
        serial transfer path become cancellable too (parallel workers get their
        own cancel at construction)."""
        self._cancel = event

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

    def _backoff(self, seconds: float) -> None:
        """Sleep, but abort the retry loop immediately if cancelled."""
        if self._jitter:
            seconds = seconds + random.uniform(0, seconds * self._jitter)
        if self._cancel is None:
            time.sleep(seconds)
        elif self._cancel.wait(seconds):
            raise _RetryCancelled()

    def with_retry(self, fn: Callable[[IMAPClient], T]) -> T:
        net_attempt = 0
        auth_attempt = 0
        while True:
            last_exc: Exception | None = None
            try:
                try:
                    return fn(self.client)
                except AuthFailed as exc:
                    # Only a reconnect's login can be rejected transiently; a
                    # first-time rejection is bad credentials — no retry.
                    if not self._authenticated_once or auth_attempt >= len(AUTH_RETRY_SLEEPS):
                        raise
                    last_exc = exc
                    self._client = None
                    self._backoff(AUTH_RETRY_SLEEPS[auth_attempt])
                    auth_attempt += 1
                except (IMAPClientError, OSError, ConnectionFailed) as exc:
                    # ConnectionFailed comes from connect() during a reconnect —
                    # as transient as the dropped command that triggered it.
                    net_attempt += 1
                    self._client = None  # next .client access reconnects + reselects
                    if net_attempt >= self.max_retries:
                        raise
                    last_exc = exc
                    self._backoff(min(2 ** (net_attempt - 1), 8))
            except _RetryCancelled:
                # Cancelled while waiting out the backoff: surface the error
                # that triggered this retry rather than the internal signal.
                raise last_exc from None

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            except Exception:
                pass
            self._client = None
