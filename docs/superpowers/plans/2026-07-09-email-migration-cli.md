# Email Migration CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Interactive Python CLI that migrates a mailbox IMAP→IMAP, preserving folders, flags, dates, and attachments, with resume/dedup state.

**Architecture:** Small focused modules: provider presets → connection wrapper (reconnect/retry) → folder planner (delimiter + SPECIAL-USE mapping) → transfer engine (raw RFC822 copy, per-message error tolerance, quota abort) → JSON resume state (Message-ID dedup, UIDVALIDITY invalidation) → Typer/Rich wizard on top.

**Tech Stack:** Python 3.11+, IMAPClient, Typer, Rich, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-07-09-email-migration-cli-design.md`

## Global Constraints

- Python `>=3.11`; package manager `uv`; build backend `hatchling`.
- Runtime deps exactly: `imapclient>=3.0`, `typer>=0.12`, `rich>=13.7`. Dev dep: `pytest>=8.0`. No other dependencies.
- Distribution name `email-export-import`, import package `email_export_import`, console script `email-export-import`.
- State location: `~/.email-export-import/state/<src-email>__<dst-email>.json`; directory mode `0o700`, file mode `0o600`.
- Passwords: masked prompt or env vars `EEI_SRC_PASSWORD` / `EEI_DST_PASSWORD`. Never in argv, logs, or state.
- Retry count for transient IMAP failures: 3 (`max_retries=3`).
- Preserved flags exactly: `\Seen`, `\Answered`, `\Flagged`, `\Draft`, `\Deleted`. Never `\Recent`.
- All test commands run through `uv run pytest`.
- Commit after every task (each task's final step).

---

### Task 1: Project scaffold, models, errors

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `email_export_import/__init__.py`
- Create: `email_export_import/models.py`
- Create: `email_export_import/errors.py`
- Create: `tests/__init__.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - `models.ProviderPreset(key: str, name: str, host: str, port: int, ssl: bool, app_password_hint: str | None = None, skip_folders: tuple[str, ...] = ())` — frozen dataclass.
  - `models.Account(host: str, port: int, ssl: bool, email: str, password: str)` — mutable dataclass.
  - `models.FolderPlan(source: str, dest: str, create: bool)` — dataclass.
  - `models.TransferProgress(migrated: int = 0, skipped: int = 0, failed: int = 0, failures: list[str] = [])` — dataclass with field factory.
  - `errors.MigrationError`, `errors.ConnectionFailed(MigrationError)`, `errors.AuthFailed(MigrationError)`, `errors.QuotaExceeded(MigrationError)`.

- [ ] **Step 1: Create pyproject.toml**

```toml
[project]
name = "email-export-import"
version = "0.1.0"
description = "Interactive CLI to migrate a mailbox between IMAP servers"
requires-python = ">=3.11"
dependencies = [
    "imapclient>=3.0",
    "typer>=0.12",
    "rich>=13.7",
]

[project.scripts]
email-export-import = "email_export_import.cli:app"

[dependency-groups]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["email_export_import"]
```

- [ ] **Step 2: Create .gitignore**

```gitignore
.venv/
__pycache__/
*.pyc
dist/
.pytest_cache/
```

- [ ] **Step 3: Create empty package files**

Create `email_export_import/__init__.py` and `tests/__init__.py`, both empty.

- [ ] **Step 4: Write the failing test**

Create `tests/test_models.py`:

```python
from email_export_import.errors import (
    AuthFailed,
    ConnectionFailed,
    MigrationError,
    QuotaExceeded,
)
from email_export_import.models import (
    Account,
    FolderPlan,
    ProviderPreset,
    TransferProgress,
)


def test_provider_preset_defaults():
    p = ProviderPreset(key="x", name="X", host="imap.x.com", port=993, ssl=True)
    assert p.app_password_hint is None
    assert p.skip_folders == ()


def test_transfer_progress_defaults_are_independent():
    a = TransferProgress()
    b = TransferProgress()
    a.failures.append("boom")
    assert b.failures == []
    assert (a.migrated, a.skipped, a.failed) == (0, 0, 0)


def test_folder_plan_fields():
    fp = FolderPlan(source="Work/Projects", dest="Work.Projects", create=True)
    assert fp.source == "Work/Projects"
    assert fp.dest == "Work.Projects"
    assert fp.create is True


def test_account_fields():
    a = Account(host="h", port=993, ssl=True, email="a@x", password="p")
    assert a.email == "a@x"


def test_error_hierarchy():
    assert issubclass(ConnectionFailed, MigrationError)
    assert issubclass(AuthFailed, MigrationError)
    assert issubclass(QuotaExceeded, MigrationError)
```

- [ ] **Step 5: Run test to verify it fails**

Run: `uv sync && uv run pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'email_export_import.errors'` (or `.models`).

- [ ] **Step 6: Write models.py**

Create `email_export_import/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderPreset:
    """A known provider's IMAP settings plus UX hints."""

    key: str
    name: str
    host: str
    port: int
    ssl: bool
    app_password_hint: str | None = None
    skip_folders: tuple[str, ...] = ()


@dataclass
class Account:
    """Connection settings for one mailbox."""

    host: str
    port: int
    ssl: bool
    email: str
    password: str


@dataclass
class FolderPlan:
    """One source folder mapped onto a destination folder."""

    source: str
    dest: str
    create: bool


@dataclass
class TransferProgress:
    """Counters accumulated over a migration run."""

    migrated: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)
```

- [ ] **Step 7: Write errors.py**

Create `email_export_import/errors.py`:

```python
class MigrationError(Exception):
    """Base for all migration errors."""


class ConnectionFailed(MigrationError):
    """Could not reach the server or negotiate TLS."""


class AuthFailed(MigrationError):
    """Server rejected the credentials."""


class QuotaExceeded(MigrationError):
    """Destination refused APPEND because the mailbox is full."""
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: 5 PASS.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .gitignore uv.lock email_export_import/ tests/
git commit -m "feat: scaffold project with models and error types"
```

---

### Task 2: Provider presets

**Files:**
- Create: `email_export_import/providers.py`
- Test: `tests/test_providers.py`

**Interfaces:**
- Consumes: `models.ProviderPreset` (Task 1).
- Produces:
  - `providers.PRESETS: dict[str, ProviderPreset]` with keys `"gmail"`, `"outlook"`, `"yahoo"`, `"icloud"`, `"yandex"`.
  - `providers.get_preset(key: str) -> ProviderPreset` — raises `KeyError` on unknown key.
  - `providers.list_presets() -> list[ProviderPreset]` — stable order.

- [ ] **Step 1: Write the failing test**

Create `tests/test_providers.py`:

