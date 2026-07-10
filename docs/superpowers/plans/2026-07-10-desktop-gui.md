# Desktop GUI (Flet) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cross-platform Flet desktop wizard over the existing migration engine: session resume shared with the CLI, TR/EN i18n, testable controller, cancel support.

**Architecture:** Engine untouched except one optional `cancel` event on `migrate()`. New `email_export_import/gui/` package: `i18n.py` (locale dicts), `controller.py` (all logic, no flet import), `views.py` + `app.py` (thin flet layer). `flet` is an optional dependency; core tests never import it.

**Tech Stack:** Python 3.11+, Flet >=0.25,<1.0, existing engine (IMAPClient), pytest, uv.

**Spec:** `docs/superpowers/specs/2026-07-10-desktop-gui-design.md`

## Global Constraints

- Engine changes limited to: `transfer.migrate()` gains `cancel: threading.Event | None = None`. Nothing else in `transfer/state/connection/folders/providers/models/errors` changes.
- `flet` lives only in `[project.optional-dependencies] gui = ["flet>=0.25,<1.0"]`; imported only inside `email_export_import/gui/views.py` and `app.py`. `controller.py` and `i18n.py` MUST NOT import flet.
- GUI console script: `email-export-import-gui = "email_export_import.gui.app:main"`.
- Passwords never written to disk (no gui.json field, no state field, no log).
- Locale files `locales/tr.json` and `locales/en.json` must have identical key sets (enforced by test).
- GUI preferences file: `~/.email-export-import/gui.json` (locale only), mode `0o600`.
- All tests via `uv run pytest`. Tests that need flet use `pytest.importorskip("flet")`.
- Flet API note: plan code targets Flet 0.25 API (`ft.app`, `page.views`, `page.open()`). If the installed flet version renames a symbol, adjust mechanically and record the deviation in your report — do not redesign.
- Commit after every task.

---

### Task 1: Engine cancel event

**Files:**
- Modify: `email_export_import/transfer.py`
- Test: `tests/test_transfer.py`

**Interfaces:**
- Consumes: existing `migrate(src, dst, plans, state, on_message=None, workers=1)`.
- Produces: `migrate(..., cancel: threading.Event | None = None)` — when `cancel` is set, workers stop at the next message boundary exactly like the internal stop event; already-flushed state is preserved; `migrate` returns the partial `TransferProgress` (no exception).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transfer.py`:

```python
def test_cancel_preset_stops_before_any_message(monkeypatch, tmp_path):
    import threading

    msgs = [make_message(uid=i, message_id=f"<m{i}@x>") for i in (1, 2)]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state = MigrationState(tmp_path / "s.json")

    cancel = threading.Event()
    cancel.set()
    progress = migrate(src, dst, [FolderPlan("INBOX", "INBOX", create=False)],
                       state, cancel=cancel)
    assert progress.migrated == 0
    assert len(dst_fake.folders["INBOX"]) == 0


def test_cancel_mid_run_keeps_state_resumable(monkeypatch, tmp_path):
    import threading

    msgs = [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 6)]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state_path = tmp_path / "s.json"

    cancel = threading.Event()
    seen = []

    def cancel_after_two(folder, uid):
        seen.append(uid)
        if len(seen) == 2:
            cancel.set()

    progress = migrate(src, dst, [FolderPlan("INBOX", "INBOX", create=False)],
                       MigrationState(state_path), on_message=cancel_after_two,
                       cancel=cancel)
    assert progress.migrated == 2
    assert len(dst_fake.folders["INBOX"]) == 2

    # Resume completes the rest exactly once — cancel behaved like Ctrl-C.
    progress2 = migrate(src, dst, [FolderPlan("INBOX", "INBOX", create=False)],
                        MigrationState(state_path))
    assert progress2.migrated == 3
    assert progress2.skipped == 2
    assert len(dst_fake.folders["INBOX"]) == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transfer.py -k cancel -v`
Expected: 2 FAIL — `migrate() got an unexpected keyword argument 'cancel'`.

- [ ] **Step 3: Implement**

In `email_export_import/transfer.py`, change `migrate`'s signature and stop wiring:

```python
def migrate(
    src: MailConnection,
    dst: MailConnection,
    plans: list[FolderPlan],
    state: MigrationState,
    on_message: MessageCallback | None = None,
    workers: int = 1,
    cancel: threading.Event | None = None,
) -> TransferProgress:
    progress = TransferProgress()
    lock = threading.Lock()
    stop = cancel if cancel is not None else threading.Event()
