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

# Ceiling on the kernel's per-socket send buffer.
#
# Uploading a message hands its whole body to the socket in one call, and the
# kernel is free to queue megabytes of it (macOS auto-tunes the send buffer up
# to net.inet.tcp.autosndbufmax, 4 MB by default) and then build one enormous
# mbuf chain out of that queue to hand to the NIC. Copying such a chain is what
# panics the macOS send path under sustained bulk upload:
#
#     panic: m_copym_with_hdrs ... copy overflow @uipc_mbuf.c:3268
#
# Pinning SO_SNDBUF disables that auto-tuning, so the queue — and therefore the
# chain built from it — stays bounded no matter how large the message is:
# sendall() simply loops, pushing the body in buffer-sized pieces. It costs
# nothing in practice (the destination server, not this buffer, is the
# bottleneck) and removes the oversize condition on every platform, with no
# root access and nothing for the user to configure.
SEND_BUFFER_BYTES = 256 * 1024


def _cap_send_buffer(client: IMAPClient, size: int = SEND_BUFFER_BYTES) -> None:
    """Best-effort: never fail a connection over a tuning hint."""
    try:
        client.socket().setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, size)
    except Exception:
        pass


# The second, independent layer against the same kernel panic: even with the
# send buffer pinned, a machine with TCP segmentation offload enabled plus
# content-filter software was seen panicking under sustained upload (observed
# in the field on macOS 26 with Check Point installed). Slicing every write
# and pausing between slices keeps the kernel's queue shallow at all times,
# so the oversized-mbuf-chain copy that panics simply never exists.
#
# This ceiling is DELIBERATELY not configurable — no setting, no environment
# variable. The user-facing rate limit can only slow uploads further; nothing
# can raise this. A migration tool must never be able to take the host down,
# and "fast" is not worth a kernel panic.
HARD_UPLOAD_CEILING_BYTES_PER_SEC = 2 * 1024 * 1024  # field-validated for 5h46m

# And a process-wide aggregate ceiling on top: many simultaneous transfers
# times many workers must not multiply into a flood (32 connections at
# 2 MB/s each would still stress the shared mbuf pool). Every connection's
# writes draw from this one bucket, whatever the user's settings say.
GLOBAL_UPLOAD_CEILING_BYTES_PER_SEC = 8 * 1024 * 1024


def _global_pacer():
    """Created lazily so importing this module never starts clocks in tests."""
    global _GLOBAL_PACER
    if _GLOBAL_PACER is None:
        from .throttle import RateLimiter

        _GLOBAL_PACER = RateLimiter(GLOBAL_UPLOAD_CEILING_BYTES_PER_SEC)
    return _GLOBAL_PACER


_GLOBAL_PACER = None


class _PacedSocket:
    """Wraps the connection's socket: writes go out in small slices with a
    drain pause after each, enforcing the hard per-connection upload ceiling.
    Everything else passes through untouched (reads use imaplib's buffered
    file object created before this wrapper is installed)."""

    CHUNK = 64 * 1024
    _PAUSE = CHUNK / HARD_UPLOAD_CEILING_BYTES_PER_SEC

    def __init__(self, sock) -> None:
        self._sock = sock

    def __getattr__(self, name):
        return getattr(self._sock, name)

    def sendall(self, data) -> None:
        mv = memoryview(bytes(data)) if not isinstance(data, (bytes, memoryview)) else memoryview(data)
        pacer = _global_pacer()
        for i in range(0, len(mv), self.CHUNK):
            chunk = mv[i : i + self.CHUNK]
            pacer.acquire(len(chunk))  # process-wide aggregate ceiling
            self._sock.sendall(chunk)
            time.sleep(self._PAUSE)  # let the queue drain before the next slice


def _install_hard_ceiling(client: IMAPClient) -> None:
    """Best-effort like _cap_send_buffer, but on any real connection this
    succeeds; the guard is for exotic transports in tests."""
    try:
        client._imap.sock = _PacedSocket(client._imap.sock)  # noqa: SLF001
    except Exception:
        pass


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
        _cap_send_buffer(client)  # bound the kernel's send queue before any upload
        _install_hard_ceiling(client)  # and keep that queue shallow, always
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