```python
import pytest

from email_export_import.providers import PRESETS, get_preset, list_presets


def test_all_expected_presets_exist():
    assert set(PRESETS) == {"gmail", "outlook", "yahoo", "icloud", "yandex"}


def test_all_presets_use_ssl_993():
    for p in PRESETS.values():
        assert p.ssl is True
        assert p.port == 993


def test_all_presets_have_app_password_hint():
    for p in PRESETS.values():
        assert p.app_password_hint


def test_gmail_skip_list_covers_duplicating_labels():
    gmail = get_preset("gmail")
    assert "[Gmail]/All Mail" in gmail.skip_folders
    assert "[Gmail]/Important" in gmail.skip_folders
    assert "[Gmail]/Starred" in gmail.skip_folders


def test_hosts():
    assert get_preset("gmail").host == "imap.gmail.com"
    assert get_preset("outlook").host == "outlook.office365.com"
    assert get_preset("yahoo").host == "imap.mail.yahoo.com"
    assert get_preset("icloud").host == "imap.mail.me.com"
    assert get_preset("yandex").host == "imap.yandex.com"


def test_get_preset_unknown_raises():
    with pytest.raises(KeyError):
        get_preset("aol")


def test_list_presets_returns_all():
    assert {p.key for p in list_presets()} == set(PRESETS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_providers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'email_export_import.providers'`.

- [ ] **Step 3: Write providers.py**

Create `email_export_import/providers.py`:

```python
from __future__ import annotations

from .models import ProviderPreset

# Gmail exposes every label as an IMAP folder and [Gmail]/All Mail contains
# every message; migrating those alongside the label folders would duplicate
# each message on the destination. Skipped by default (user-editable).
_GMAIL_SKIP = ("[Gmail]/All Mail", "[Gmail]/Important", "[Gmail]/Starred")

PRESETS: dict[str, ProviderPreset] = {
    "gmail": ProviderPreset(
        key="gmail",
        name="Gmail",
        host="imap.gmail.com",
        port=993,
        ssl=True,
        app_password_hint=(
            "Gmail requires an app password (not your normal password): "
            "https://myaccount.google.com/apppasswords"
        ),
        skip_folders=_GMAIL_SKIP,
    ),
    "outlook": ProviderPreset(
        key="outlook",
        name="Outlook / Office365",
        host="outlook.office365.com",
        port=993,
        ssl=True,
        app_password_hint=(
            "Outlook requires an app password: "
            "https://account.live.com/proofs/AppPassword"
        ),
    ),
    "yahoo": ProviderPreset(
        key="yahoo",
        name="Yahoo Mail",
        host="imap.mail.yahoo.com",
        port=993,
        ssl=True,
        app_password_hint=(
            "Yahoo requires an app password: "
            "https://login.yahoo.com/account/security"
        ),
    ),
    "icloud": ProviderPreset(
        key="icloud",
        name="iCloud Mail",
        host="imap.mail.me.com",
        port=993,
        ssl=True,
        app_password_hint=(
            "iCloud requires an app-specific password: "
            "https://appleid.apple.com/account/manage"
        ),
    ),
    "yandex": ProviderPreset(
        key="yandex",
        name="Yandex Mail",
        host="imap.yandex.com",
        port=993,
        ssl=True,
        app_password_hint=(
            "Yandex requires an app password: "
            "https://id.yandex.com/security/app-passwords"
        ),
    ),
}


def get_preset(key: str) -> ProviderPreset:
    """Return the preset for *key*; raises KeyError for unknown providers."""
    return PRESETS[key]


def list_presets() -> list[ProviderPreset]:
    """All presets in stable (insertion) order."""
    return list(PRESETS.values())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_providers.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add email_export_import/providers.py tests/test_providers.py
git commit -m "feat: add provider preset registry with app-password hints"
```

---

### Task 3: Resume state

**Files:**
- Create: `email_export_import/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: nothing from other modules (stdlib only).
- Produces:
  - `state.MigrationState(path: Path)` — loads existing JSON if present.
  - `MigrationState.for_pair(src_email: str, dst_email: str, base_dir: Path | None = None) -> MigrationState` — classmethod; creates `<base>/state/` with mode `0o700`; default base `~/.email-export-import`.
  - `MigrationState.set_uidvalidity(folder: str, uidvalidity: int) -> None` — if the stored value differs, discards that folder's UID set (Message-IDs survive).
  - `MigrationState.is_migrated(folder: str, message_id: str | None, uid: int) -> bool` — Message-ID lookup when present, else UID lookup.
  - `MigrationState.mark_migrated(folder: str, message_id: str | None, uid: int) -> None`
  - `MigrationState.flush() -> None` — writes JSON, file mode `0o600`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_state.py`:

```python
from email_export_import.state import MigrationState


def test_mark_and_lookup_by_message_id(tmp_path):
    s = MigrationState(tmp_path / "s.json")
    assert not s.is_migrated("INBOX", "<a@x>", 1)
    s.mark_migrated("INBOX", "<a@x>", 1)
    assert s.is_migrated("INBOX", "<a@x>", 1)
    # Same Message-ID, different UID: still migrated (dedup is by Message-ID).
    assert s.is_migrated("INBOX", "<a@x>", 999)


def test_mark_and_lookup_by_uid_when_no_message_id(tmp_path):
    s = MigrationState(tmp_path / "s.json")
    s.mark_migrated("INBOX", None, 7)
    assert s.is_migrated("INBOX", None, 7)
    assert not s.is_migrated("INBOX", None, 8)


def test_folders_are_isolated(tmp_path):
    s = MigrationState(tmp_path / "s.json")
    s.mark_migrated("INBOX", "<a@x>", 1)
    assert not s.is_migrated("Sent", "<a@x>", 1)


def test_uidvalidity_change_discards_uids_keeps_message_ids(tmp_path):
    s = MigrationState(tmp_path / "s.json")
    s.set_uidvalidity("INBOX", 100)
    s.mark_migrated("INBOX", "<a@x>", 10)
    s.mark_migrated("INBOX", None, 11)
    s.set_uidvalidity("INBOX", 200)  # server regenerated UIDs
    assert s.is_migrated("INBOX", "<a@x>", 10)  # Message-ID survives
    assert not s.is_migrated("INBOX", None, 11)  # UID entry discarded


def test_same_uidvalidity_keeps_uids(tmp_path):
    s = MigrationState(tmp_path / "s.json")
    s.set_uidvalidity("INBOX", 100)
    s.mark_migrated("INBOX", None, 11)
    s.set_uidvalidity("INBOX", 100)
    assert s.is_migrated("INBOX", None, 11)


def test_flush_and_reload_roundtrip(tmp_path):
    path = tmp_path / "s.json"
    s = MigrationState(path)
    s.set_uidvalidity("INBOX", 100)
    s.mark_migrated("INBOX", "<a@x>", 1)
    s.mark_migrated("INBOX", None, 2)
    s.flush()

    s2 = MigrationState(path)
    assert s2.is_migrated("INBOX", "<a@x>", 1)
    assert s2.is_migrated("INBOX", None, 2)
    s2.set_uidvalidity("INBOX", 100)  # unchanged -> UIDs kept
    assert s2.is_migrated("INBOX", None, 2)


def test_for_pair_creates_secure_paths(tmp_path):
    s = MigrationState.for_pair("a@x.com", "b@y.com", base_dir=tmp_path / "base")
    s.flush()
    state_dir = tmp_path / "base" / "state"
    assert s.path == state_dir / "a@x.com__b@y.com.json"
    assert (state_dir.stat().st_mode & 0o777) == 0o700
    assert (s.path.stat().st_mode & 0o777) == 0o600
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'email_export_import.state'`.

