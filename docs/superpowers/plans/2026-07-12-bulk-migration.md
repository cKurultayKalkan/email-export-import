# Bulk Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user enter many mailboxes sharing one source provider + one destination server and start them all, under a concurrency cap that protects a rate-limiting destination.

**Architecture:** A new bulk-entry view collects `AccountSpec`s. An app-layer coordinator adds a queued placeholder `Run` per spec, then on each (event-loop) poll tick pumps up to `cap` connect+plan operations off-loop, promoting each to a real running `Run`. Reuses `Run`/`RunManager`/`transfer` unchanged.

**Tech Stack:** Python 3.11+, flet 0.85 (optional gui extra), pytest (network-free FakeIMAPClient/FakePage).

## Global Constraints

- Passwords are NEVER persisted to disk (in-memory only for the connect lifetime).
- GUI code is optional: tests `pytest.importorskip("flet")`; engine stays flet-free.
- All UI mutation happens on the event loop (via `page.run_task`/`ui()`), never `run_thread`. (See flet-ui-thread-affinity.)
- Locale keys must exist in BOTH `email_export_import/locales/en.json` and `tr.json` (test_i18n enforces identical key sets).
- Concurrency cap default = 2; adjustable range 1–4; persisted in `gui.json`.

---

## File structure

- Create `email_export_import/gui/prefs.py` — read/merge/write `gui.json` (0600). One responsibility: pref persistence.
- Modify `email_export_import/gui/i18n.py` — route `set_locale` through `prefs.save_pref`.
- Modify `email_export_import/gui/run_manager.py` — `RunManager.max_active`; `Run.mark_failed(message)`.
- Modify `email_export_import/gui/views.py` — extract preset-fill helper; add `build_bulk`; add "New bulk" dashboard button; add cap dropdown to `build_settings`.
- Modify `email_export_import/gui/app.py` — `AccountSpec`, `start_bulk`, `pump_bulk`, `connect_and_plan`, wiring, poll pump, settings cap callback.
- Modify `email_export_import/locales/en.json` + `tr.json` — bulk/settings keys.
- Tests: `tests/test_prefs.py`, `tests/test_bulk_view.py`, add to `tests/test_gui_e2e.py`, `tests/test_run_manager.py` (mark_failed/max_active).

---

### Task 1: Pref persistence helper (merge, not clobber)

**Files:**
- Create: `email_export_import/gui/prefs.py`
- Modify: `email_export_import/gui/i18n.py` (`set_locale`)
- Test: `tests/test_prefs.py`

**Interfaces:**
- Produces: `prefs.load_prefs(path: Path) -> dict`; `prefs.save_pref(path: Path, key: str, value) -> None` (read-merge-write, 0600, mkdir parents).

- [ ] **Step 1: Failing test** `tests/test_prefs.py`

```python
import json
from email_export_import.gui import prefs

def test_save_pref_merges_and_preserves_other_keys(tmp_path):
    p = tmp_path / "gui.json"
    prefs.save_pref(p, "locale", "tr")
    prefs.save_pref(p, "max_active", 3)
    data = json.loads(p.read_text())
    assert data == {"locale": "tr", "max_active": 3}
    assert prefs.load_prefs(p) == {"locale": "tr", "max_active": 3}

def test_load_prefs_missing_file_is_empty(tmp_path):
    assert prefs.load_prefs(tmp_path / "nope.json") == {}
```

- [ ] **Step 2: Run — expect FAIL** `uv run pytest tests/test_prefs.py -q`

- [ ] **Step 3: Implement `prefs.py`**

```python
from __future__ import annotations

import json
import os
from pathlib import Path


def load_prefs(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def save_pref(path: Path, key: str, value) -> None:
    path = Path(path)
    data = load_prefs(path)
    data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data))
    os.chmod(path, 0o600)
```

- [ ] **Step 4: Route `i18n.set_locale` through it** — replace the os.open block in `set_locale` with:

```python
        self.locale = locale
        from . import prefs
        prefs.save_pref(self._prefs_path, "locale", locale)
```

- [ ] **Step 5: Run** `uv run pytest tests/test_prefs.py tests/test_i18n.py -q` → PASS

- [ ] **Step 6: Commit** `feat(gui): merge-preserving pref persistence`

---