```

(The rest of `migrate` already treats `stop` as the shutdown signal — reusing the caller's event as `stop` gives external cancellation for free; no other lines change.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_transfer.py -v`
Expected: all PASS (existing tests unaffected — `cancel=None` keeps today's behavior).

- [ ] **Step 5: Run the full suite and commit**

Run: `uv run pytest`
Expected: all PASS.

```bash
git add email_export_import/transfer.py tests/test_transfer.py
git commit -m "feat: allow external cancellation of migrate() via event"
```

---

### Task 2: i18n module and locales

**Files:**
- Create: `email_export_import/gui/__init__.py` (empty)
- Create: `email_export_import/gui/i18n.py`
- Create: `email_export_import/locales/tr.json`
- Create: `email_export_import/locales/en.json`
- Test: `tests/test_i18n.py`

**Interfaces:**
- Consumes: nothing from the engine (stdlib only).
- Produces:
  - `i18n.I18n(locale: str | None = None)` — loads both locale files; `locale=None` → saved preference, else OS locale, else `"en"`.
  - `I18n.t(key: str, **fmt) -> str` — active locale → English fallback → key itself; `str.format(**fmt)` applied.
  - `I18n.locale: str`; `I18n.set_locale(locale: str) -> None` — switches and persists to `~/.email-export-import/gui.json` (0o600).
  - `i18n.LOCALES_DIR: Path`, `i18n.available_locales() -> list[str]`.
  - Preference file override for tests: `I18n(..., prefs_path: Path | None = None)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_i18n.py`:

```python
import json

from email_export_import.gui.i18n import LOCALES_DIR, I18n, available_locales


def test_locale_files_have_identical_keys():
    tr = json.loads((LOCALES_DIR / "tr.json").read_text())
    en = json.loads((LOCALES_DIR / "en.json").read_text())
    assert set(tr) == set(en)
    assert tr  # not empty


def test_available_locales():
    assert set(available_locales()) == {"en", "tr"}


def test_t_translates_and_formats(tmp_path):
    i = I18n(locale="tr", prefs_path=tmp_path / "gui.json")
    en = I18n(locale="en", prefs_path=tmp_path / "gui.json")
    assert i.t("app.title") != ""
    assert i.t("app.title") != en.t("app.title")  # actually translated
    assert "5" in en.t("plan.total", count=5)


def test_missing_key_falls_back_to_key(tmp_path):
    i = I18n(locale="tr", prefs_path=tmp_path / "gui.json")
    assert i.t("no.such.key") == "no.such.key"


def test_set_locale_persists(tmp_path):
    prefs = tmp_path / "gui.json"
    i = I18n(locale="en", prefs_path=prefs)
    i.set_locale("tr")
    assert (prefs.stat().st_mode & 0o777) == 0o600
    assert I18n(prefs_path=prefs).locale == "tr"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_i18n.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'email_export_import.gui'`.

- [ ] **Step 3: Write i18n.py**

Create `email_export_import/gui/__init__.py` (empty) and `email_export_import/gui/i18n.py`:

```python
from __future__ import annotations

import json
import locale as locale_module
import os
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
DEFAULT_PREFS_PATH = Path.home() / ".email-export-import" / "gui.json"
FALLBACK = "en"


def available_locales() -> list[str]:
    return sorted(p.stem for p in LOCALES_DIR.glob("*.json"))


def _system_locale() -> str | None:
    try:
        lang = locale_module.getlocale()[0] or ""
    except Exception:
        return None
    return lang.split("_")[0].lower() or None


class I18n:
    """Tiny translation layer: active locale -> English -> the key itself."""

    def __init__(self, locale: str | None = None, prefs_path: Path | None = None) -> None:
        self._prefs_path = prefs_path or DEFAULT_PREFS_PATH
        self._tables = {
            name: json.loads((LOCALES_DIR / f"{name}.json").read_text())
            for name in available_locales()
        }
        self.locale = (
            locale
            or self._saved_locale()
            or (_system_locale() if _system_locale() in self._tables else None)
            or FALLBACK
        )

    def _saved_locale(self) -> str | None:
        try:
            saved = json.loads(self._prefs_path.read_text()).get("locale")
        except Exception:
            return None
        return saved if saved in self._tables else None

    def set_locale(self, locale: str) -> None:
        self.locale = locale
        self._prefs_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            self._prefs_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"locale": locale}))
        os.chmod(self._prefs_path, 0o600)

    def t(self, key: str, **fmt) -> str:
        text = self._tables.get(self.locale, {}).get(key)
        if text is None:
            text = self._tables.get(FALLBACK, {}).get(key)
        if text is None:
            return key
        return text.format(**fmt) if fmt else text
```

- [ ] **Step 4: Write the locale files**

Create `email_export_import/locales/en.json`:

```json
{
  "app.title": "Email Migrator",
  "welcome.heading": "Move a mailbox to a new server",
  "welcome.resume_heading": "Unfinished migrations",
  "welcome.resume": "Resume",
  "welcome.new": "New migration",
  "welcome.migrated_count": "{count} messages already moved",
  "account.source_title": "Old mail account (source)",
  "account.dest_title": "New mail account (destination)",
  "account.provider": "Provider",
  "account.custom": "Custom (enter server manually)",
  "account.host": "IMAP server",
  "account.port": "Port",
  "account.ssl": "Use SSL/TLS",
  "account.email": "Email address",
  "account.password": "Password",
  "account.test": "Test connection",
  "account.testing": "Connecting…",
  "account.connected": "Connected",
  "account.next": "Next",
  "account.back": "Back",
  "cert.title": "Certificate cannot be verified",
  "cert.body": "The server presented a certificate that cannot be verified (often self-signed). The connection stays encrypted, but an attacker on the network could impersonate the server and capture your password. Continue only if you trust this server and network.",
  "cert.continue": "Continue without verification",
  "cert.cancel": "Cancel",
  "plan.title": "Migration plan",
  "plan.folder": "Folder",
  "plan.messages": "Messages",
  "plan.destination": "Destination",
  "plan.include": "Include",
  "plan.new_folder": "(new)",
  "plan.workers": "Parallel connections",
  "plan.total": "{count} messages selected",
  "plan.start": "Start migration",
  "progress.title": "Migrating…",
  "progress.cancel": "Cancel",
  "progress.cancelling": "Stopping… current messages are being finished",
  "done.title": "Finished",
  "done.migrated": "Moved",
  "done.skipped": "Already there (skipped)",
  "done.failed": "Failed",
  "done.failures_heading": "Messages that could not be moved",
  "done.resume_hint": "You can run the app again anytime — it continues where it left off without duplicates.",
  "done.quota": "The destination mailbox is full. Free up space and run again to continue.",
  "done.close": "Close",
  "error.auth": "The server rejected this email/password. Check the address and the app password.",
  "error.connection": "Could not connect to the server. Check the server name and your network.",
  "language.label": "Language"
}
```

Create `email_export_import/locales/tr.json`:

```json
{
  "app.title": "E-posta Taşıyıcı",
  "welcome.heading": "Posta kutunuzu yeni sunucuya taşıyın",
  "welcome.resume_heading": "Yarım kalan taşımalar",
  "welcome.resume": "Devam et",
  "welcome.new": "Yeni taşıma",
  "welcome.migrated_count": "{count} mesaj zaten taşındı",
  "account.source_title": "Eski posta hesabı (kaynak)",
  "account.dest_title": "Yeni posta hesabı (hedef)",
  "account.provider": "Sağlayıcı",
  "account.custom": "Özel (sunucuyu elle girin)",
  "account.host": "IMAP sunucusu",
  "account.port": "Port",
  "account.ssl": "SSL/TLS kullan",
  "account.email": "E-posta adresi",
  "account.password": "Parola",
  "account.test": "Bağlantıyı test et",
  "account.testing": "Bağlanılıyor…",
  "account.connected": "Bağlandı",
  "account.next": "İleri",
  "account.back": "Geri",
  "cert.title": "Sertifika doğrulanamıyor",
  "cert.body": "Sunucu doğrulanamayan bir sertifika sundu (genellikle self-signed). Bağlantı şifreli kalır, ancak ağdaki bir saldırgan sunucu taklidi yapıp parolanızı ele geçirebilir. Yalnızca bu sunucuya ve ağa güveniyorsanız devam edin.",
  "cert.continue": "Doğrulamadan devam et",
  "cert.cancel": "Vazgeç",
  "plan.title": "Taşıma planı",
  "plan.folder": "Klasör",
  "plan.messages": "Mesaj",
  "plan.destination": "Hedef",
  "plan.include": "Dahil et",
  "plan.new_folder": "(yeni)",
  "plan.workers": "Paralel bağlantı",
  "plan.total": "{count} mesaj seçildi",
  "plan.start": "Taşımayı başlat",
  "progress.title": "Taşınıyor…",
  "progress.cancel": "İptal",
  "progress.cancelling": "Durduruluyor… mevcut mesajlar tamamlanıyor",
  "done.title": "Tamamlandı",
  "done.migrated": "Taşınan",
  "done.skipped": "Zaten vardı (atlanan)",
  "done.failed": "Başarısız",
  "done.failures_heading": "Taşınamayan mesajlar",
  "done.resume_hint": "Uygulamayı istediğiniz zaman tekrar çalıştırın — kaldığı yerden, mükerrer oluşturmadan devam eder.",
  "done.quota": "Hedef posta kutusu dolu. Yer açın ve devam etmek için tekrar çalıştırın.",
  "done.close": "Kapat",
  "error.auth": "Sunucu bu e-posta/parolayı reddetti. Adresi ve uygulama parolasını kontrol edin.",
  "error.connection": "Sunucuya bağlanılamadı. Sunucu adını ve ağınızı kontrol edin.",
  "language.label": "Dil"
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_i18n.py -v`
Expected: 5 PASS.

- [ ] **Step 6: Run the full suite and commit**

Run: `uv run pytest`
Expected: all PASS.

```bash
git add email_export_import/gui/ email_export_import/locales/ tests/test_i18n.py
git commit -m "feat: add GUI i18n layer with TR/EN locales"
```

---

### Task 3: Controller — sessions, connection testing, plan building

**Files:**
- Modify: `pyproject.toml`
- Create: `email_export_import/gui/controller.py`
- Test: `tests/test_gui_controller.py`

**Interfaces:**
- Consumes: `MigrationState.list_resumable/for_pair/set_config`, `MailConnection`, `build_folder_plan`, `providers.list_presets`, `models.Account/FolderPlan`, `errors.*`; `_namespace_prefix`-equivalent logic (reimplemented here — the CLI helper stays in cli.py).
- Produces:
  - `controller.ConnectionResult(ok: bool, kind: str | None, message: str | None, conn: MailConnection | None)` — dataclass; `kind` in `{"auth", "cert", "connection"}` when not ok.
  - `Controller(state_dir: Path | None = None)`
  - `Controller.list_sessions() -> list[MigrationState]`
  - `Controller.test_connection(account: Account) -> ConnectionResult` — never raises; on success keeps the live connection in the result.
  - `Controller.build_plan(src_conn, dst_conn, skip: set[str]) -> PlanResult` where `PlanResult(plans: list[FolderPlan], counts: dict[str, int], total: int)`.
  - pyproject: `[project.optional-dependencies] gui = ["flet>=0.25,<1.0"]`, script `email-export-import-gui = "email_export_import.gui.app:main"`.

- [ ] **Step 1: Update pyproject.toml**

Add to `pyproject.toml`:

```toml
[project.optional-dependencies]
gui = ["flet>=0.25,<1.0"]
```

and under `[project.scripts]`:

```toml
email-export-import-gui = "email_export_import.gui.app:main"
```

Run: `uv sync` (core only — do NOT install the gui extra in CI/dev by default).

- [ ] **Step 2: Write the failing tests**

Create `tests/test_gui_controller.py`:

```python
import ssl as ssl_mod

import pytest
from imapclient.exceptions import LoginError

from email_export_import import connection
from email_export_import.gui.controller import ConnectionResult, Controller
from email_export_import.models import Account
from email_export_import.state import MigrationState
from tests.fakes import FakeIMAPClient, make_message

ACCOUNT = Account(host="imap.test", port=993, ssl=True, email="a@x", password="p")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)


def install(monkeypatch, factory):
    monkeypatch.setattr(connection, "IMAPClient", factory)


def test_list_sessions_delegates_to_state(tmp_path):
    s = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    s.set_config({"src": {"host": "h", "email": "a@x"}, "dst": {"host": "h2", "email": "b@y"}})
    s.flush()
    sessions = Controller(state_dir=tmp_path).list_sessions()
    assert len(sessions) == 1
    assert sessions[0].config["src"]["email"] == "a@x"


def test_test_connection_success_returns_live_conn(monkeypatch, tmp_path):
    fake = FakeIMAPClient()
    install(monkeypatch, lambda host, port=993, ssl=True, **kw: fake)
    result = Controller(state_dir=tmp_path).test_connection(ACCOUNT)
    assert result.ok
    assert result.conn is not None
    assert fake.logged_in


def test_test_connection_auth_failure(monkeypatch, tmp_path):
    fake = FakeIMAPClient()
    fake.login_error = LoginError("AUTHENTICATIONFAILED")
    install(monkeypatch, lambda host, port=993, ssl=True, **kw: fake)
    result = Controller(state_dir=tmp_path).test_connection(ACCOUNT)
    assert (result.ok, result.kind) == (False, "auth")
    assert result.message


def test_test_connection_cert_failure(monkeypatch, tmp_path):
    def factory(host, port=993, ssl=True, **kw):
        raise ssl_mod.SSLCertVerificationError(1, "certificate verify failed")

    install(monkeypatch, factory)
    result = Controller(state_dir=tmp_path).test_connection(ACCOUNT)
    assert (result.ok, result.kind) == (False, "cert")


def test_test_connection_network_failure(monkeypatch, tmp_path):
    def factory(host, port=993, ssl=True, **kw):
        raise OSError("no route to host")

    install(monkeypatch, factory)
    result = Controller(state_dir=tmp_path).test_connection(ACCOUNT)
    assert (result.ok, result.kind) == (False, "connection")


def test_build_plan_applies_namespace_and_skip(monkeypatch, tmp_path):
    src = FakeIMAPClient(folders={
        "INBOX": [make_message(uid=1, message_id="<a@x>")],
        "Noise": [make_message(uid=2, message_id="<b@x>")],
        "Work": [make_message(uid=3, message_id="<c@x>")],
    })
    dst = FakeIMAPClient(folders={"INBOX": []}, delimiter=".", namespace_prefix="INBOX.")
    install(monkeypatch, lambda host, port=993, ssl=True, **kw: src if host == "imap.test" else dst)

    c = Controller(state_dir=tmp_path)
    src_conn = c.test_connection(ACCOUNT).conn
    dst_conn = c.test_connection(
        Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")
    ).conn
    plan = c.build_plan(src_conn, dst_conn, skip={"Noise"})
    by_source = {p.source: p for p in plan.plans}
    assert set(by_source) == {"INBOX", "Work"}
    assert by_source["Work"].dest == "INBOX.Work"
    assert plan.counts["INBOX"] == 1
    assert plan.total == 2
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_gui_controller.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'email_export_import.gui.controller'`.

- [ ] **Step 4: Write controller.py (part 1)**

Create `email_export_import/gui/controller.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..connection import MailConnection
from ..errors import AuthFailed, CertificateVerifyFailed, ConnectionFailed
from ..folders import build_folder_plan
from ..models import Account, FolderPlan
from ..state import MigrationState


@dataclass
class ConnectionResult:
    ok: bool
    kind: str | None = None  # "auth" | "cert" | "connection"
    message: str | None = None
    conn: MailConnection | None = None


@dataclass
class PlanResult:
    plans: list[FolderPlan]
    counts: dict[str, int]
    total: int


class Controller:
    """All GUI decisions live here; views only render what this returns.

    Deliberately flet-free so every path is unit-testable headless.
    """

    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = state_dir

    def list_sessions(self) -> list[MigrationState]:
        return MigrationState.list_resumable(base_dir=self.state_dir)

    def test_connection(self, account: Account) -> ConnectionResult:
        conn = MailConnection(account)
        try:
            conn.connect()
        except CertificateVerifyFailed as exc:
            return ConnectionResult(ok=False, kind="cert", message=str(exc))
        except AuthFailed as exc:
            return ConnectionResult(ok=False, kind="auth", message=str(exc))
        except ConnectionFailed as exc:
            return ConnectionResult(ok=False, kind="connection", message=str(exc))
        return ConnectionResult(ok=True, conn=conn)

    @staticmethod
    def _namespace_prefix(conn: MailConnection) -> str:
        try:
            prefix, _sep = conn.with_retry(lambda c: c.namespace()).personal[0]
        except Exception:
            return ""
        if isinstance(prefix, bytes):
            prefix = prefix.decode()
        return prefix or ""

    def build_plan(
        self,
        src_conn: MailConnection,
        dst_conn: MailConnection,
        skip: set[str],
    ) -> PlanResult:
        plans = build_folder_plan(
            src_conn.with_retry(lambda c: c.list_folders()),
            dst_conn.with_retry(lambda c: c.list_folders()),
            skip,
            dst_prefix=self._namespace_prefix(dst_conn),
        )
        counts: dict[str, int] = {}
        for p in plans:
            try:
                counts[p.source] = src_conn.with_retry(
                    lambda c, n=p.source: c.folder_status(n, [b"MESSAGES"])
                )[b"MESSAGES"]
            except Exception:
                counts[p.source] = 0
        return PlanResult(plans=plans, counts=counts, total=sum(counts.values()))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_gui_controller.py -v`
Expected: 6 PASS.

- [ ] **Step 6: Run the full suite and commit**

Run: `uv run pytest`
Expected: all PASS.

```bash
git add pyproject.toml uv.lock email_export_import/gui/controller.py tests/test_gui_controller.py
git commit -m "feat: add GUI controller (sessions, connection testing, plan building)"
```

---

### Task 4: Controller — migration runner with cancel and snapshot polling

**Files:**
- Modify: `email_export_import/gui/controller.py`
- Test: `tests/test_gui_controller.py`

**Interfaces:**
- Consumes: `transfer.migrate(..., cancel=...)` (Task 1), `MigrationState`, `TransferProgress`, `QuotaExceeded`.
- Produces (appended to `controller.py`):
  - `RunSnapshot(processed: int, total: int, current_folder: str | None, running: bool, result: TransferProgress | None, error_kind: str | None, error_message: str | None)` — dataclass. `error_kind` in `{"quota", "fatal"}`.
  - `Controller.start(src_conn, dst_conn, plans, state, workers, total, skip: set[str] | None = None) -> None` — spawns a daemon thread; saves session config (`state.set_config` + flush, including the skip set) before starting; marks the state completed on clean finish.
  - `Controller.snapshot() -> RunSnapshot` — thread-safe; the UI polls this on a ~100 ms timer (this is the event-batching mechanism from the spec: N on_message events collapse into one snapshot read).
  - `Controller.cancel() -> None` — sets the cancel event; run ends at the next message boundary; state stays resumable.
  - `Controller.join(timeout: float | None = None) -> None` — test helper; waits for the worker thread.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gui_controller.py`:

```python
def _wire_pair(monkeypatch, src_fake, dst_fake):
    install(
        monkeypatch,
        lambda host, port=993, ssl=True, **kw: src_fake if host == "imap.test" else dst_fake,
    )


def test_runner_completes_and_marks_state(monkeypatch, tmp_path):
    src_fake = FakeIMAPClient(
        folders={"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>") for i in (1, 2, 3)]}
    )
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    _wire_pair(monkeypatch, src_fake, dst_fake)

    c = Controller(state_dir=tmp_path)
    src_conn = c.test_connection(ACCOUNT).conn
    dst_acc = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")
    dst_conn = c.test_connection(dst_acc).conn
    plan = c.build_plan(src_conn, dst_conn, skip=set())
    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)

    c.start(src_conn, dst_conn, plan.plans, state, workers=1, total=plan.total)
    c.join(timeout=10)

    snap = c.snapshot()
    assert snap.running is False
    assert snap.error_kind is None
    assert snap.result.migrated == 3
    assert snap.processed == 3
    assert len(dst_fake.folders["INBOX"]) == 3
    # Session config saved (no passwords) and marked completed.
    reloaded = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    assert reloaded.status == "completed"
    assert "password" not in str(reloaded.config).lower()