- [ ] **Step 3: Write state.py**

Create `email_export_import/state.py`:

```python
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_BASE_DIR = Path.home() / ".email-export-import"


class MigrationState:
    """Per (source, destination) resume state.

    Records, per source folder, which messages already landed on the
    destination — by Message-ID when the message has one, by source UID
    otherwise. UID entries are only trusted while the folder's UIDVALIDITY
    is unchanged (RFC 3501: a UIDVALIDITY bump means all old UIDs are void).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        # {folder: {"uidvalidity": int|None, "message_ids": set[str], "uids": set[int]}}
        self._folders: dict[str, dict] = {}
        if path.exists():
            raw = json.loads(path.read_text())
            for name, f in raw.get("folders", {}).items():
                self._folders[name] = {
                    "uidvalidity": f["uidvalidity"],
                    "message_ids": set(f["message_ids"]),
                    "uids": set(f["uids"]),
                }

    @classmethod
    def for_pair(
        cls, src_email: str, dst_email: str, base_dir: Path | None = None
    ) -> "MigrationState":
        base = base_dir or DEFAULT_BASE_DIR
        state_dir = base / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o700)
        os.chmod(state_dir, 0o700)
        return cls(state_dir / f"{src_email}__{dst_email}.json")

    def _folder(self, folder: str) -> dict:
        return self._folders.setdefault(
            folder, {"uidvalidity": None, "message_ids": set(), "uids": set()}
        )

    def set_uidvalidity(self, folder: str, uidvalidity: int) -> None:
        f = self._folder(folder)
        if f["uidvalidity"] is not None and f["uidvalidity"] != uidvalidity:
            f["uids"] = set()  # old-generation UIDs are meaningless now
        f["uidvalidity"] = uidvalidity

    def is_migrated(self, folder: str, message_id: str | None, uid: int) -> bool:
        f = self._folder(folder)
        if message_id is not None:
            return message_id in f["message_ids"]
        return uid in f["uids"]

    def mark_migrated(self, folder: str, message_id: str | None, uid: int) -> None:
        f = self._folder(folder)
        if message_id is not None:
            f["message_ids"].add(message_id)
        else:
            f["uids"].add(uid)

    def flush(self) -> None:
        raw = {
            "folders": {
                name: {
                    "uidvalidity": f["uidvalidity"],
                    "message_ids": sorted(f["message_ids"]),
                    "uids": sorted(f["uids"]),
                }
                for name, f in self._folders.items()
            }
        }
        self.path.write_text(json.dumps(raw))
        os.chmod(self.path, 0o600)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_state.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add email_export_import/state.py tests/test_state.py
git commit -m "feat: add resume state with Message-ID dedup and UIDVALIDITY invalidation"
```

---

### Task 4: Folder planner

**Files:**
- Create: `email_export_import/folders.py`
- Test: `tests/test_folders.py`

**Interfaces:**
- Consumes: `models.FolderPlan` (Task 1).
- Produces:
  - `folders.translate_path(name: str, src_delim: str, dst_delim: str) -> str`
  - `folders.build_folder_plan(src_listing, dst_listing, skip_folders: set[str] = frozenset()) -> list[FolderPlan]`
  - Listings are what `IMAPClient.list_folders()` returns: `list[tuple[tuple[bytes, ...], bytes, str]]` = `(flags, delimiter, name)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_folders.py`:

```python
from email_export_import.folders import build_folder_plan, translate_path


def listing(*rows):
    """rows: (name, flags_tuple, delim). Returns IMAPClient-shaped listing."""
    return [(flags, delim.encode(), name) for name, flags, delim in rows]


def test_translate_path_between_delimiters():
    assert translate_path("Work/Projects/2026", "/", ".") == "Work.Projects.2026"
    assert translate_path("INBOX", "/", ".") == "INBOX"
    assert translate_path("A.B", ".", ".") == "A.B"


def test_plan_creates_missing_folders_with_translated_names():
    src = listing(("INBOX", (), "/"), ("Work/Projects", (), "/"))
    dst = listing(("INBOX", (), "."))
    plans = build_folder_plan(src, dst)
    by_source = {p.source: p for p in plans}
    assert by_source["INBOX"].dest == "INBOX"
    assert by_source["INBOX"].create is False
    assert by_source["Work/Projects"].dest == "Work.Projects"
    assert by_source["Work/Projects"].create is True


def test_special_use_maps_to_destination_equivalent():
    src = listing(("Sent Messages", (b"\\Sent",), "/"))
    dst = listing(("Gesendet", (b"\\Sent",), "/"))
    plans = build_folder_plan(src, dst)
    assert plans == [type(plans[0])(source="Sent Messages", dest="Gesendet", create=False)]


def test_special_use_without_dest_match_falls_back_to_name():
    src = listing(("Sent Messages", (b"\\Sent",), "/"))
    dst = listing(("INBOX", (), "/"))
    plans = build_folder_plan(src, dst)
    assert plans[0].dest == "Sent Messages"
    assert plans[0].create is True


def test_skip_folders_and_noselect_are_excluded():
    src = listing(
        ("INBOX", (), "/"),
        ("[Gmail]/All Mail", (b"\\All",), "/"),
        ("[Gmail]", (b"\\Noselect",), "/"),
    )
    dst = listing(("INBOX", (), "/"))
    plans = build_folder_plan(src, dst, skip_folders={"[Gmail]/All Mail"})
    assert [p.source for p in plans] == ["INBOX"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_folders.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'email_export_import.folders'`.

- [ ] **Step 3: Write folders.py**

Create `email_export_import/folders.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_folders.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add email_export_import/folders.py tests/test_folders.py
git commit -m "feat: add folder planner with delimiter translation and SPECIAL-USE mapping"
```

---

### Task 5: FakeIMAPClient test double

**Files:**
- Create: `tests/fakes.py`
- Test: `tests/test_fakes.py`

**Interfaces:**
- Consumes: nothing from the package (pure test infrastructure).
- Produces (used by Tasks 6–9): `tests.fakes.FakeIMAPClient` implementing the IMAPClient subset the tool uses, with IMAPClient-shaped return values:
  - `FakeIMAPClient(folders: dict[str, list[dict]] | None = None, special_use: dict[str, bytes] | None = None, delimiter: str = "/", uidvalidity: int = 1)`
  - Message dicts: `{"uid": int, "flags": tuple[bytes, ...], "internaldate": datetime, "body": bytes}`
  - Methods: `login`, `list_folders`, `select_folder`, `search`, `fetch`, `append`, `create_folder`, `folder_exists`, `folder_status`, `logout`.
  - Failure injection: `.append_error: Exception | None` (raised by next `append` calls while set), `.login_error: Exception | None`.
  - Helper: `tests.fakes.make_message(uid: int, message_id: str | None, subject: str = "hi", flags: tuple[bytes, ...] = (), internaldate: datetime | None = None, attachment: bytes | None = None) -> dict`