### Task 2: RunManager cap + Run.mark_failed

**Files:**
- Modify: `email_export_import/gui/run_manager.py`
- Test: `tests/test_run_manager.py`

**Interfaces:**
- Produces: `RunManager(max_active: int = 2)` attribute `max_active`; `Run.mark_failed(message: str) -> None` sets status `error`, error `("fatal", message)`.

- [ ] **Step 1: Failing test** (append to `tests/test_run_manager.py`)

```python
def test_mark_failed_sets_error_snapshot():
    from email_export_import.gui.run_manager import Run
    from email_export_import.state import MigrationState
    st = MigrationState.for_pair("a@x", "b@y")
    run = Run(key="k", title="t", src_conn=None, dst_conn=None, plans=None,
              state=st, workers=2, total=0)
    run.mark_failed("boom")
    snap = run.snapshot()
    assert snap.status == "error"
    assert snap.error_kind == "fatal"
    assert snap.error_message == "boom"

def test_run_manager_default_max_active_is_two():
    from email_export_import.gui.run_manager import RunManager
    assert RunManager().max_active == 2
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement.** In `Run` add:

```python
    def mark_failed(self, message: str) -> None:
        with self._lock:
            self._status = "error"
            self._error = ("fatal", message)
```

In `RunManager.__init__(self, state_dir=None)` add param `max_active: int = 2` and `self.max_active = max_active`.

- [ ] **Step 4: Run** `uv run pytest tests/test_run_manager.py -q` → PASS

- [ ] **Step 5: Commit** `feat(gui): Run.mark_failed + RunManager.max_active`

---

### Task 3: `build_bulk` view + `collect()` validation

**Files:**
- Modify: `email_export_import/gui/views.py`
- Modify: `email_export_import/locales/en.json`, `tr.json`
- Test: `tests/test_bulk_view.py`

**Interfaces:**
- Consumes: `list_presets()`, `Account`.
- Produces: `views.build_bulk(i18n, on_start: Callable[[list[Account_pair], str|None], None], on_back) -> tuple[View, dict]`. Handles: `collect() -> list[tuple[Account, Account]]` (src, dst per row) or raises `ValueError` with a user message; `preset_key() -> str | None`. Rows with all-empty fields are skipped.
- Extract from `build_account` a module helper `_apply_preset(preset_key, host, port, use_ssl, hint)` reused by both.

**Locale keys (add to en.json AND tr.json):**
`bulk.title`, `bulk.dest_title`, `bulk.add_row`, `bulk.remove_row`, `bulk.email`, `bulk.src_password`, `bulk.dst_password`, `bulk.start_all`, `bulk.no_rows`, `bulk.row_invalid`, `dash.new_bulk`, `settings.max_active`.

en.json values: `"Bulk migration"`, `"Destination server"`, `"Add account"`, `"Remove"`, `"Email"`, `"Source password"`, `"Destination password"`, `"Start all"`, `"Add at least one account"`, `"Row {n}: fill email and both passwords"`, `"New bulk"`, `"Max simultaneous transfers"`.
tr.json values: `"Toplu taşıma"`, `"Hedef sunucu"`, `"Hesap ekle"`, `"Kaldır"`, `"E-posta"`, `"Kaynak parola"`, `"Hedef parola"`, `"Hepsini başlat"`, `"En az bir hesap ekleyin"`, `"Satır {n}: e-posta ve iki parolayı doldurun"`, `"Toplu"`, `"En fazla eşzamanlı taşıma"`.

- [ ] **Step 1: Failing test** `tests/test_bulk_view.py`

```python
import pytest
flet = pytest.importorskip("flet")
from email_export_import.gui.i18n import I18n
from email_export_import.gui import views

EN = I18n(locale="en")

def _build(captured):
    def on_start(pairs, preset_key):
        captured.append((pairs, preset_key))
    view, handles = views.build_bulk(EN, on_start, on_back=lambda: None)
    return view, handles

def test_collect_skips_blank_rows_and_builds_pairs():
    view, h = _build([])
    # shared dest
    h["_dst"]["host"].value = "dst.test"; h["_dst"]["port"].value = "993"
    # one filled row, one blank
    h["_add_row"]()
    r0 = h["rows"]()[0]
    r0["email"].value = "a@x.com"; r0["src_pw"].value = "s"; r0["dst_pw"].value = "d"
    pairs = h["collect"]()
    assert len(pairs) == 1
    src, dst = pairs[0]
    assert src.email == "a@x.com" and src.password == "s"
    assert dst.email == "a@x.com" and dst.host == "dst.test" and dst.password == "d"