def test_runner_cancel_leaves_session_resumable(monkeypatch, tmp_path):
    import threading

    msgs = [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 30)]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})

    gate = threading.Event()
    real_append = dst_fake.append

    def slow_append(folder, body, flags=(), msg_time=None):
        gate.wait(timeout=5)  # hold until the test cancels
        return real_append(folder, body, flags=flags, msg_time=msg_time)

    dst_fake.append = slow_append
    _wire_pair(monkeypatch, src_fake, dst_fake)

    c = Controller(state_dir=tmp_path)
    src_conn = c.test_connection(ACCOUNT).conn
    dst_acc = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")
    dst_conn = c.test_connection(dst_acc).conn
    plan = c.build_plan(src_conn, dst_conn, skip=set())
    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)

    c.start(src_conn, dst_conn, plan.plans, state, workers=1, total=plan.total)
    c.cancel()
    gate.set()  # release the in-flight append
    c.join(timeout=10)

    snap = c.snapshot()
    assert snap.running is False
    assert snap.result.migrated < len(msgs)  # stopped early
    reloaded = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    assert reloaded.status == "running"  # still resumable


def test_runner_quota_reports_error(monkeypatch, tmp_path):
    from imapclient.exceptions import IMAPClientError

    src_fake = FakeIMAPClient(
        folders={"INBOX": [make_message(uid=1, message_id="<m1@x>")]}
    )
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    dst_fake.append_error = IMAPClientError("APPEND failed [OVERQUOTA]")
    _wire_pair(monkeypatch, src_fake, dst_fake)

    c = Controller(state_dir=tmp_path)
    src_conn = c.test_connection(ACCOUNT).conn
    dst_acc = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")
    dst_conn = c.test_connection(dst_acc).conn
    plan = c.build_plan(src_conn, dst_conn, skip=set())
    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)

    c.start(src_conn, dst_conn, plan.plans, state, workers=1, total=plan.total)
    c.join(timeout=10)

    snap = c.snapshot()
    assert snap.error_kind == "quota"
    assert MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path).status == "running"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gui_controller.py -k runner -v`
Expected: 3 FAIL — `AttributeError: 'Controller' object has no attribute 'start'`.

- [ ] **Step 3: Extend controller.py**

Add to the imports in `email_export_import/gui/controller.py`:

```python
import threading