- [ ] **Step 1: Write the failing test**

Create `tests/test_fakes.py`:

```python
from datetime import datetime

from tests.fakes import FakeIMAPClient, make_message


def test_fetch_returns_imapclient_shaped_metadata():
    msg = make_message(uid=1, message_id="<a@x>", flags=(b"\\Seen",),
                       internaldate=datetime(2020, 1, 2, 3, 4, 5))
    fake = FakeIMAPClient(folders={"INBOX": [msg]})
    fake.login("u", "p")
    info = fake.select_folder("INBOX")
    assert info[b"UIDVALIDITY"] == 1
    assert fake.search() == [1]

    meta = fake.fetch([1], [b"FLAGS", b"INTERNALDATE", b"RFC822.SIZE",
                           b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
    entry = meta[1]
    assert entry[b"FLAGS"] == (b"\\Seen",)
    assert entry[b"INTERNALDATE"] == datetime(2020, 1, 2, 3, 4, 5)
    assert b"<a@x>" in entry[b"BODY[HEADER.FIELDS (MESSAGE-ID)]"]

    body = fake.fetch([1], [b"BODY.PEEK[]"])[1][b"BODY[]"]
    assert b"Message-ID: <a@x>" in body


def test_append_stores_message_with_flags_and_time():
    fake = FakeIMAPClient(folders={"INBOX": []})
    when = datetime(2021, 6, 1)
    fake.append("INBOX", b"raw-message", flags=(b"\\Seen",), msg_time=when)
    fake.select_folder("INBOX")
    uid = fake.search()[0]
    got = fake.fetch([uid], [b"FLAGS", b"INTERNALDATE", b"BODY.PEEK[]"])[uid]
    assert got[b"FLAGS"] == (b"\\Seen",)
    assert got[b"INTERNALDATE"] == when
    assert got[b"BODY[]"] == b"raw-message"


def test_create_folder_and_exists():
    fake = FakeIMAPClient()
    assert not fake.folder_exists("Archive")
    fake.create_folder("Archive")
    assert fake.folder_exists("Archive")


def test_append_error_injection():
    fake = FakeIMAPClient(folders={"INBOX": []})
    fake.append_error = RuntimeError("boom")
    try:
        fake.append("INBOX", b"x")
        raise AssertionError("should have raised")
    except RuntimeError:
        pass


def test_message_without_message_id():
    msg = make_message(uid=5, message_id=None)
    fake = FakeIMAPClient(folders={"INBOX": [msg]})
    fake.select_folder("INBOX")
    blob = fake.fetch([5], [b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])[5]
    assert b"Message-ID" not in blob[b"BODY[HEADER.FIELDS (MESSAGE-ID)]"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fakes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.fakes'`.

- [ ] **Step 3: Write fakes.py**

Create `tests/fakes.py`:

```python
"""In-memory IMAPClient double.

Implements exactly the subset of IMAPClient the tool touches, returning
values in IMAPClient's shapes (bytes keys, (flags, delim, name) listings).
"""
from __future__ import annotations

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


class FakeIMAPClient:
    def __init__(
        self,
        folders: dict[str, list[dict]] | None = None,
        special_use: dict[str, bytes] | None = None,
        delimiter: str = "/",
        uidvalidity: int = 1,
    ) -> None:
        self.folders = {n: list(m) for n, m in (folders or {"INBOX": []}).items()}
        self.special_use = special_use or {}
        self.delimiter = delimiter
        self.uidvalidity = uidvalidity
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
                "internaldate": msg_time,
                "body": msg,
            }
        )

    @staticmethod
    def _mid_blob(body: bytes) -> bytes:
        mid = email.message_from_bytes(body).get("Message-ID")
        if not mid:
            return b"\r\n"
        return f"Message-ID: {mid}\r\n\r\n".encode()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fakes.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/fakes.py tests/test_fakes.py
git commit -m "test: add in-memory FakeIMAPClient double"
```

---

### Task 6: Connection wrapper

**Files:**
- Create: `email_export_import/connection.py`
- Test: `tests/test_connection.py`

**Interfaces:**
- Consumes: `models.Account`, `errors.ConnectionFailed`, `errors.AuthFailed` (Task 1); `tests.fakes.FakeIMAPClient` (Task 5, tests only).
- Produces:
  - `connection.MailConnection(account: Account, max_retries: int = 3)`
  - `MailConnection.connect() -> IMAPClient` — raises `ConnectionFailed` / `AuthFailed` with human messages; re-selects the previously selected folder after reconnect.
  - `MailConnection.client: IMAPClient` — property; lazily connects.
  - `MailConnection.select_folder(folder: str, readonly: bool = False) -> dict` — retried; remembers selection for reconnects.
  - `MailConnection.with_retry(fn: Callable[[IMAPClient], T]) -> T` — up to `max_retries` attempts; on `IMAPClientError`/`OSError` drops the session and reconnects; exponential backoff via `time.sleep`.
  - `MailConnection.close() -> None` — best-effort logout.

- [ ] **Step 1: Write the failing test**

Create `tests/test_connection.py`:

```python
import socket

import pytest
from imapclient.exceptions import IMAPClientError, LoginError

from email_export_import import connection
from email_export_import.connection import MailConnection
from email_export_import.errors import AuthFailed, ConnectionFailed
from email_export_import.models import Account
from tests.fakes import FakeIMAPClient

ACCOUNT = Account(host="imap.test", port=993, ssl=True, email="a@x", password="p")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)


def install_factory(monkeypatch, clients):
    """Monkeypatch connection.IMAPClient with a factory popping from *clients*."""
    calls = []

    def factory(host, port=993, ssl=True):
        calls.append((host, port, ssl))
        client = clients.pop(0)
        if isinstance(client, Exception):
            raise client
        return client

    monkeypatch.setattr(connection, "IMAPClient", factory)
    return calls


def test_connect_success(monkeypatch):
    fake = FakeIMAPClient()
    install_factory(monkeypatch, [fake])
    conn = MailConnection(ACCOUNT)
    conn.connect()
    assert fake.logged_in


def test_unreachable_host_raises_connection_failed(monkeypatch):
    install_factory(monkeypatch, [socket.gaierror("no such host")])
    conn = MailConnection(ACCOUNT)
    with pytest.raises(ConnectionFailed) as exc:
        conn.connect()
    assert "imap.test" in str(exc.value)


def test_bad_login_raises_auth_failed(monkeypatch):
    fake = FakeIMAPClient()
    fake.login_error = LoginError("AUTHENTICATIONFAILED")
    install_factory(monkeypatch, [fake])
    conn = MailConnection(ACCOUNT)
    with pytest.raises(AuthFailed) as exc:
        conn.connect()
    assert "app password" in str(exc.value)


def test_with_retry_reconnects_and_reselects(monkeypatch):
    broken = FakeIMAPClient(folders={"INBOX": []})
    healthy = FakeIMAPClient(folders={"INBOX": []})
    install_factory(monkeypatch, [broken, healthy])
    conn = MailConnection(ACCOUNT)
    conn.select_folder("INBOX")

    attempts = []

    def flaky(client):
        attempts.append(client)
        if client is broken:
            raise IMAPClientError("connection dropped")
        return "ok"

    assert conn.with_retry(flaky) == "ok"
    assert attempts == [broken, healthy]
    assert healthy.select_calls == ["INBOX"]  # reselected after reconnect


def test_with_retry_gives_up_after_max_retries(monkeypatch):
    fakes = [FakeIMAPClient() for _ in range(4)]
    install_factory(monkeypatch, list(fakes))
    conn = MailConnection(ACCOUNT, max_retries=3)

    def always_fails(client):
        raise IMAPClientError("still broken")

    with pytest.raises(IMAPClientError):
        conn.with_retry(always_fails)


def test_close_is_idempotent(monkeypatch):
    fake = FakeIMAPClient()
    install_factory(monkeypatch, [fake])
    conn = MailConnection(ACCOUNT)
    conn.connect()
    conn.close()
    conn.close()
    assert not fake.logged_in
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_connection.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'email_export_import.connection'`.

