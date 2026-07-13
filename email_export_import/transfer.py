from __future__ import annotations

import email
import queue
import threading
from typing import Callable

from .connection import MailConnection
from .errors import QuotaExceeded
from .models import FolderPlan, TransferProgress
from .spool import MessageSpool
from .state import MigrationState
from .throttle import RateLimiter

# Flags copied to the destination. \Recent is server-managed (RFC 3501) and
# must never be set by a client.
PRESERVED_FLAGS = (b"\\Seen", b"\\Answered", b"\\Flagged", b"\\Draft", b"\\Deleted")

_QUOTA_MARKERS = ("quota",)

# Fired after every processed message: (source_folder, uid).
MessageCallback = Callable[[str, int], None]

# A folder's UIDs are split into chunks of this size; each chunk is one unit
# of work a worker claims, so a single huge folder still parallelises.
CHUNK_SIZE = 500

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


def _plan_units(
    src: MailConnection,
    dst: MailConnection,
    plans: list[FolderPlan],
    state: MigrationState,
    progress: TransferProgress,
    on_message: MessageCallback | None = None,
) -> tuple[list[tuple[FolderPlan, list[int]]], dict[str, list[int]]]:
    """Serial planning pass: record UIDVALIDITY, create missing destination
    folders, and split every folder's *unfinished* UIDs into work units.

    A rejection here (e.g. namespace-invalid CREATE) fails this folder and
    moves on — one bad folder must not kill the whole run.

    Also returns every folder's full searched UID list, so the end of the run
    can prove nothing fell through (see _verify_complete)."""
    units: list[tuple[FolderPlan, list[int]]] = []
    expected: dict[str, list[int]] = {}
    for plan in plans:
        try:
            info = src.select_folder(plan.source, readonly=True)
            state.set_uidvalidity(plan.source, info[b"UIDVALIDITY"])

            if plan.create:
                # folder_exists guard makes the create idempotent under retry.
                dst.with_retry(
                    lambda c, dest=plan.dest: None
                    if c.folder_exists(dest)
                    else c.create_folder(dest)
                )

            # Roundcube-style webmail lists only SUBSCRIBEd folders — an
            # unsubscribed folder full of migrated mail looks like a failed
            # migration. Best-effort: never fail the folder over it.
            try:
                dst.with_retry(lambda c, dest=plan.dest: c.subscribe_folder(dest))
            except Exception:
                pass

            uids = src.with_retry(lambda c: c.search())
        except Exception as exc:
            progress.failed += 1
            progress.failures.append(f"{plan.source}: {exc}")
            continue

        # Resume fast-path. Messages whose UID is already recorded are done, so
        # drop them here — before any FETCH. Without this, resuming a
        # half-finished folder re-fetches the metadata of every message it
        # already moved (tens of thousands of pointless round-trips) just to
        # re-derive a Message-ID it has already seen. set_uidvalidity() above
        # has already cleared these if the UIDs went stale.
        done = state.migrated_uids(plan.source)
        pending: list[int] = []
        for uid in uids:
            if uid in done:
                progress.skipped += 1
                if on_message is not None:
                    on_message(plan.source, uid)  # keep the progress count honest
            else:
                pending.append(uid)

        expected[plan.source] = list(uids)
        for i in range(0, len(pending), CHUNK_SIZE):
            units.append((plan, pending[i : i + CHUNK_SIZE]))
    return units, expected


def _process_unit(
    src: MailConnection,
    dst: MailConnection,
    plan: FolderPlan,
    uids: list[int],
    state: MigrationState,
    progress: TransferProgress,
    lock: threading.Lock,
    stop: threading.Event,
    on_message: MessageCallback | None,
    spool: MessageSpool | None = None,
    throttle: RateLimiter | None = None,
    failed_uids: set | None = None,
) -> None:
    """Migrate one chunk of UIDs. State and progress mutations are guarded by
    *lock*; network transfers run outside it so workers overlap on the wire."""
    src.select_folder(plan.source, readonly=True)
    # Cheap metadata pass for the chunk — no bodies, memory stays flat.
    meta = src.with_retry(lambda c: c.fetch(uids, _META_FIELDS))

    for uid in uids:
        if stop.is_set():
            return
        m = meta.get(uid)
        if m is None:
            # Vanished between search() and fetch() (expunged mid-run):
            # nothing to copy, but the message was processed — record it so a
            # resume never retries it and the completeness check passes.
            with lock:
                progress.skipped += 1
                state.mark_processed(plan.source, uid)
            if on_message is not None:
                on_message(plan.source, uid)
            continue
        message_id = parse_message_id(m.get(_MID_KEY))
        try:
            with lock:
                already = state.is_migrated(plan.source, message_id, uid)
            if already:
                with lock:
                    # Backfill the UID. States written before UIDs were recorded
                    # hold only Message-IDs, so this first pass is what earns
                    # them the no-FETCH fast-path on the next resume.
                    state.mark_migrated(plan.source, message_id, uid)
                    progress.skipped += 1
                continue
            # Bodies fetched one at a time and released each iteration, so a
            # 50 MB attachment costs 50 MB, not the whole folder.
            spooled = (
                spool.get(plan.source, uid, message_id) if spool is not None else None
            )
            if spooled is not None:
                body = spooled.body
                flags = spooled.flags
                internaldate = spooled.internaldate
            else:
                body = src.with_retry(lambda c: c.fetch([uid], [b"BODY.PEEK[]"]))[uid][b"BODY[]"]
                flags = preserved_flags(m.get(b"FLAGS", ()))
                internaldate = m.get(b"INTERNALDATE")
                if spool is not None:
                    spool.put(plan.source, uid, message_id, body, flags, internaldate)
            if throttle is not None:
                # Pace the upload: this is the write that hits the network hardest.
                throttle.acquire(len(body), cancel=stop)
            try:
                dst.with_retry(
                    lambda c: c.append(plan.dest, body, flags=flags, msg_time=internaldate)
                )
            except Exception as exc:
                # Quota classification applies only to the destination APPEND.
                if is_quota_error(exc):
                    with lock:
                        state.flush()
                    raise QuotaExceeded(
                        f"Destination mailbox is full — APPEND refused: {exc}"
                    ) from exc
                raise
        except QuotaExceeded:
            raise
        except Exception as exc:
            with lock:
                progress.failed += 1
                progress.failures.append(
                    f"{plan.source} uid={uid} message_id={message_id}: {exc}"
                )
                if failed_uids is not None:
                    # Already reported — the completeness check must not
                    # count this UID a second time.
                    failed_uids.add((plan.source, uid))
        else:
            with lock:
                state.mark_migrated(plan.source, message_id, uid)
                progress.migrated += 1
                # A flush failure here propagates: state persistence is broken and
                # continuing would silently un-dedup every following message.
                state.flush()
            if spool is not None:
                spool.discard(plan.source, uid)
        finally:
            if on_message is not None:
                on_message(plan.source, uid)