from ..errors import QuotaExceeded
from ..models import TransferProgress
from ..transfer import migrate
```

Add after `PlanResult`:

```python
@dataclass
class RunSnapshot:
    processed: int
    total: int
    current_folder: str | None
    running: bool
    result: TransferProgress | None = None
    error_kind: str | None = None  # "quota" | "fatal"
    error_message: str | None = None
```

Add these members to `Controller.__init__`:

```python
        self._run_lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._processed = 0
        self._total = 0
        self._current_folder: str | None = None
        self._result: TransferProgress | None = None
        self._error: tuple[str, str] | None = None
```

Add these methods to `Controller`:

```python
    def start(
        self,
        src_conn: MailConnection,
        dst_conn: MailConnection,
        plans: list[FolderPlan],
        state: MigrationState,
        workers: int,
        total: int,
        skip: set[str] | None = None,
    ) -> None:
        with self._run_lock:
            self._cancel = threading.Event()
            self._processed = 0
            self._total = total
            self._current_folder = None
            self._result = None
            self._error = None

        state.set_config(
            {
                "src": self._account_config(src_conn.account),
                "dst": self._account_config(dst_conn.account),
                "skip": sorted(skip or set()),
                "workers": workers,
            }
        )
        state.flush()

        def on_message(folder: str, uid: int) -> None:
            with self._run_lock:
                self._processed += 1
                self._current_folder = folder

        def run() -> None:
            try:
                result = migrate(
                    src_conn, dst_conn, plans, state,
                    on_message=on_message, workers=workers, cancel=self._cancel,
                )
                if not self._cancel.is_set():
                    state.mark_completed()
                    state.flush()
                with self._run_lock:
                    self._result = result
            except QuotaExceeded as exc:
                with self._run_lock:
                    self._error = ("quota", str(exc))
            except Exception as exc:
                with self._run_lock:
                    self._error = ("fatal", str(exc))
            finally:
                src_conn.close()
                dst_conn.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    @staticmethod
    def _account_config(account: Account) -> dict:
        return {
            "host": account.host,
            "port": account.port,
            "ssl": account.ssl,
            "verify_ssl": account.verify_ssl,
            "email": account.email,
        }

    def snapshot(self) -> RunSnapshot:
        with self._run_lock:
            running = self._thread is not None and self._thread.is_alive()
            error_kind, error_message = self._error or (None, None)
            return RunSnapshot(
                processed=self._processed,
                total=self._total,
                current_folder=self._current_folder,
                running=running,
                result=self._result,
                error_kind=error_kind,
                error_message=error_message,
            )

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
```

Note: the skip decision is already baked into `plans`; the session file
records the skip set so a resumed session restores the user's choices.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gui_controller.py -v`
Expected: 9 PASS.

