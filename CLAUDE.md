# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync --extra gui                      # dev setup (gui extra needed to run/test the desktop app)
uv run pytest                            # full suite — fast, network-free
uv run pytest tests/test_transfer.py     # one file
uv run pytest tests/test_transfer.py -k quota   # one test by keyword

uv run email-export-import               # CLI wizard (non-interactive: --src-preset/--dst-host/... --yes,
                                         #   passwords via EEI_SRC_PASSWORD / EEI_DST_PASSWORD)
uv run email-export-import-gui           # Flet desktop app
uv run python -m email_export_import.daemon   # headless daemon (EEI_BASE_DIR overrides state dir)
```

There is no linter/formatter configured; pytest is the only gate. Packaging is CI-only (`.github/workflows/build-gui.yml`, triggered by tag push): `flet build` per OS plus a PyInstaller `eei-daemon` sidecar, macOS signed+notarized.

Development follows TDD: write the failing test first. Design specs and implementation plans live in `docs/superpowers/` (`specs/`, `plans/`, `daemon-architecture.md`).

## Architecture

One UI-agnostic engine, three front-ends that share the same on-disk state (`~/.email-export-import/`), so a migration can be started in one front-end and finished in another:

- **Engine** (top-level modules in `email_export_import/`): `transfer.py` (fetch → append, Message-ID dedup, quota abort), `connection.py` (IMAPClient wrapper: retry/backoff, friendly errors), `folders.py` (delimiter translation, SPECIAL-USE mapping, `INBOX.` namespaces), `state.py` (atomic JSON resume state, UIDVALIDITY invalidation), `spool.py`, `throttle.py`, `providers.py`, `models.py`, `errors.py`. No UI imports here.
- **CLI** (`cli.py`): Typer/Rich wizard + non-interactive flags; imports the engine directly, works without the daemon.
- **GUI** (`gui/`): Flet dashboard. `run_manager.py` is deliberately flet-free (one migration = one `Run`: own thread, cancel event, lock-guarded `RunSnapshot`) so all logic is testable headless. `views.py` renders; `app.py` wires handlers.
- **Daemon** (`daemon/`): headless process so migrations survive the GUI closing. Stdlib-only `ThreadingHTTPServer` on a random loopback port, token-guarded; port+token written 0600 to `daemon.json` in the state dir (the "rendezvous"). `lifecycle.connect_or_spawn()` reuses a live daemon or spawns one. Passwords travel per-request and live only in memory — never on disk, anywhere in this codebase.

### Backend abstraction (GUI ↔ engine/daemon)

`app.py` codes against a single backend shape, chosen in `_make_backend()`:

- `gui/local_backend.py` — `LocalBackend`, in-process `RunManager` + `Controller`.
- `gui/daemon_backend.py` — `DaemonBackend`, adapter over `DaemonClient` HTTP calls, reconstructs `RunSnapshot`s from wire dicts.

The two must stay method-for-method identical (parity is tested). Any capability added to one backend must be added to the other, plus the daemon endpoint (`daemon/server.py`) and `DaemonClient` (`daemon/client.py`) when it crosses the wire.

### Flet threading rule

UI mutation must happen on the Flet event loop (`page.run_task` / the `ui()` wrapper in `app.py`). IMAP and other blocking work runs off-thread (`Run` threads, `async_ops.run_async`). Mutating controls from a worker thread silently produces a dead/frozen UI — this was a real production bug.

### i18n

Every user-visible GUI string goes through `I18n.t(key)`. Add new keys to **both** `locales/en.json` and `locales/tr.json` — `test_i18n.py` asserts the key sets are identical. Missing keys fall back to the key string.

## Testing conventions

- **No network, ever.** `tests/fakes.py` provides `FakeIMAPClient`, an in-memory double implementing exactly the IMAPClient subset the tool uses (IMAPClient's own shapes: bytes keys, `(flags, delim, name)` tuples).
- **GUI e2e** (`test_gui_e2e.py`): a `FakePage` stands in for `ft.Page`, `_page_main()` runs for real, and tests invoke the actual click handlers headless.
- **Daemon tests** run the real HTTP server in-process against the fake IMAP client.

## Gotchas

- **`flet build` bundles ONLY `[project].dependencies`** — optional extras are invisible to it. A runtime dependency of the packaged app must go in the main dependency list (this is why `flet` and `certifi` are there; keep the `flet` pin in lockstep with the `gui` extra). See the comments in `pyproject.toml` before touching dependencies.
- **Version lives in two places**: `pyproject.toml` `[project].version` and `email_export_import/__init__.py` `__version__` — `test_version.py` fails if they diverge.
- **Entry points are wrappers**: `main.py` is the `flet build` entry (launches the GUI); `eei_daemon.py` is the PyInstaller sidecar entry (absolute imports, because a frozen script runs as `__main__` and breaks relative imports).
- State/spool/prefs files are written `0700`/`0600`; keep that when adding new persisted files.
