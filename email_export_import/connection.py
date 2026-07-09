from __future__ import annotations

import socket
import ssl as ssl_module
import time
from typing import Callable, TypeVar

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from .errors import AuthFailed, ConnectionFailed
from .models import Account

T = TypeVar("T")


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

    def connect(self) -> IMAPClient:
        try:
            client = IMAPClient(
                self.account.host, port=self.account.port, ssl=self.account.ssl
            )
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
        self._client = client
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
