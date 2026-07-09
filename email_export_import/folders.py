from __future__ import annotations

from .models import FolderPlan

# RFC 6154 special-use attributes we match across servers so that, e.g., the
# source's "Sent Messages" lands in the destination's "Gesendet" when both
# advertise \Sent — instead of creating a duplicate sent folder.
SPECIAL_USE_FLAGS = (b"\\Sent", b"\\Drafts", b"\\Trash", b"\\Junk", b"\\Archive")

# Shape of IMAPClient.list_folders(): [(flags, delimiter, name), ...]
Listing = list[tuple[tuple[bytes, ...], bytes, str]]


def translate_path(name: str, src_delim: str, dst_delim: str) -> str:
    """Rewrite a folder path from the source hierarchy delimiter to the
    destination's (e.g. 'Work/Projects' -> 'Work.Projects')."""
    if src_delim == dst_delim:
        return name
    return name.replace(src_delim, dst_delim)


def _delimiter(listing: Listing) -> str:
    for _flags, delim, _name in listing:
        if delim:
            return delim.decode()
    return "/"


def _special_use(flags: tuple[bytes, ...]) -> bytes | None:
    for f in flags:
        if f in SPECIAL_USE_FLAGS:
            return f
    return None


def build_folder_plan(
    src_listing: Listing,
    dst_listing: Listing,
    skip_folders: set[str] = frozenset(),
) -> list[FolderPlan]:
    """Map every selectable source folder onto a destination folder.

    Priority: skip-list > \\Noselect exclusion > SPECIAL-USE match >
    delimiter-translated 1:1 name (created if missing).
    """
    src_delim = _delimiter(src_listing)
    dst_delim = _delimiter(dst_listing)
    dst_names = {name for _f, _d, name in dst_listing}

    dst_by_special: dict[bytes, str] = {}
    for flags, _d, name in dst_listing:
        su = _special_use(flags)
        if su is not None and su not in dst_by_special:
            dst_by_special[su] = name

    plans: list[FolderPlan] = []
    for flags, _d, name in src_listing:
        if name in skip_folders:
            continue
        if b"\\Noselect" in flags:
            continue
        su = _special_use(flags)
        if su is not None and su in dst_by_special:
            plans.append(FolderPlan(source=name, dest=dst_by_special[su], create=False))
            continue
        dest = translate_path(name, src_delim, dst_delim)
        plans.append(FolderPlan(source=name, dest=dest, create=dest not in dst_names))
    return plans