- [ ] **Step 3: Write connection.py**

Create `email_export_import/connection.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_connection.py -v`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add email_export_import/connection.py tests/test_connection.py
git commit -m "feat: add connection wrapper with friendly errors and reconnect-retry"
```

---

### Task 7: Transfer engine

**Files:**
- Create: `email_export_import/transfer.py`
- Test: `tests/test_transfer.py`

**Interfaces:**
- Consumes: `MailConnection` (Task 6), `MigrationState` (Task 3), `FolderPlan`, `TransferProgress` (Task 1), `QuotaExceeded` (Task 1), `FakeIMAPClient`/`make_message` (Task 5, tests only).
- Produces:
  - `transfer.parse_message_id(header_blob: bytes | None) -> str | None`
  - `transfer.preserved_flags(flags: tuple[bytes, ...]) -> tuple[bytes, ...]`
  - `transfer.is_quota_error(exc: Exception) -> bool`
  - `transfer.migrate_folder(src: MailConnection, dst: MailConnection, plan: FolderPlan, state: MigrationState, progress: TransferProgress, on_message: Callable[[str, int], None] | None = None) -> None`
  - `transfer.migrate(src: MailConnection, dst: MailConnection, plans: list[FolderPlan], state: MigrationState, on_message: Callable[[str, int], None] | None = None) -> TransferProgress`
  - `on_message(folder, uid)` fires after every processed message (migrated, skipped, or failed) — the CLI uses it to advance the progress bar.

- [ ] **Step 1: Write the failing test**

Create `tests/test_transfer.py`:

```python
from datetime import datetime

import pytest
from imapclient.exceptions import IMAPClientError

from email_export_import import connection
from email_export_import.connection import MailConnection
from email_export_import.errors import QuotaExceeded
from email_export_import.models import Account, FolderPlan
from email_export_import.state import MigrationState
from email_export_import.transfer import (
    is_quota_error,
    migrate,
    parse_message_id,
    preserved_flags,
)
from tests.fakes import FakeIMAPClient, make_message

SRC = Account(host="src.test", port=993, ssl=True, email="a@x", password="p")
DST = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)


def make_conns(monkeypatch, src_fake, dst_fake):
    def factory(host, port=993, ssl=True):
        return src_fake if host == "src.test" else dst_fake

    monkeypatch.setattr(connection, "IMAPClient", factory)
    return MailConnection(SRC), MailConnection(DST)


def test_parse_message_id():
    assert parse_message_id(b"Message-ID: <a@x>\r\n\r\n") == "<a@x>"
    assert parse_message_id(b"\r\n") is None
    assert parse_message_id(None) is None


def test_preserved_flags_filters_recent_and_unknown():
    got = preserved_flags((b"\\Seen", b"\\Recent", b"\\Flagged", b"$Custom"))
    assert got == (b"\\Seen", b"\\Flagged")


def test_is_quota_error():
    assert is_quota_error(IMAPClientError("APPEND failed: [OVERQUOTA] full"))
    assert is_quota_error(RuntimeError("Quota exceeded"))
    assert not is_quota_error(RuntimeError("parse error"))


def test_roundtrip_preserves_body_flags_date(monkeypatch, tmp_path):
    when = datetime(2019, 5, 5, 12, 0, 0)
    msg = make_message(uid=1, message_id="<m1@x>", flags=(b"\\Seen", b"\\Recent"),
                       internaldate=when)
    src_fake = FakeIMAPClient(folders={"INBOX": [msg]})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state = MigrationState(tmp_path / "s.json")

    progress = migrate(src, dst, [FolderPlan("INBOX", "INBOX", create=False)], state)

    assert (progress.migrated, progress.skipped, progress.failed) == (1, 0, 0)
    stored = dst_fake.folders["INBOX"][0]
    assert stored["body"] == msg["body"]          # raw copy, byte-identical
    assert stored["flags"] == (b"\\Seen",)         # \Recent dropped
    assert stored["internaldate"] == when


def test_rerun_skips_everything(monkeypatch, tmp_path):
    msgs = [make_message(uid=i, message_id=f"<m{i}@x>") for i in (1, 2)]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    plans = [FolderPlan("INBOX", "INBOX", create=False)]

    state = MigrationState(tmp_path / "s.json")
    first = migrate(src, dst, plans, state)
    assert first.migrated == 2

    state2 = MigrationState(tmp_path / "s.json")  # fresh load, same file
    second = migrate(src, dst, plans, state2)
    assert (second.migrated, second.skipped) == (0, 2)
    assert len(dst_fake.folders["INBOX"]) == 2  # no duplicates


def test_message_without_id_uses_uid_dedup(monkeypatch, tmp_path):
    msg = make_message(uid=42, message_id=None)
    src_fake = FakeIMAPClient(folders={"INBOX": [msg]})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    plans = [FolderPlan("INBOX", "INBOX", create=False)]

    state = MigrationState(tmp_path / "s.json")
    assert migrate(src, dst, plans, state).migrated == 1
    state2 = MigrationState(tmp_path / "s.json")
    assert migrate(src, dst, plans, state2).skipped == 1


def test_missing_dest_folder_is_created(monkeypatch, tmp_path):
    src_fake = FakeIMAPClient(folders={"Work/P": [make_message(uid=1, message_id="<a@x>")]})
    dst_fake = FakeIMAPClient(folders={"INBOX": []}, delimiter=".")
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state = MigrationState(tmp_path / "s.json")

    migrate(src, dst, [FolderPlan("Work/P", "Work.P", create=True)], state)
    assert dst_fake.folder_exists("Work.P")
    assert len(dst_fake.folders["Work.P"]) == 1


def test_single_bad_message_does_not_kill_run(monkeypatch, tmp_path):
    msgs = [make_message(uid=1, message_id="<a@x>"), make_message(uid=2, message_id="<b@x>")]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})

    real_append = dst_fake.append
    def flaky_append(folder, body, flags=(), msg_time=None):
        if b"<a@x>" in body:
            raise IMAPClientError("message rejected")
        return real_append(folder, body, flags=flags, msg_time=msg_time)
    dst_fake.append = flaky_append

    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state = MigrationState(tmp_path / "s.json")
    progress = migrate(src, dst, [FolderPlan("INBOX", "INBOX", create=False)], state)

    assert progress.migrated == 1
    assert progress.failed == 1
    assert "<a@x>" in progress.failures[0]