def test_collect_no_rows_raises():
    view, h = _build([])
    h["_dst"]["host"].value = "dst.test"
    with pytest.raises(ValueError):
        h["collect"]()

def test_collect_partial_row_raises():
    view, h = _build([])
    h["_dst"]["host"].value = "dst.test"
    h["_add_row"]()
    r0 = h["rows"]()[0]
    r0["email"].value = "a@x.com"  # missing passwords
    with pytest.raises(ValueError):
        h["collect"]()
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement `build_bulk`** in `views.py`. Source preset + dest fields once; a `ft.Column` of row dicts; `collect()` validates. Dest port defaults 993; ssl default True; verify default True. Source host/port/ssl come from the preset row (reuse `_apply_preset`). Each src Account uses the shared source host/port/ssl + row email/src_pw; each dst Account uses shared dest host/port/ssl/verify + row email (dest email = src email) + dst_pw. `collect()` raises `ValueError(i18n.t("bulk.no_rows"))` if no non-blank rows, `ValueError(i18n.t("bulk.row_invalid", n=idx+1))` for a partially-filled row. Handles dict exposes `collect`, `preset_key`, `rows` (callable → list of row dicts), `_add_row`, `_dst` (dict of dest fields) for tests. Show a `status` Text for validation errors when wired in app.

- [ ] **Step 4: Run** `uv run pytest tests/test_bulk_view.py tests/test_i18n.py -q` → PASS

- [ ] **Step 5: Commit** `feat(gui): bulk-entry view with validated collect()`

---

### Task 4: Dashboard "New bulk" button + Settings cap dropdown

**Files:**
- Modify: `email_export_import/gui/views.py` (`build_dashboard`, `build_settings`)
- Test: covered by Task 5 e2e + a settings assertion

**Interfaces:**
- `build_dashboard(..., on_new_bulk: Callable[[], None])` — new required kwarg; adds a button `i18n.t("dash.new_bulk")` next to New.
- `build_settings(..., max_active: int = 2, on_max_active: Callable[[int], None] | None = None)` — adds a dropdown (options "1".."4", value str(max_active), on_select → on_max_active(int)).

- [ ] **Step 1:** Add the `on_new_bulk` button in `build_dashboard`'s trailing controls (Row with New + New bulk).
- [ ] **Step 2:** Add cap dropdown to `build_settings` after the version/update block.
- [ ] **Step 3:** Run `uv run pytest tests/test_views*.py -q` (update any signature-based view tests to pass the new kwargs; default `on_new_bulk=lambda: None`).
- [ ] **Step 4: Commit** `feat(gui): New-bulk button + concurrency-cap setting`

---

### Task 5: App coordinator (AccountSpec, start_bulk, pump_bulk) + wiring

**Files:**
- Modify: `email_export_import/gui/app.py`
- Test: `tests/test_gui_e2e.py`

**Interfaces:**
- Consumes: `views.build_bulk`, `controller.test_connection`, `controller.build_plan`, `controller.default_skip`, `Run`, `RunManager.max_active`, `Run.mark_failed`, `prefs`.
- Produces (internal): `show_bulk()`, `start_bulk(pairs, preset_key)`, `pump_bulk()`.

- [ ] **Step 1: Failing e2e tests** (append to `tests/test_gui_e2e.py`) — helper to fill the bulk screen, then:

```python
def test_bulk_starts_all_with_cap_two(monkeypatch, tmp_path):
    # two accounts, both migrate (cap default 2)
    ...
    page = _run_page()
    assert _click(page.views[-1], EN("dash.new_bulk"))
    # fill dest + 2 rows, Start all
    ...
    assert _wait(lambda: len(dst_a.folders["INBOX"]) == 1 and len(dst_b.folders["INBOX"]) == 1)

def test_bulk_cap_one_queues_second(monkeypatch, tmp_path):
    # set max_active=1 via prefs; assert one running while other queued, then both done
    ...
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement coordinator.** Add near WizardState:

```python
    # bulk coordinator state (in _page_main)
    bulk_pending: list[tuple] = []          # (src Account, dst Account, preset_key)
    bulk_starting: set[str] = set()
