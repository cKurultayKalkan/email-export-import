from __future__ import annotations

import email
from typing import Callable

from .connection import MailConnection
from .errors import QuotaExceeded
from .models import FolderPlan, TransferProgress
from .state import MigrationState

# Flags copied to the destination. \Recent is server-managed (RFC 3501) and
# must never be set by a client.
PRESERVED_FLAGS = (b"\\Seen", b"\\Answered", b"\\Flagged", b"\\Draft", b"\\Deleted")

_QUOTA_MARKERS = ("quota", "overquota", "over quota", "exceeded")

# Fired after every processed message: (source_folder, uid).
MessageCallback = Callable[[str, int], None]

_META_FIELDS = [
    b"FLAGS",
    b"INTERNALDATE",
    b"RFC822.SIZE",
    b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]",
]
_MID_KEY = b"BODY[HEADER.FIELDS (MESSAGE-ID)]"


def parse_message_id(header_blob: bytes | None) -> str | None:
    if not header_blob:
        return None
    mid = email.message_from_bytes(header_blob).get("Message-ID")
    return mid.strip() if mid else None


def preserved_flags(flags: tuple[bytes, ...]) -> tuple[bytes, ...]:
    return tuple(f for f in flags if f in PRESERVED_FLAGS)


def is_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


def migrate_folder(
    src: MailConnection,
    dst: MailConnection,
    plan: FolderPlan,
    state: MigrationState,
    progress: TransferProgress,
    on_message: MessageCallback | None = None,
) -> None:
    info = src.select_folder(plan.source, readonly=True)
    state.set_uidvalidity(plan.source, info[b"UIDVALIDITY"])

    if plan.create:
        # folder_exists guard makes the create idempotent under retry.
        dst.with_retry(
            lambda c: None if c.folder_exists(plan.dest) else c.create_folder(plan.dest)
        )

    uids = src.with_retry(lambda c: c.search())
    if not uids:
        return

    # Cheap metadata pass for the whole folder — no bodies, memory stays flat.
    meta = src.with_retry(lambda c: c.fetch(uids, _META_FIELDS))

    for uid in uids:
        m = meta.get(uid)
        if m is None:
            # Vanished between search() and fetch() (expunged mid-run):
            # nothing to copy, but the message was processed — count and report it.
            progress.skipped += 1
            if on_message is not None:
                on_message(plan.source, uid)
            continue
        message_id = parse_message_id(m.get(_MID_KEY))
        try:
            if state.is_migrated(plan.source, message_id, uid):
                progress.skipped += 1
                continue
            # Bodies fetched one at a time and released each iteration, so a
            # 50 MB attachment costs 50 MB, not the whole folder.
            body = src.with_retry(lambda c: c.fetch([uid], [b"BODY.PEEK[]"]))[uid][b"BODY[]"]
            flags = preserved_flags(m.get(b"FLAGS", ()))
            internaldate = m.get(b"INTERNALDATE")
            try:
                dst.with_retry(
                    lambda c: c.append(plan.dest, body, flags=flags, msg_time=internaldate)
                )
            except Exception as exc:
                # Quota classification applies only to the destination APPEND.
                if is_quota_error(exc):
                    state.flush()
                    raise QuotaExceeded(
                        f"Destination mailbox is full — APPEND refused: {exc}"
                    ) from exc
                raise
        except QuotaExceeded:
            raise
        except Exception as exc:
            progress.failed += 1
            progress.failures.append(
                f"{plan.source} uid={uid} message_id={message_id}: {exc}"
            )
        else:
            state.mark_migrated(plan.source, message_id, uid)
            progress.migrated += 1
            # A flush failure here propagates: state persistence is broken and
            # continuing would silently un-dedup every following message.
            state.flush()
        finally:
            if on_message is not None:
                on_message(plan.source, uid)


def migrate(
    src: MailConnection,
    dst: MailConnection,
    plans: list[FolderPlan],
    state: MigrationState,
    on_message: MessageCallback | None = None,
) -> TransferProgress:
    progress = TransferProgress()
    try:
        for plan in plans:
            migrate_folder(src, dst, plan, state, progress, on_message)
    finally:
        state.flush()  # Ctrl-C / crash loses at most the in-flight message
    return progress
