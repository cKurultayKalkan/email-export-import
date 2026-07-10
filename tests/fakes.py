"""In-memory IMAPClient double.

Implements exactly the subset of IMAPClient the tool touches, returning
values in IMAPClient's shapes (bytes keys, (flags, delim, name) listings).
"""
from __future__ import annotations

import collections
import email
import itertools
from datetime import datetime


def make_message(
    uid: int,
    message_id: str | None,
    subject: str = "hi",
    flags: tuple[bytes, ...] = (),
    internaldate: datetime | None = None,
    attachment: bytes | None = None,
) -> dict:
    headers = f"Subject: {subject}\r\nFrom: a@x\r\nTo: b@y\r\n"
    if message_id is not None:
        headers += f"Message-ID: {message_id}\r\n"
    if attachment is not None:
        boundary = "BOUND"
        body = (
            f"{headers}MIME-Version: 1.0\r\n"
            f'Content-Type: multipart/mixed; boundary="{boundary}"\r\n\r\n'
            f"--{boundary}\r\nContent-Type: text/plain\r\n\r\nbody text\r\n"
            f"--{boundary}\r\nContent-Type: application/octet-stream\r\n"
            f"Content-Disposition: attachment; filename=f.bin\r\n"
            f"Content-Transfer-Encoding: 8bit\r\n\r\n"
        ).encode() + attachment + f"\r\n--{boundary}--\r\n".encode()
    else:
        body = f"{headers}\r\nbody text\r\n".encode()
    return {
        "uid": uid,
        "flags": flags,
        "internaldate": internaldate or datetime(2020, 1, 1),
        "body": body,
    }


Namespace = collections.namedtuple("Namespace", ["personal", "other", "shared"])


class FakeIMAPClient:
    def __init__(
        self,
        folders: dict[str, list[dict]] | None = None,
        special_use: dict[str, bytes] | None = None,
        delimiter: str = "/",
        uidvalidity: int = 1,
        namespace_prefix: str = "",
    ) -> None:
        self.folders = {n: list(m) for n, m in (folders or {"INBOX": []}).items()}
        self.special_use = special_use or {}
        self.delimiter = delimiter
        self.uidvalidity = uidvalidity
        self.namespace_prefix = namespace_prefix
        self.selected: str | None = None
        self.logged_in = False
        self.append_error: Exception | None = None
        self.login_error: Exception | None = None
        self._next_uid = itertools.count(1000)
        self.select_calls: list[str] = []

    # --- session ---------------------------------------------------------
    def login(self, user: str, password: str) -> None:
        if self.login_error is not None:
            raise self.login_error
        self.logged_in = True

    def logout(self) -> None:
        self.logged_in = False

    # --- folders ---------------------------------------------------------
    def namespace(self):
        return Namespace(
            personal=[(self.namespace_prefix, self.delimiter)], other=[], shared=[]
        )

    def list_folders(self):
        out = []
        for name in self.folders:
            flags = (self.special_use[name],) if name in self.special_use else ()
            out.append((flags, self.delimiter.encode(), name))
        return out

    def select_folder(self, name: str, readonly: bool = False) -> dict:
        self.selected = name
        self.select_calls.append(name)
        return {b"UIDVALIDITY": self.uidvalidity, b"EXISTS": len(self.folders[name])}

    def folder_exists(self, name: str) -> bool:
        return name in self.folders

    def create_folder(self, name: str) -> None:
        self.folders.setdefault(name, [])

    def folder_status(self, name: str, what=None) -> dict:
        return {b"MESSAGES": len(self.folders[name])}

    # --- messages --------------------------------------------------------
    def search(self, criteria="ALL"):
        return [m["uid"] for m in self.folders[self.selected]]

    def fetch(self, uids, data):
        result: dict[int, dict] = {}
        for m in self.folders[self.selected]:
            if m["uid"] not in uids:
                continue
            entry: dict[bytes, object] = {}
            for item in data:
                if item == b"FLAGS":
                    entry[b"FLAGS"] = m["flags"]
                elif item == b"INTERNALDATE":
                    entry[b"INTERNALDATE"] = m["internaldate"]
                elif item == b"RFC822.SIZE":
                    entry[b"RFC822.SIZE"] = len(m["body"])
                elif item == b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]":
                    entry[b"BODY[HEADER.FIELDS (MESSAGE-ID)]"] = self._mid_blob(m["body"])
                elif item == b"BODY.PEEK[]":
                    entry[b"BODY[]"] = m["body"]
            result[m["uid"]] = entry
        return result

    def append(self, folder: str, msg: bytes, flags=(), msg_time=None) -> None:
        if self.append_error is not None:
            raise self.append_error
        self.folders[folder].append(
            {
                "uid": next(self._next_uid),
                "flags": tuple(flags),
                "internaldate": msg_time or datetime(2020, 1, 1),
                "body": msg,
            }
        )

    @staticmethod
    def _mid_blob(body: bytes) -> bytes:
        mid = email.message_from_bytes(body).get("Message-ID")
        if not mid:
            return b"\r\n"
        return f"Message-ID: {mid}\r\n\r\n".encode()