```

`show_bulk()`: build `views.build_bulk(i18n, on_start=start_bulk, on_back=back_to_dashboard)`; push view.

`start_bulk(pairs, preset_key)`:
```python
    for src, dst in pairs:
        key = f"{src.email}__{dst.email}"
        existing = manager.get(key)
        if existing is not None and existing.is_active:
            continue
        state = MigrationState.for_pair(src.email, dst.email, base_dir=manager.state_dir)
        ph = Run(key=key, title=f"{src.email} → {dst.email}",
                 src_conn=None, dst_conn=None, plans=None, state=state,
                 workers=manager.default_workers(), total=0)
        manager.add(ph)
        bulk_pending.append((src, dst, preset_key))
    back_to_dashboard()
```

`pump_bulk()` (called from the poll, on the loop):
```python
    while (manager.active_count() + len(bulk_starting) < manager.max_active
           and bulk_pending):
        src, dst, preset_key = bulk_pending.pop(0)
        key = f"{src.email}__{dst.email}"
        bulk_starting.add(key)
        run_async(
            lambda s=src, d=dst, pk=preset_key: _bulk_connect(s, d, pk),
            on_done=ui(lambda built, k=key: _bulk_started(k, built)),
            on_error=ui(lambda exc, k=key: _bulk_failed(k, str(exc))),
        )
```

`_bulk_connect(src, dst, preset_key)`: mirror `_reconnect_and_build` but from Accounts:
```python
    sr = controller.test_connection(src)
    if not sr.ok:
        raise RuntimeError(sr.message or "source connection failed")
    try:
        dr = controller.test_connection(dst)
        if not dr.ok:
            raise RuntimeError(dr.message or "destination connection failed")
        try:
            skip = controller.default_skip(preset_key)
            plan = controller.build_plan(sr.conn, dr.conn, skip)
        except Exception:
            dr.conn.close(); raise
    except Exception:
        sr.conn.close(); raise
    return sr.conn, dr.conn, plan, skip
```

`_bulk_started(key, built)`:
```python
    src_conn, dst_conn, plan, skip = built
    active_plans = [p for p in plan.plans if p.source not in skip]
    total = sum(plan.counts.get(p.source, 0) for p in active_plans)
    run = Run(key=key, title=f"{src_conn.account.email} → {dst_conn.account.email}",
              src_conn=src_conn, dst_conn=dst_conn, plans=active_plans,
              state=MigrationState.for_pair(src_conn.account.email, dst_conn.account.email,
                                            base_dir=manager.state_dir),
              workers=manager.default_workers(), total=total, skip=set(skip))
    manager.add(run)      # replaces the queued placeholder
    run.start()
    bulk_starting.discard(key)
    highlight[0] = key
    refresh_current()
```

`_bulk_failed(key, message)`:
```python
    run = manager.get(key)
    if run is not None:
        run.mark_failed(message)
    bulk_starting.discard(key)
    refresh_current()
```

Wire: dashboard `on_new_bulk=show_bulk`; settings `max_active`/`on_max_active` reading/writing prefs and `manager.max_active`; at startup load `manager.max_active = prefs.load_prefs(i18n._prefs_path).get("max_active", 2)`. Add `pump_bulk()` call at the top of the poll loop body (inside the try), so queued specs pump on the loop every tick.

- [ ] **Step 4: Run** `uv run --extra gui pytest tests/test_gui_e2e.py -q` → PASS

- [ ] **Step 5: Full suite** `uv run --extra gui pytest -q` → PASS

- [ ] **Step 6: Commit** `feat(gui): bulk migration coordinator with capped auto-queue`

---

## Self-review notes

- Spec coverage: entry shape (T3), shared dest (T3), cap+queue (T2/T5), auto-skip folders (T5 via default_skip), settings cap persisted (T1/T4/T5), mark_failed visibility (T2/T5), duplicate guard (T5 start_bulk), no password persistence (placeholders never flush; connect passwords in-memory). ✓
- Cap counts active + starting (T5 pump_bulk). ✓
- Locale key parity enforced (T3 lists both en/tr). ✓