- [ ] **Step 5: Run the full suite and commit**

Run: `uv run pytest`
Expected: all PASS.

```bash
git add email_export_import/gui/controller.py tests/test_gui_controller.py
git commit -m "feat: add GUI migration runner with cancel and snapshot polling"
```

---

### Task 5: Flet views and app entry point

**Files:**
- Create: `email_export_import/gui/views.py`
- Create: `email_export_import/gui/app.py`
- Test: `tests/test_gui_app.py`

**Interfaces:**
- Consumes: `Controller`, `ConnectionResult`, `PlanResult`, `RunSnapshot` (Tasks 3–4); `I18n` (Task 2); `providers.list_presets`; `models.Account`.
- Produces: `app.main()` (console-script target) and `views.build_*` functions. Views hold NO business logic — every decision goes through the controller.

- [ ] **Step 1: Write the (skippable) smoke test**

Create `tests/test_gui_app.py`:

```python
import pytest

flet = pytest.importorskip("flet")


def test_gui_modules_import():
    from email_export_import.gui import app, views  # noqa: F401

    assert callable(app.main)


def test_wizard_state_defaults():
    from email_export_import.gui.app import WizardState

    ws = WizardState()
    assert ws.workers == 4
    assert ws.skip == set()
```

(Runs only when the `gui` extra is installed: `uv sync --extra gui`. Core CI stays flet-free and skips this file.)