def test_quota_error_aborts_run(monkeypatch, tmp_path):
    msgs = [make_message(uid=i, message_id=f"<m{i}@x>") for i in (1, 2, 3)]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    dst_fake.append_error = IMAPClientError("APPEND failed [OVERQUOTA]")
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state = MigrationState(tmp_path / "s.json")

    with pytest.raises(QuotaExceeded):
        migrate(src, dst, [FolderPlan("INBOX", "INBOX", create=False)], state)


def test_on_message_fires_for_every_processed_message(monkeypatch, tmp_path):
    msgs = [make_message(uid=i, message_id=f"<m{i}@x>") for i in (1, 2)]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state = MigrationState(tmp_path / "s.json")

    seen = []
    migrate(src, dst, [FolderPlan("INBOX", "INBOX", create=False)], state,
            on_message=lambda folder, uid: seen.append((folder, uid)))
    assert seen == [("INBOX", 1), ("INBOX", 2)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transfer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'email_export_import.transfer'`.

- [ ] **Step 3: Write transfer.py**

Create `email_export_import/transfer.py`:

```python
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
            dst.with_retry(
                lambda c: c.append(plan.dest, body, flags=flags, msg_time=internaldate)
            )
            state.mark_migrated(plan.source, message_id, uid)
            progress.migrated += 1
            state.flush()
        except Exception as exc:
            if is_quota_error(exc):
                state.flush()
                raise QuotaExceeded(
                    f"Destination mailbox is full — APPEND refused: {exc}"
                ) from exc
            progress.failed += 1
            progress.failures.append(
                f"{plan.source} uid={uid} message_id={message_id}: {exc}"
            )
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_transfer.py -v`
Expected: 9 PASS.

Note: `test_quota_error_aborts_run` exercises the retry path — `with_retry` attempts the APPEND 3 times before the quota error surfaces; the autouse `no_sleep` fixture keeps this instant.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest -v`
Expected: all tests from Tasks 1–7 PASS.

- [ ] **Step 6: Commit**

```bash
git add email_export_import/transfer.py tests/test_transfer.py
git commit -m "feat: add transfer engine with dedup, quota abort, per-message error tolerance"
```

---

### Task 8: CLI wizard

**Files:**
- Create: `email_export_import/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7. Exact call made by the CLI into the engine: `migrate(src_conn, dst_conn, plans, state, on_message=...)`; plans from `build_folder_plan(src_listing, dst_listing, skip_set)`; state from `MigrationState.for_pair(src_email, dst_email, base_dir=state_dir)`.
- Produces:
  - `cli.app` — Typer application (console-script entry point).
  - CLI options: `--src-preset/--src-host/--src-port/--src-ssl|--no-src-ssl/--src-email`, same with `dst`, `--skip` (comma-separated, overrides preset default), `--yes` (non-interactive), `--state-dir` (override state base dir).
  - Passwords read from `EEI_SRC_PASSWORD` / `EEI_DST_PASSWORD`, falling back to a masked prompt.
  - Exit codes: 0 success, 1 error (connect failure in non-interactive mode, quota), 130 on Ctrl-C.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli.py`:

```python
from typer.testing import CliRunner

from email_export_import import connection
from email_export_import.cli import app
from tests.fakes import FakeIMAPClient, make_message

runner = CliRunner()


def install_hosts(monkeypatch, by_host):
    def factory(host, port=993, ssl=True):
        return by_host[host]

    monkeypatch.setattr(connection, "IMAPClient", factory)


def base_args(extra=()):
    return [
        "--src-host", "src.test", "--src-email", "a@x.com",
        "--dst-host", "dst.test", "--dst-email", "b@y.com",
        "--yes",
        *extra,
    ]


def test_non_interactive_end_to_end(monkeypatch, tmp_path):
    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<a@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path)]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )

    assert result.exit_code == 0, result.output
    assert len(dst.folders["INBOX"]) == 1
    assert "Migrated" in result.output


def test_missing_password_env_prompts_are_avoided_with_env(monkeypatch, tmp_path):
    src = FakeIMAPClient(folders={"INBOX": []})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path)]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 0, result.output
    assert "password" not in result.output.lower()  # no prompt leaked


def test_skip_option_excludes_folder(monkeypatch, tmp_path):
    src = FakeIMAPClient(folders={
        "INBOX": [make_message(uid=1, message_id="<a@x>")],
        "Noise": [make_message(uid=2, message_id="<b@x>")],
    })
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path), "--skip", "Noise"]),
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 0, result.output
    assert not dst.folder_exists("Noise")


def test_auth_failure_non_interactive_exits_1(monkeypatch, tmp_path):
    from imapclient.exceptions import LoginError

    src = FakeIMAPClient()
    src.login_error = LoginError("AUTHENTICATIONFAILED")
    dst = FakeIMAPClient()
    install_hosts(monkeypatch, {"src.test": src, "dst.test": dst})

    result = runner.invoke(
        app,
        base_args(["--state-dir", str(tmp_path)]),
        env={"EEI_SRC_PASSWORD": "bad", "EEI_DST_PASSWORD": "p2"},
    )
    assert result.exit_code == 1
    assert "rejected the login" in result.output


def test_preset_fills_host(monkeypatch, tmp_path):
    gmail = FakeIMAPClient(folders={"INBOX": []})
    dst = FakeIMAPClient(folders={"INBOX": []})
    install_hosts(monkeypatch, {"imap.gmail.com": gmail, "dst.test": dst})

    result = runner.invoke(
        app,
        [
            "--src-preset", "gmail", "--src-email", "a@gmail.com",
            "--dst-host", "dst.test", "--dst-email", "b@y.com",
            "--yes", "--state-dir", str(tmp_path),
        ],
        env={"EEI_SRC_PASSWORD": "p1", "EEI_DST_PASSWORD": "p2"},
    )
    # The factory only maps "imap.gmail.com" — a zero exit code proves the
    # preset filled in the host (any other host would KeyError inside invoke).
    assert result.exit_code == 0, result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'email_export_import.cli'`.

- [ ] **Step 3: Write cli.py**

Create `email_export_import/cli.py`:

```python
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from .connection import MailConnection
from .errors import MigrationError, QuotaExceeded
from .folders import build_folder_plan
from .models import Account, ProviderPreset
from .providers import get_preset, list_presets
from .state import MigrationState
from .transfer import migrate

SRC_PASSWORD_ENV = "EEI_SRC_PASSWORD"
DST_PASSWORD_ENV = "EEI_DST_PASSWORD"

app = typer.Typer(add_completion=False)
console = Console()


def _choose_preset(role: str) -> ProviderPreset | None:
    """Interactive preset menu. Returns None for Custom."""
    presets = list_presets()
    table = Table(title=f"{role} provider")
    table.add_column("#", justify="right")
    table.add_column("Provider")
    table.add_column("Server")
    for i, p in enumerate(presets, 1):
        table.add_row(str(i), p.name, f"{p.host}:{p.port}")
    table.add_row(str(len(presets) + 1), "Custom", "enter host/port manually")
    console.print(table)
    choice = IntPrompt.ask("Choose", default=1)
    if 1 <= choice <= len(presets):
        return presets[choice - 1]
    return None


def _gather_account(
    role: str,
    preset_key: Optional[str],
    host: Optional[str],
    port: Optional[int],
    ssl: bool,
    email_addr: Optional[str],
    password_env: str,
) -> tuple[Account, ProviderPreset | None]:
    preset: ProviderPreset | None = None
    if preset_key is not None:
        preset = get_preset(preset_key)
    elif host is None:
        preset = _choose_preset(role)

    if preset is not None:
        host = host or preset.host
        port = port or preset.port
        ssl = preset.ssl
        if preset.app_password_hint:
            console.print(f"[yellow]{preset.app_password_hint}[/yellow]")

    if host is None:
        host = Prompt.ask(f"{role} IMAP host")
    if port is None:
        port = IntPrompt.ask(f"{role} IMAP port", default=993)
    if email_addr is None:
        email_addr = Prompt.ask(f"{role} email address")
    password = os.environ.get(password_env) or Prompt.ask(
        f"{role} password", password=True
    )
    return (
        Account(host=host, port=port, ssl=ssl, email=email_addr, password=password),
        preset,
    )


def _connect(account: Account, role: str, interactive: bool) -> MailConnection:
    while True:
        conn = MailConnection(account)
        try:
            conn.connect()
            console.print(f"[green]{role}: connected to {account.host} as {account.email}[/green]")
            return conn
        except MigrationError as exc:
            console.print(f"[red]{exc}[/red]")
            if not interactive or not Confirm.ask("Edit connection details and retry?"):
                raise typer.Exit(code=1)
            account.host = Prompt.ask("IMAP host", default=account.host)
            account.port = IntPrompt.ask("IMAP port", default=account.port)
            account.email = Prompt.ask("Email address", default=account.email)
            account.password = Prompt.ask("Password", password=True)


def _folder_counts(conn: MailConnection, names: list[str]) -> dict[str, int]:
    counts = {}
    for name in names:
        try:
            counts[name] = conn.client.folder_status(name, [b"MESSAGES"])[b"MESSAGES"]
        except Exception:
            counts[name] = 0
    return counts


@app.command()
def run(
    src_preset: Optional[str] = typer.Option(None, "--src-preset", help="gmail|outlook|yahoo|icloud|yandex"),
    src_host: Optional[str] = typer.Option(None, "--src-host"),
    src_port: Optional[int] = typer.Option(None, "--src-port"),
    src_ssl: bool = typer.Option(True, "--src-ssl/--no-src-ssl"),
    src_email: Optional[str] = typer.Option(None, "--src-email"),
    dst_preset: Optional[str] = typer.Option(None, "--dst-preset", help="gmail|outlook|yahoo|icloud|yandex"),
    dst_host: Optional[str] = typer.Option(None, "--dst-host"),
    dst_port: Optional[int] = typer.Option(None, "--dst-port"),
    dst_ssl: bool = typer.Option(True, "--dst-ssl/--no-dst-ssl"),
    dst_email: Optional[str] = typer.Option(None, "--dst-email"),
    skip: Optional[str] = typer.Option(None, "--skip", help="Comma-separated source folders to skip (overrides preset default)"),
    yes: bool = typer.Option(False, "--yes", help="No prompts; fail instead of asking"),
    state_dir: Optional[Path] = typer.Option(None, "--state-dir", help="Override state directory (default ~/.email-export-import)"),
) -> None:
    """Migrate a mailbox from one IMAP server to another."""
    interactive = not yes

    src_account, src_preset_obj = _gather_account(
        "Source", src_preset, src_host, src_port, src_ssl, src_email, SRC_PASSWORD_ENV
    )
    src_conn = _connect(src_account, "Source", interactive)

    dst_account, _ = _gather_account(
        "Destination", dst_preset, dst_host, dst_port, dst_ssl, dst_email, DST_PASSWORD_ENV
    )
    dst_conn = _connect(dst_account, "Destination", interactive)

    # Skip list: --skip wins; otherwise preset default, editable interactively.
    default_skip = set(src_preset_obj.skip_folders) if src_preset_obj else set()
    if skip is not None:
        skip_set = {s.strip() for s in skip.split(",") if s.strip()}
    elif interactive and default_skip:
        raw = Prompt.ask(
            "Folders to skip (comma-separated)",
            default=", ".join(sorted(default_skip)),
        )
        skip_set = {s.strip() for s in raw.split(",") if s.strip()}
    else:
        skip_set = default_skip

    plans = build_folder_plan(
        src_conn.client.list_folders(), dst_conn.client.list_folders(), skip_set
    )
    counts = _folder_counts(src_conn, [p.source for p in plans])
    total = sum(counts.values())

    plan_table = Table(title=f"Migration plan — {total} messages in {len(plans)} folders")
    plan_table.add_column("Source folder")
    plan_table.add_column("Messages", justify="right")
    plan_table.add_column("Destination folder")
    for p in plans:
        dest = p.dest + (" [dim](new)[/dim]" if p.create else "")
        plan_table.add_row(p.source, str(counts[p.source]), dest)
    console.print(plan_table)
    if skip_set:
        console.print(f"[dim]Skipping: {', '.join(sorted(skip_set))}[/dim]")

    if interactive and not Confirm.ask("Start migration?"):
        raise typer.Exit(code=0)

    state = MigrationState.for_pair(
        src_account.email, dst_account.email, base_dir=state_dir
    )

    progress_bar = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    )
    try:
        with progress_bar:
            task = progress_bar.add_task("Migrating", total=total)
            result = migrate(
                src_conn,
                dst_conn,
                plans,
                state,
                on_message=lambda folder, uid: progress_bar.update(
                    task, advance=1, description=folder
                ),
            )
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted — progress saved. Run again with the same accounts to resume.[/yellow]")
        raise typer.Exit(code=130)
    except QuotaExceeded as exc:
        console.print(f"[red]{exc}[/red]")
        console.print("[yellow]Progress saved. Free up space on the destination, then run again to resume.[/yellow]")
        raise typer.Exit(code=1)
    finally:
        src_conn.close()
        dst_conn.close()

    summary = Table(title="Done")
    summary.add_column("Migrated", justify="right")
    summary.add_column("Skipped (already there)", justify="right")
    summary.add_column("Failed", justify="right")
    summary.add_row(str(result.migrated), str(result.skipped), str(result.failed))
    console.print(summary)
    if result.failures:
        console.print("[red]Failed messages:[/red]")
        for line in result.failures:
            console.print(f"  [red]- {line}[/red]")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Smoke-test the entry point**

Run: `uv run email-export-import --help`
Expected: help text listing `--src-preset`, `--src-host`, `--dst-host`, `--skip`, `--yes`, `--state-dir`.

- [ ] **Step 6: Commit**

```bash
git add email_export_import/cli.py tests/test_cli.py
git commit -m "feat: add interactive Typer/Rich wizard and non-interactive CLI"
```

---

### Task 9: End-to-end scenarios + README

**Files:**
- Create: `tests/test_integration.py`
- Create: `README.md`

**Interfaces:**
- Consumes: everything. No new production code — this task locks in the spec's integration scenarios against the fake and documents usage.
- Produces: regression suite for resume-after-interrupt, attachment integrity, and cross-delimiter migration; user-facing README.

- [ ] **Step 1: Write the integration tests**

Create `tests/test_integration.py`:

```python
"""Spec integration scenarios (design doc §Testing) against FakeIMAPClient."""
from datetime import datetime

import pytest
from imapclient.exceptions import IMAPClientError

from email_export_import import connection
from email_export_import.connection import MailConnection
from email_export_import.errors import QuotaExceeded
from email_export_import.folders import build_folder_plan
from email_export_import.models import Account
from email_export_import.state import MigrationState
from email_export_import.transfer import migrate
from tests.fakes import FakeIMAPClient, make_message

SRC = Account(host="src.test", port=993, ssl=True, email="a@x", password="p")
DST = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)