def migrate(
    src: MailConnection,
    dst: MailConnection,
    plans: list[FolderPlan],
    state: MigrationState,
    on_message: MessageCallback | None = None,
    workers: int = 1,
    cancel: threading.Event | None = None,
    spool: MessageSpool | None = None,
    throttle: RateLimiter | None = None,
) -> TransferProgress:
    progress = TransferProgress()
    lock = threading.Lock()
    failed_uids: set = set()
    stop = cancel if cancel is not None else threading.Event()
    # Make the planning pass and the serial path honour cancellation too (the
    # parallel workers build their own cancel-aware connections below).
    src.set_cancel(stop)
    dst.set_cancel(stop)
    try:
        units, expected = _plan_units(src, dst, plans, state, progress, on_message)
        if not units:
            if not stop.is_set():
                _verify_complete(expected, state, progress)
            return progress

        if workers <= 1:
            for plan, uids in units:
                _process_unit(
                    src, dst, plan, uids, state, progress, lock, stop, on_message,
                    spool, throttle, failed_uids,
                )
            if not stop.is_set():
                _verify_complete(expected, state, progress, failed_uids)
            return progress

        # Each worker owns a private connection pair (IMAP sessions are not
        # shareable across threads) and pulls chunks until the queue drains.
        unit_queue: queue.Queue[tuple[FolderPlan, list[int]]] = queue.Queue()
        for unit in units:
            unit_queue.put(unit)
        errors: list[Exception] = []

        def run_worker() -> None:
            # jitter de-synchronises workers' login-retry backoffs so they don't
            # re-hit a rate-limiting server in lockstep.
            wsrc = MailConnection(src.account, max_retries=src.max_retries, cancel=stop, jitter=0.25)
            wdst = MailConnection(dst.account, max_retries=dst.max_retries, cancel=stop, jitter=0.25)
            try:
                while not stop.is_set():
                    try:
                        plan, uids = unit_queue.get_nowait()
                    except queue.Empty:
                        return
                    _process_unit(
                        wsrc, wdst, plan, uids, state, progress, lock, stop, on_message,
                        spool, throttle, failed_uids,
                    )
            except Exception as exc:
                stop.set()
                with lock:
                    errors.append(exc)
            finally:
                wsrc.close()
                wdst.close()

        threads = [
            threading.Thread(target=run_worker, daemon=True)
            for _ in range(min(workers, len(units)))
        ]
        for t in threads:
            t.start()
        try:
            for t in threads:
                t.join()
        except BaseException:
            stop.set()  # Ctrl-C: workers exit at their next message boundary
            raise
        if errors:
            raise errors[0]
        if not stop.is_set():
            _verify_complete(expected, state, progress, failed_uids)
        return progress
    finally:
        state.flush()  # Ctrl-C / crash loses at most the in-flight messages


def _verify_complete(
    expected: dict[str, list[int]],
    state: MigrationState,
    progress: TransferProgress,
    failed_uids: set | None = None,
) -> None:
    """Belt and braces before a run may call itself finished: every UID the
    planning pass saw must be recorded as handled by now. Anything else means
    messages fell through silently (a flaky SELECT, a dropped unit) — surface
    them as failures so the run shows red instead of a lying green tick."""
    reported = failed_uids or set()
    for folder, uids in expected.items():
        done = state.migrated_uids(folder)
        missing = [
            u for u in uids if u not in done and (folder, u) not in reported
        ]
        if missing:
            progress.failed += len(missing)
            progress.failures.append(
                f"{folder}: {len(missing)} messages were never transferred"
            )