- [ ] **Step 2: Write views.py**

Create `email_export_import/gui/views.py`:

```python
from __future__ import annotations

from typing import Callable

import flet as ft

from ..models import Account
from ..providers import list_presets
from .controller import PlanResult, RunSnapshot
from .i18n import I18n


def _title_bar(i18n: I18n, on_locale: Callable[[str], None]) -> ft.Row:
    return ft.Row(
        [
            ft.Text(i18n.t("app.title"), size=22, weight=ft.FontWeight.BOLD, expand=True),
            ft.Dropdown(
                width=110,
                value=i18n.locale,
                options=[ft.dropdown.Option("tr", "Türkçe"), ft.dropdown.Option("en", "English")],
                on_change=lambda e: on_locale(e.control.value),
                label=i18n.t("language.label"),
            ),
        ]
    )


def build_welcome(
    i18n: I18n,
    sessions: list,
    on_resume: Callable[[object], None],
    on_new: Callable[[], None],
    on_locale: Callable[[str], None],
) -> ft.View:
    rows = []
    for s in sessions:
        cfg = s.config or {}
        rows.append(
            ft.ListTile(
                title=ft.Text(f"{cfg['src']['email']} → {cfg['dst']['email']}"),
                subtitle=ft.Text(
                    i18n.t("welcome.migrated_count", count=s.migrated_count())
                ),
                trailing=ft.FilledButton(
                    i18n.t("welcome.resume"), on_click=lambda e, s=s: on_resume(s)
                ),
            )
        )
    body: list[ft.Control] = [
        _title_bar(i18n, on_locale),
        ft.Text(i18n.t("welcome.heading"), size=16),
    ]
    if rows:
        body.append(ft.Text(i18n.t("welcome.resume_heading"), weight=ft.FontWeight.BOLD))
        body.extend(rows)
    body.append(ft.FilledButton(i18n.t("welcome.new"), on_click=lambda e: on_new()))
    return ft.View("/", body, padding=24, spacing=16)


def build_account(
    i18n: I18n,
    role: str,  # "source" | "dest"
    initial: dict,
    on_test: Callable[[Account], None],
    on_back: Callable[[], None],
    status_text: ft.Text,
) -> tuple[ft.View, dict]:
    presets = list_presets()
    custom_key = "__custom__"
    preset_dd = ft.Dropdown(
        label=i18n.t("account.provider"),
        value=initial.get("preset", custom_key),
        options=[ft.dropdown.Option(p.key, p.name) for p in presets]
        + [ft.dropdown.Option(custom_key, i18n.t("account.custom"))],
    )
    host = ft.TextField(label=i18n.t("account.host"), value=initial.get("host", ""))
    port = ft.TextField(label=i18n.t("account.port"), value=str(initial.get("port", 993)), width=110)
    use_ssl = ft.Checkbox(label=i18n.t("account.ssl"), value=initial.get("ssl", True))
    email = ft.TextField(label=i18n.t("account.email"), value=initial.get("email", ""))
    password = ft.TextField(
        label=i18n.t("account.password"), password=True, can_reveal_password=True
    )
    hint = ft.Text("", size=12, color=ft.Colors.AMBER)

    def preset_changed(e=None):
        for p in presets:
            if p.key == preset_dd.value:
                host.value, port.value, use_ssl.value = p.host, str(p.port), p.ssl
                hint.value = p.app_password_hint or ""
                break
        else:
            hint.value = ""
        if host.page:
            host.update(); port.update(); use_ssl.update(); hint.update()

    preset_dd.on_change = preset_changed
    if initial.get("preset"):
        preset_changed()

    def account() -> Account:
        return Account(
            host=host.value.strip(),
            port=int(port.value or 993),
            ssl=bool(use_ssl.value),
            email=email.value.strip(),
            password=password.value,
        )

    controls = [
        ft.Text(i18n.t(f"account.{'source' if role == 'source' else 'dest'}_title"),
                size=18, weight=ft.FontWeight.BOLD),
        preset_dd, host, ft.Row([port, use_ssl]), email, password, hint, status_text,
        ft.Row(
            [
                ft.TextButton(i18n.t("account.back"), on_click=lambda e: on_back()),
                ft.FilledButton(i18n.t("account.test"), on_click=lambda e: on_test(account())),
            ],
            alignment=ft.MainAxisAlignment.END,
        ),
    ]
    return ft.View(f"/{role}", controls, padding=24, spacing=12), {"account": account}


def build_plan(
    i18n: I18n,
    plan: PlanResult,
    skip: set[str],
    workers: int,
    on_toggle: Callable[[str, bool], None],
    on_workers: Callable[[int], None],
    on_start: Callable[[], None],
    on_back: Callable[[], None],
) -> ft.View:
    rows = [
        ft.DataRow(
            cells=[
                ft.DataCell(ft.Checkbox(
                    value=p.source not in skip,
                    on_change=lambda e, s=p.source: on_toggle(s, e.control.value),
                )),
                ft.DataCell(ft.Text(p.source)),
                ft.DataCell(ft.Text(str(plan.counts.get(p.source, 0)))),
                ft.DataCell(ft.Text(p.dest + (" " + i18n.t("plan.new_folder") if p.create else ""))),
            ]
        )
        for p in plan.plans
    ]
    table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text(i18n.t("plan.include"))),
            ft.DataColumn(ft.Text(i18n.t("plan.folder"))),
            ft.DataColumn(ft.Text(i18n.t("plan.messages")), numeric=True),
            ft.DataColumn(ft.Text(i18n.t("plan.destination"))),
        ],
        rows=rows,
    )
    selected_total = sum(
        plan.counts.get(p.source, 0) for p in plan.plans if p.source not in skip
    )
    workers_dd = ft.Dropdown(
        label=i18n.t("plan.workers"),
        value=str(workers),
        width=160,
        options=[ft.dropdown.Option(str(n)) for n in (1, 2, 4, 8, 16)],
        on_change=lambda e: on_workers(int(e.control.value)),
    )
    return ft.View(
        "/plan",
        [
            ft.Text(i18n.t("plan.title"), size=18, weight=ft.FontWeight.BOLD),
            ft.Column([table], scroll=ft.ScrollMode.AUTO, expand=True),
            ft.Row([workers_dd, ft.Text(i18n.t("plan.total", count=selected_total))]),
            ft.Row(
                [
                    ft.TextButton(i18n.t("account.back"), on_click=lambda e: on_back()),
                    ft.FilledButton(i18n.t("plan.start"), on_click=lambda e: on_start()),
                ],
                alignment=ft.MainAxisAlignment.END,
            ),
        ],
        padding=24,
        spacing=12,
    )


def build_progress(
    i18n: I18n, on_cancel: Callable[[], None]
) -> tuple[ft.View, ft.ProgressBar, ft.Text, ft.Text]:
    bar = ft.ProgressBar(value=0)
    counter = ft.Text("0 / 0")
    folder = ft.Text("")
    view = ft.View(
        "/progress",
        [
            ft.Text(i18n.t("progress.title"), size=18, weight=ft.FontWeight.BOLD),
            bar, counter, folder,
            ft.Row(
                [ft.TextButton(i18n.t("progress.cancel"), on_click=lambda e: on_cancel())],
                alignment=ft.MainAxisAlignment.END,
            ),
        ],
        padding=24,
        spacing=12,
    )
    return view, bar, counter, folder


def build_done(
    i18n: I18n, snap: RunSnapshot, on_close: Callable[[], None]
) -> ft.View:
    controls: list[ft.Control] = [
        ft.Text(i18n.t("done.title"), size=18, weight=ft.FontWeight.BOLD)
    ]
    if snap.error_kind == "quota":
        controls.append(ft.Text(i18n.t("done.quota"), color=ft.Colors.RED))
    elif snap.error_kind == "fatal":
        controls.append(ft.Text(snap.error_message or "", color=ft.Colors.RED))
    if snap.result is not None:
        controls.append(
            ft.Row(
                [
                    ft.Text(f"{i18n.t('done.migrated')}: {snap.result.migrated}"),
                    ft.Text(f"{i18n.t('done.skipped')}: {snap.result.skipped}"),
                    ft.Text(f"{i18n.t('done.failed')}: {snap.result.failed}"),
                ],
                spacing=24,
            )
        )
        if snap.result.failures:
            controls.append(ft.Text(i18n.t("done.failures_heading"), weight=ft.FontWeight.BOLD))
            controls.append(
                ft.Column(
                    [ft.Text(line, size=12) for line in snap.result.failures[:50]],
                    scroll=ft.ScrollMode.AUTO,
                    height=200,
                )
            )
    controls.append(ft.Text(i18n.t("done.resume_hint"), size=12))
    controls.append(ft.FilledButton(i18n.t("done.close"), on_click=lambda e: on_close()))
    return ft.View("/done", controls, padding=24, spacing=12)
```