def make_conns(monkeypatch, src_fake, dst_fake):
    monkeypatch.setattr(
        connection, "IMAPClient",
        lambda host, port=993, ssl=True: src_fake if host == "src.test" else dst_fake,
    )
    return MailConnection(SRC), MailConnection(DST)


def test_attachment_survives_byte_identical(monkeypatch, tmp_path):
    payload = bytes(range(256)) * 100  # 25.6 KB binary attachment
    msg = make_message(uid=1, message_id="<att@x>", attachment=payload)
    src_fake = FakeIMAPClient(folders={"INBOX": [msg]})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)

    plans = build_folder_plan(src_fake.list_folders(), dst_fake.list_folders())
    migrate(src, dst, plans, MigrationState(tmp_path / "s.json"))

    assert dst_fake.folders["INBOX"][0]["body"] == msg["body"]


def test_interrupted_run_resumes_without_duplicates(monkeypatch, tmp_path):
    msgs = [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 6)]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    plans = build_folder_plan(src_fake.list_folders(), dst_fake.list_folders())
    state_path = tmp_path / "s.json"

    # First run dies at message 3 (quota simulates any mid-run abort: the
    # engine flushes state before raising). Failure must be persistent —
    # with_retry re-attempts APPEND, so a fail-once fake would succeed on
    # the retry and the run would never abort.
    real_append = dst_fake.append
    calls = {"n": 0}
    def dying_append(folder, body, flags=(), msg_time=None):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise IMAPClientError("OVERQUOTA")
        return real_append(folder, body, flags=flags, msg_time=msg_time)
    dst_fake.append = dying_append

    with pytest.raises(QuotaExceeded):
        migrate(src, dst, plans, MigrationState(state_path))
    assert len(dst_fake.folders["INBOX"]) == 2

    # Second run: obstacle gone, resume completes the rest exactly once.
    dst_fake.append = real_append
    result = migrate(src, dst, plans, MigrationState(state_path))
    assert result.migrated == 3
    assert result.skipped == 2
    assert len(dst_fake.folders["INBOX"]) == 5
    ids = sorted(
        m["body"].split(b"Message-ID: ")[1].split(b"\r\n")[0]
        for m in dst_fake.folders["INBOX"]
    )
    assert ids == [f"<m{i}@x>".encode() for i in range(1, 6)]


def test_cross_delimiter_migration_full_stack(monkeypatch, tmp_path):
    when = datetime(2018, 3, 3, 9, 30)
    src_fake = FakeIMAPClient(
        folders={
            "INBOX": [make_message(uid=1, message_id="<i@x>", flags=(b"\\Seen",), internaldate=when)],
            "Work/Projects": [make_message(uid=2, message_id="<w@x>")],
            "Sent Messages": [make_message(uid=3, message_id="<s@x>")],
        },
        special_use={"Sent Messages": b"\\Sent"},
        delimiter="/",
    )
    dst_fake = FakeIMAPClient(
        folders={"INBOX": [], "Gesendet": []},
        special_use={"Gesendet": b"\\Sent"},
        delimiter=".",
    )
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)

    plans = build_folder_plan(src_fake.list_folders(), dst_fake.list_folders())
    result = migrate(src, dst, plans, MigrationState(tmp_path / "s.json"))

    assert result.migrated == 3
    assert len(dst_fake.folders["INBOX"]) == 1
    assert dst_fake.folders["INBOX"][0]["flags"] == (b"\\Seen",)
    assert dst_fake.folders["INBOX"][0]["internaldate"] == when
    assert len(dst_fake.folders["Work.Projects"]) == 1   # delimiter translated
    assert len(dst_fake.folders["Gesendet"]) == 1        # SPECIAL-USE matched
    assert "Sent Messages" not in dst_fake.folders       # no duplicate sent folder
```

- [ ] **Step 2: Run test to verify current behavior**

Run: `uv run pytest tests/test_integration.py -v`
Expected: 3 PASS (these test existing code; if any fail, the engine has a bug — fix the engine, not the test).

- [ ] **Step 3: Write README.md**

Create `README.md`:

````markdown
# email-export-import

Migrate a mailbox from one IMAP server to another — folders, read/starred
flags, original dates, and attachments preserved. Interruptible and
resumable: run it again and it picks up where it left off, never
duplicating a message.

## Install

```bash
uv sync
```

## Usage

Interactive wizard:

```bash
uv run email-export-import
```

Pick the old provider (Gmail, Outlook/Office365, Yahoo, iCloud, Yandex, or
Custom host/port), enter the email address and an **app password**, repeat
for the new provider, review the folder plan, confirm.

Non-interactive:

```bash
EEI_SRC_PASSWORD=... EEI_DST_PASSWORD=... uv run email-export-import \
  --src-preset gmail --src-email old@gmail.com \
  --dst-host imap.newserver.com --dst-email new@newserver.com \
  --yes
```

## Notes

- **App passwords:** Gmail, Outlook, Yahoo, iCloud, and Yandex all reject
  normal passwords over IMAP. The wizard shows the provider's app-password
  page before asking.
- **Gmail:** `[Gmail]/All Mail`, `[Gmail]/Important`, and `[Gmail]/Starred`
  are skipped by default — they are label views that would duplicate every
  message. Override with `--skip`.
- **Resume:** state lives in `~/.email-export-import/state/`. Interrupt
  with Ctrl-C anytime; re-run with the same accounts to resume.
- **Quota:** if the destination fills up, the run aborts immediately with
  a clear message; free space and re-run to resume.

## Development

```bash
uv run pytest
```
````

- [ ] **Step 4: Run the full suite one last time**

Run: `uv run pytest -v`
Expected: all tests PASS (Tasks 1–9).

- [ ] **Step 5: Commit**

```bash
git add tests/test_integration.py README.md
git commit -m "test: add end-to-end scenarios; docs: add README"
```