- [ ] **Step 3: Write app.py**

Create `email_export_import/gui/app.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field

import flet as ft

from ..models import Account
from ..state import MigrationState
from . import views
from .controller import Controller, PlanResult
from .i18n import I18n


@dataclass
class WizardState:
    src_account: Account | None = None
    dst_account: Account | None = None
    src_conn: object = None
    dst_conn: object = None
    plan: PlanResult | None = None
    skip: set[str] = field(default_factory=set)
    workers: int = 4
    resume_session: MigrationState | None = None


def main() -> None:
    ft.app(target=_page_main)


def _page_main(page: ft.Page) -> None:
    i18n = I18n()
    controller = Controller()
    ws = WizardState()
    page.title = i18n.t("app.title")
    page.window.width = 760
    page.window.height = 640

    def set_locale(locale: str) -> None:
        i18n.set_locale(locale)
        go_welcome()

    def go_welcome() -> None:
        page.views.clear()
        page.views.append(
            views.build_welcome(
                i18n, controller.list_sessions(), on_resume=resume_session,
                on_new=lambda: go_account("source"), on_locale=set_locale,
            )
        )
        page.update()

    def resume_session(session: MigrationState) -> None:
        ws.resume_session = session
        cfg = session.config or {}
        ws.workers = cfg.get("workers", 4)
        ws.skip = set(cfg.get("skip", []))
        go_account("source", prefill=cfg.get("src", {}))

    def go_account(role: str, prefill: dict | None = None) -> None:
        status = ft.Text("")
        initial = dict(prefill or {})
        if role == "dest" and ws.src_account and not initial.get("email"):
            initial["email"] = ws.src_account.email
        if ws.resume_session and role == "dest" and not prefill:
            initial = dict((ws.resume_session.config or {}).get("dst", {}))
            initial.setdefault("email", ws.src_account.email if ws.src_account else "")

        def on_test(account: Account) -> None:
            status.value = i18n.t("account.testing")
            status.update()
            result = controller.test_connection(account)
            if result.ok:
                status.value = i18n.t("account.connected")
                status.update()
                if role == "source":
                    ws.src_account, ws.src_conn = account, result.conn
                    go_account("dest")
                else:
                    ws.dst_account, ws.dst_conn = account, result.conn
                    go_plan()
            elif result.kind == "cert":
                _cert_dialog(account)
            else:
                status.value = i18n.t(f"error.{result.kind}")
                status.update()

        def _cert_dialog(account: Account) -> None:
            def retry_unverified(e) -> None:
                page.close(dialog)
                account.verify_ssl = False
                on_test(account)

            dialog = ft.AlertDialog(
                modal=True,
                title=ft.Text(i18n.t("cert.title")),
                content=ft.Text(i18n.t("cert.body")),
                actions=[
                    ft.TextButton(i18n.t("cert.cancel"), on_click=lambda e: page.close(dialog)),
                    ft.FilledButton(i18n.t("cert.continue"), on_click=retry_unverified),
                ],
            )
            page.open(dialog)

        def on_back() -> None:
            go_welcome() if role == "source" else go_account("source")

        view, _handles = views.build_account(i18n, role, initial, on_test, on_back, status)
        page.views.clear()
        page.views.append(view)
        page.update()

    def go_plan() -> None:
        ws.plan = controller.build_plan(ws.src_conn, ws.dst_conn, ws.skip)

        def on_toggle(source: str, included: bool) -> None:
            (ws.skip.discard if included else ws.skip.add)(source)
            go_plan_refresh()

        def go_plan_refresh() -> None:
            page.views[-1] = views.build_plan(
                i18n, ws.plan, ws.skip, ws.workers,
                on_toggle, on_workers, start_migration, lambda: go_account("dest"),
            )
            page.update()

        def on_workers(n: int) -> None:
            ws.workers = n

        page.views.clear()
        page.views.append(
            views.build_plan(
                i18n, ws.plan, ws.skip, ws.workers,
                on_toggle, on_workers, start_migration, lambda: go_account("dest"),
            )
        )
        page.update()

    def start_migration() -> None:
        active_plans = [p for p in ws.plan.plans if p.source not in ws.skip]
        total = sum(ws.plan.counts.get(p.source, 0) for p in active_plans)
        state = ws.resume_session or MigrationState.for_pair(
            ws.src_account.email, ws.dst_account.email
        )
        controller.start(ws.src_conn, ws.dst_conn, active_plans, state,
                         workers=ws.workers, total=total, skip=ws.skip)
        go_progress(total)

    def go_progress(total: int) -> None:
        view, bar, counter, folder = views.build_progress(i18n, on_cancel=controller.cancel)
        page.views.clear()
        page.views.append(view)
        page.update()

        import time

        def poll() -> None:
            while True:
                snap = controller.snapshot()
                bar.value = (snap.processed / snap.total) if snap.total else 0
                counter.value = f"{snap.processed} / {snap.total}"
                folder.value = snap.current_folder or ""
                try:
                    bar.update(); counter.update(); folder.update()
                except Exception:
                    return  # page closed
                if not snap.running:
                    page.views.clear()
                    page.views.append(views.build_done(i18n, snap, on_close=page.window.close))
                    page.update()
                    return
                time.sleep(0.1)

        page.run_thread(poll)

    go_welcome()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Install the gui extra and run the smoke tests**

Run: `uv sync --extra gui && uv run pytest tests/test_gui_app.py -v`
Expected: 2 PASS (or 2 SKIPPED if flet failed to install on this platform — then fix the install before proceeding).

- [ ] **Step 5: Manual launch check**

Run: `uv run email-export-import-gui`
Expected: window opens on the welcome screen; language toggle switches TR/EN. Close the window. (If a Flet 0.25+ API symbol differs in the installed version, adjust mechanically and note it in your report.)

- [ ] **Step 6: Run the full suite and commit**

Run: `uv run pytest`
Expected: all PASS (gui tests run because the extra is installed).

```bash
git add email_export_import/gui/ tests/test_gui_app.py
git commit -m "feat: add Flet wizard views and GUI entry point"
```

---

### Task 6: Packaging workflow, README, smoke checklist

**Files:**
- Create: `.github/workflows/build-gui.yml`
- Create: `docs/superpowers/gui-smoke-checklist.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: everything (no code changes).
- Produces: CI that builds unsigned desktop bundles on tag push; user docs.

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/build-gui.yml`:

```yaml
name: build-gui
on:
  push:
    tags: ["v*"]
  workflow_dispatch:

jobs:
  build:
    strategy:
      matrix:
        include:
          - os: macos-latest
            target: macos
          - os: ubuntu-latest
            target: linux
          - os: windows-latest
            target: windows
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - name: Install Flutter (required by flet build)
        uses: subosito/flutter-action@v2
        with:
          channel: stable
      - name: Build
        run: |
          uv sync --extra gui
          uv run flet build ${{ matrix.target }} --project email-export-import
      - uses: actions/upload-artifact@v4
        with:
          name: email-export-import-${{ matrix.target }}
          path: build/${{ matrix.target }}
```

Note: builds are **unsigned** — signing/notarization is a tracked release task, not part of this plan.

- [ ] **Step 2: Write the smoke checklist**

Create `docs/superpowers/gui-smoke-checklist.md`:

```markdown
# GUI manual smoke checklist

Run before each release, on at least one platform:

- [ ] `uv run email-export-import-gui` opens the welcome screen
- [ ] Language toggle switches every visible string (TR ↔ EN)
- [ ] Unfinished CLI session appears in the resume list; Resume asks only passwords
- [ ] Preset dropdown fills host/port/SSL; Custom leaves them editable
- [ ] Wrong password shows the auth error text (no crash, no traceback)
- [ ] Self-signed server raises the certificate dialog; Continue connects
- [ ] Plan screen counts match the mailbox; unchecking a folder lowers the total
- [ ] Progress advances (counter + bar + folder name)
- [ ] Cancel stops within a few seconds; relaunching offers to resume
- [ ] Done screen shows summary; failures listed when present
- [ ] Killing the app mid-run and relaunching resumes without duplicates
```

- [ ] **Step 3: Update README.md**

Add after the "Non-interactive" usage block in `README.md`:

```markdown
Desktop app (experimental):

```bash
uv sync --extra gui
uv run email-export-import-gui
```

Same engine, same resume files as the CLI — start a migration in one and
finish it in the other. Turkish and English UI.
```

- [ ] **Step 4: Run the full suite and commit**

Run: `uv run pytest`
Expected: all PASS.

```bash
git add .github/ docs/superpowers/gui-smoke-checklist.md README.md
git commit -m "ci: add unsigned desktop build workflow; docs: GUI usage and smoke checklist"
```
