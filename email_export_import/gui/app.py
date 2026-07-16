from __future__ import annotations

import asyncio
import os
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import flet as ft

from .. import __version__
from .. import applog
from .. import secrets_store
from . import prefs
from . import sysinfo
from . import updater
from . import views
from .async_ops import run_async
from .controller import Controller
from .daemon_backend import DaemonBackend
from .i18n import I18n
from .local_backend import LocalBackend
from .run_manager import RunManager


@dataclass
class WizardState:
    # Account dicts (host/port/ssl/email/password/verify_ssl) — the GUI no
    # longer holds live IMAP connections; the backend connects and holds them,
    # returning only a plan_id.
    src: dict | None = None
    dst: dict | None = None
    skip: set[str] = field(default_factory=set)
    workers: int = 4
    spool: bool = False
    plan_id: str | None = None
    plan_folders: list = field(default_factory=list)
    plan_total: int = 0
    remember: bool = False  # save the passwords to the OS keychain on start
    # Set when the plan screen is entered from Resume (reuse the existing key
    # and title instead of minting a new session).
    resume_key: str | None = None
    resume_title: str | None = None


def _local_backend(prefs_values: dict):
    manager = RunManager()
    manager.load_resumable()
    manager.load_completed()
    backend = LocalBackend(manager, Controller())
    backend.set_max_active(prefs_values.get("max_active", 2))
    backend.set_workers(prefs_values.get("workers", 4))
    backend.set_rate_limit(prefs_values.get("rate_limit", 0))
    return backend


def _daemon_enabled() -> bool:
    # The daemon owns the persistent tray icon and keeps migrations running
    # when the GUI is closed — the intended model on every desktop OS. Set
    # EEI_NO_DAEMON=1 to force the in-process fallback (e.g. for debugging).
    import os

    return os.environ.get("EEI_NO_DAEMON") != "1"


def _make_backend(prefs_values: dict):
    """Prefer the out-of-process daemon (migrations survive the GUI closing);
    fall back to the in-process backend if ANYTHING about the daemon path
    fails — spawn, connect, or the initial settings push. Startup must never
    crash to a blank window over the daemon; the app always works in-process."""
    if not _daemon_enabled():
        return _local_backend(prefs_values)
    try:
        from ..daemon.lifecycle import connect_or_spawn

        # The macOS daemon runs from a copy outside the .app; its first cold
        # start (PyInstaller onefile extraction + Gatekeeper verification) was
        # measured at ~12s, so allow generous headroom or we fall back to the
        # in-process backend and lose the tray.
        client = connect_or_spawn(timeout=25)
        if client is not None:
            backend = DaemonBackend(client)
            backend.set_max_active(prefs_values.get("max_active", 2))
            backend.set_workers(prefs_values.get("workers", 4))
            backend.set_rate_limit(prefs_values.get("rate_limit", 0))
            return backend
    except Exception:
        pass  # any daemon trouble → run in-process instead
    return _local_backend(prefs_values)


def _signal_existing_gui() -> bool:
    """Single-instance guard: if a GUI is already running (heartbeating the
    daemon), ask the daemon to reveal that window and return True so this launch
    bows out instead of opening a second, blank window."""
    if not _daemon_enabled():
        return False
    try:
        from ..daemon import lifecycle
        from ..state import DEFAULT_BASE_DIR

        info = lifecycle._read_rendezvous(DEFAULT_BASE_DIR)
        if not info:
            return False
        client = lifecycle._client_for(info)
        if client.is_alive() and client.gui_alive():
            client.request_show()
            applog.log("gui", "existing GUI is alive; requested show, bowing out")
            return True
    except Exception:
        pass
    return False


def main() -> None:
    applog.log("gui", f"launch exe={sys.executable}")
    if _signal_existing_gui():
        return
    ft.app(target=_page_main)
    # ft.app() returns once the window is closed natively. Force the process to
    # end so no window-less interpreter lingers (a resident GUI would make the
    # daemon think one is alive and "Show window" would focus nothing).
    applog.log("gui", "ft.app returned; exiting process")
    os._exit(0)


def _report_startup_crash(page: ft.Page, tb: str) -> None:
    """A packaged app has no console, so a startup crash is otherwise an
    unexplained blank window. Write the traceback to a file the user can find
    and show it on-screen instead of nothing."""
    from ..state import DEFAULT_BASE_DIR

    log_path = DEFAULT_BASE_DIR / "crash.log"
    try:
        DEFAULT_BASE_DIR.mkdir(parents=True, exist_ok=True)
        log_path.write_text(tb, encoding="utf-8")
    except Exception:
        pass
    try:
        page.views.clear()
        page.views.append(ft.View(
            controls=[
                ft.Text("The app hit a startup error. Please send this:",
                        weight=ft.FontWeight.BOLD, color=ft.Colors.RED),
                ft.Text(str(log_path), size=11, selectable=True),
                ft.Text(tb, size=11, selectable=True, font_family="monospace"),
            ],
            scroll=ft.ScrollMode.AUTO, padding=16,
        ))
        page.update()
    except Exception:
        pass


def _splash_view(text_control: ft.Text) -> ft.View:
    """Shown instantly at launch so the window is never blank while we connect
    to (or cold-start + spawn) the daemon, which can take several seconds on a
    first launch. The caller holds `text_control` to rotate progress messages so
    the wait reads as progress, not a hang."""
    return ft.View(
        controls=[
            ft.Column(
                [ft.ProgressRing(width=44, height=44), text_control],
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=16, expand=True,
            )
        ],
        vertical_alignment=ft.MainAxisAlignment.CENTER,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
    )


def _page_main(page: ft.Page) -> None:
    try:
        i18n = I18n()
        _prefs = prefs.load_prefs(i18n._prefs_path)
        # Paint the spinner NOW, then do the (blocking) daemon connect OFF the
        # event loop — asyncio.to_thread yields the loop so this splash frame
        # actually reaches the client. Doing connect_or_spawn inline on the loop
        # thread instead left the window blank for several seconds on cold start.
        splash_text = ft.Text(i18n.t("app.starting"), size=13)
        page.views.clear()
        page.views.append(_splash_view(splash_text))
        page.update()

        boot_done = {"v": False}
        stages = [i18n.t("app.starting"), i18n.t("splash.waking"),
                  i18n.t("splash.connecting"), i18n.t("splash.almost")]

        async def _ticker() -> None:
            # Rotate the message during a long first-launch cold start so the
            # spinner reads as progress rather than a hang on one static word.
            i = 1
            while not boot_done["v"]:
                await asyncio.sleep(2.2)
                if boot_done["v"]:
                    break
                splash_text.value = stages[i % len(stages)]
                try:
                    splash_text.update()
                except Exception:
                    pass
                i += 1

        async def _boot() -> None:
            try:
                backend = await asyncio.to_thread(_make_backend, _prefs)
                applog.log("gui", f"booted backend={type(backend).__name__}")
                _run_app(page, backend, i18n, _prefs)
            except Exception:  # noqa: BLE001
                applog.log("gui", "startup crash:\n" + traceback.format_exc())
                _report_startup_crash(page, traceback.format_exc())
            finally:
                boot_done["v"] = True

        page.run_task(_ticker)
        page.run_task(_boot)
    except Exception:  # noqa: BLE001
        _report_startup_crash(page, traceback.format_exc())


def _run_app(page: ft.Page, backend, i18n: I18n, _prefs: dict) -> None:
    # The daemon should run at login so migrations continue across reboots.
    # The user chose always-on; install once on first launch, then respect the
    # Settings toggle. Only meaningful for the daemon backend.
    if isinstance(backend, DaemonBackend) and not _prefs.get("autostart_done"):
        try:
            from ..daemon import autostart
            autostart.install()
        except Exception:
            pass  # autostart is a convenience; never let it block startup
        prefs.save_pref(i18n._prefs_path, "autostart_done", True)
    ws = WizardState()
    highlight: list[str | None] = [None]
    # Bulk coordinator: pending (src, dst, preset_key) specs waiting for a slot,
    # and the keys whose connect+plan is currently in flight (counted against
    # the cap so no more than max_active logins happen at once).
    bulk_pending: list[tuple] = []
    bulk_starting: set[str] = set()
    page.title = i18n.t("app.title")
    page.window.width = 820
    page.window.height = 680

    # Buttons don't get a pointer cursor by default here, so hovering a live
    # button feels identical to hovering dead text — the UI reads as broken.
    # Set the hand cursor once, app-wide, for every button variant.
    def _cursor_style() -> ft.ButtonStyle:
        return ft.ButtonStyle(mouse_cursor=ft.MouseCursor.CLICK)

    page.theme = ft.Theme(
        filled_button_theme=ft.FilledButtonTheme(style=_cursor_style()),
        text_button_theme=ft.TextButtonTheme(style=_cursor_style()),
        outlined_button_theme=ft.OutlinedButtonTheme(style=_cursor_style()),
        icon_button_theme=ft.IconButtonTheme(style=_cursor_style()),
        button_theme=ft.ButtonTheme(style=_cursor_style()),
    )

    def safe_update(control) -> None:
        try:
            control.update()
        except RuntimeError:
            pass  # page closed / control unmounted

    # ---- closing a daemon-backed window ---------------------------------
    # Closing a daemon-backed window FULLY EXITS this viewer process; the daemon
    # (a separate process) keeps migrating and owns the tray, and "Show window"
    # relaunches a fresh GUI.
    #
    # Two hard-won lessons from Flet/macOS shape this:
    #  * HIDING the window (visible=False) disconnects the Flet client and
    #    freezes the poll loop, so a hidden GUI can be neither revealed nor quit.
    #    Never hide — exit.
    #  * prevent_close=True + os._exit() leaves a FROZEN GHOST window: macOS asks
    #    the Flutter client to defer the close to Python, Python then os._exit()s
    #    without ever telling Flutter to close, so the window process is orphaned
    #    on screen (looks like the red-X "does nothing"). So we DON'T prevent the
    #    close — Flutter tears its own window down (no ghost) — and only make the
    #    Python side follow.
    def _exit_process() -> None:
        os._exit(0)

    def _close_and_exit(reason: str) -> None:
        """End this viewer from code (tray Quit / daemon lost / menu Quit): ask
        Flet to close the window so its Flutter client tears down cleanly, then
        hard-exit as a bounded fallback (Flet's close is unreliable across
        versions, so we never rely on it alone)."""
        applog.log("gui", f"close viewer: {reason}")
        try:
            page.run_task(page.window.close)
        except Exception:
            pass
        threading.Timer(1.5, _exit_process).start()

    def _reveal_window() -> None:
        # Reached via the tray show flag when a launch focuses an already-open
        # GUI; a closed GUI is gone, not hidden, so it is relaunched instead.
        try:
            page.window.skip_task_bar = False
            page.window.visible = True
            page.window.focused = True
            page.update()
            page.run_task(page.window.to_front)
        except Exception:
            pass

    if isinstance(backend, DaemonBackend):
        # NOTE: no prevent_close — the red-X closes the Flutter window natively
        # (no ghost). We only ensure the Python process follows.
        def _on_window_event(e) -> None:
            etype = getattr(e, "type", None)
            applog.log("gui", f"window event type={etype!r}")
            is_close = etype == ft.WindowEventType.CLOSE or \
                str(getattr(etype, "value", etype)).lower() == "close"
            if is_close:
                applog.log("gui", "window close -> exit viewer (daemon keeps running)")
                # The window is already closing natively; let Flutter finish its
                # teardown, then make sure Python exits too (no window-less
                # lingering process). A ghost is impossible here.
                threading.Timer(0.4, _exit_process).start()

        page.window.on_event = _on_window_event

    # All dialogs go through these wrappers so the poll knows when a modal
    # is open: its 5x/second page.update() must pause then, or it races the
    # dialog's own click handling and the buttons feel dead.
    dialog_open = [0]

    def show_dialog(dlg) -> None:
        dialog_open[0] += 1
        page.show_dialog(dlg)

    def pop_dialog() -> None:
        if dialog_open[0] > 0:
            dialog_open[0] -= 1
        page.pop_dialog()
        try:
            page.update()  # commit the dismiss
        except Exception:
            pass

    def _defer(fn) -> None:
        """Run fn AFTER the current dialog's dismiss animation. A dialog's
        dismiss is animated/async: rebuilding page.views (or opening another
        dialog) in the same handler races the animation and can leave the
        dialog stuck on screen (seen on Windows). Deferring past it makes the
        close reliable."""
        async def _later():
            await asyncio.sleep(0.3)
            try:
                fn()
            except Exception:
                pass

        page.run_task(_later)

    def close_then(fn) -> None:
        """Close the current dialog, then run fn once its dismiss finishes."""
        pop_dialog()
        _defer(fn)

    # Blocking-work indicator: a dimmed full-page layer with a centred spinner.
    # Connecting/planning against a rate-limiting server can take many seconds
    # (login backoff), and without this the app just looks frozen.
    loading_layer = ft.Container(
        expand=True,
        bgcolor=ft.Colors.with_opacity(0.45, ft.Colors.BLACK),
        alignment=ft.Alignment.CENTER,
        visible=False,
        content=ft.Column(
            [
                ft.ProgressRing(width=48, height=48),
                ft.Text("", size=13, color=ft.Colors.WHITE),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=14,
            tight=True,
        ),
    )
    loading_text = loading_layer.content.controls[1]
    page.overlay.append(loading_layer)

    def show_loading(on: bool, message: str = "") -> None:
        loading_text.value = message
        loading_layer.visible = on
        safe_update(loading_layer)

    def ui(fn):
        """Marshal a callback onto the Flet *event loop* thread.

        Flet 0.85's control.update()/page.update() run the diff engine (mutating
        shared session state) and enqueue frames on a loop-bound asyncio.Queue
        with no thread marshalling — they are race-free ONLY on the event-loop
        thread. run_async delivers its result on a bare worker thread, and
        page.run_thread would hop to the executor (still off-loop): mutating the
        UI from either races the loop's own click handling and desyncs the
        client, leaving buttons that render but no longer respond.

        run_task run_coroutine_threadsafe's the wrapped callback ONTO the loop —
        the one place UI mutation is safe. Every run_async callback that touches
        the UI must go through here.
        """

        async def _on_loop(*a, **k):
            fn(*a, **k)

        return lambda *a, **k: page.run_task(_on_loop, *a, **k)

    # ---- dashboard ------------------------------------------------------

    def set_locale(locale: str) -> None:
        i18n.set_locale(locale)

        def _reopen() -> None:
            show_dashboard()  # chrome + rows in the new language
            show_settings()   # reopen the dialog, re-rendered
        close_then(_reopen)

    def set_max_active(n: int) -> None:
        backend.set_max_active(n)
        prefs.save_pref(i18n._prefs_path, "max_active", n)

    def set_workers(n: int) -> None:
        backend.set_workers(n)
        prefs.save_pref(i18n._prefs_path, "workers", n)

    def set_rate_limit(n: int) -> None:
        backend.set_rate_limit(n)
        prefs.save_pref(i18n._prefs_path, "rate_limit", n)

    def safe_mode() -> None:
        """One click to the gentlest settings: a single connection, one run at a
        time, and a paced upload. For machines whose network stack falls over
        under sustained bulk transfer."""
        set_workers(1)
        set_max_active(1)
        set_rate_limit(2 * 1024 * 1024)
        close_then(show_settings)  # reopen so the dropdowns show the new values

    def set_autostart(on: bool) -> None:
        from ..daemon import autostart
        autostart.install() if on else autostart.remove()

    def show_settings() -> None:
        from ..state import DEFAULT_BASE_DIR
        from ..daemon import autostart

        # Autostart only makes sense with the daemon backend.
        show_autostart = isinstance(backend, DaemonBackend)
        show_dialog(views.build_settings(
            i18n, str(DEFAULT_BASE_DIR), on_locale=set_locale,
            on_back=pop_dialog, version=__version__,
            on_check_update=lambda: _check_updates(manual=True),
            max_active=backend.max_active, on_max_active=set_max_active,
            workers=backend.workers, on_workers=set_workers,
            rate_limit=backend.rate_limit, on_rate_limit=set_rate_limit,
            on_safe_mode=safe_mode, tso_on=sysinfo.tso_enabled(),
            autostart_on=autostart.is_installed() if show_autostart else None,
            on_autostart=set_autostart if show_autostart else None,
        ))

    def show_dashboard() -> None:
        backend.refresh()  # freshen the snapshot cache before rendering
        page.views.clear()
        page.views.append(_main_view())
        page.update()

    # Live-render bookkeeping so the poll can update values in place instead of
    # rebuilding the view every tick (a rebuild recreates every button and
    # kills hover/click on the rows).
    render = {"dash_refs": {}, "dash_sig": None}

    # ---- master-detail main view -----------------------------------------

    selected_key: list[str | None] = [None]

    def select_run(key: str) -> None:
        selected_key[0] = key
        show_dashboard()

    def _main_extras(snap) -> list:
        if snap is None:
            return []
        if snap.status == "running":
            return [(ft.Icons.PAUSE, i18n.t("dash.pause"),
                     lambda: do_pause(snap.key))]
        if snap.status in ("paused", "cancelled"):
            return [(ft.Icons.PLAY_ARROW, i18n.t("dash.resume"),
                     lambda: ask_resume(snap.key))]
        if snap.status == "done":
            return [(ft.Icons.SYNC, i18n.t("dash.sync"),
                     lambda: ask_resume(snap.key))]
        return []

    def _main_view() -> ft.View:
        snaps = backend.snapshot_all()
        if highlight[0] is not None:
            selected_key[0] = highlight[0]
            highlight[0] = None
        if selected_key[0] is None or not any(
            s.key == selected_key[0] for s in snaps
        ):
            selected_key[0] = snaps[0].key if snaps else None
        sel = next((s for s in snaps if s.key == selected_key[0]), None)
        cfg = backend.config_for(sel.key) if sel is not None else None
        refs: dict = {}
        view = views.build_main(
            i18n, snaps, selected_key[0], cfg,
            on_select=select_run,
            on_pause=do_pause, on_resume=ask_resume, on_cancel=do_cancel,
            on_dismiss=do_dismiss,
            on_edit=lambda: _edit_dialog(selected_key[0]),
            refs=refs,
            on_new=start_wizard,
            folder_counts=backend.folder_counts(sel.key) if sel is not None else None,
            last_run=backend.last_run(sel.key) if sel is not None else None,
        )
        render["dash_refs"] = refs
        render["dash_sig"] = (views.dashboard_signature(snaps), selected_key[0])
        return _decorate(view, _main_extras(sel), scrollable=False)

    def do_pause(key: str) -> None:
        backend.pause(key)
        refresh_current()

    def do_cancel(key: str) -> None:
        # Cancelling is a click away on every card, so guard it behind an
        # "are you sure?" confirmation — a misclick must not discard a run.
        def confirm(_e=None) -> None:
            backend.cancel(key)
            close_then(refresh_current)

        show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text(i18n.t("cancel.confirm_title")),
                content=ft.Text(i18n.t("cancel.confirm_body")),
                actions=[
                    ft.TextButton(i18n.t("cancel.confirm_no"), on_click=lambda e: pop_dialog()),
                    ft.FilledButton(i18n.t("cancel.confirm_yes"), on_click=confirm),
                ],
            )
        )

    def do_dismiss(key: str) -> None:
        backend.remove(key)
        show_dashboard()

    # ---- resume ---------------------------------------------------------

    def ask_resume(key: str) -> None:
        cfg = backend.config_for(key)
        if cfg is None:
            return
        title = next((s.title for s in backend.snapshot_all() if s.key == key), key)
        src_cfg, dst_cfg = cfg.get("src", {}), cfg.get("dst", {})
        # Pre-fill from the OS keychain if the user chose to remember before.
        src_saved = secrets_store.get_password(
            src_cfg.get("host", ""), src_cfg.get("email", ""), "source") or ""
        dst_saved = secrets_store.get_password(
            dst_cfg.get("host", ""), dst_cfg.get("email", ""), "dest") or ""

        # If a password was already remembered for this pair, default the plan
        # step's "remember" to on so unticking there is what forgets it.
        had_saved = bool(src_saved or dst_saved)

        def submit(src_pw: str, dst_pw: str) -> None:
            pop_dialog()
            src_dict = _account_dict(src_cfg, src_pw, "EEI_SRC_PASSWORD")
            dst_dict = _account_dict(dst_cfg, dst_pw, "EEI_DST_PASSWORD")
            # Reconnecting + reading folders can take a while on a slow or
            # rate-limiting server — show the spinner so it never looks frozen.
            show_loading(True, i18n.t("account.testing"))
            run_async(
                lambda: backend.plan(src_dict, dst_dict, sorted(cfg.get("skip", []))),
                on_done=ui(lambda plan: _start_resumed(key, title, cfg,
                                                       src_dict, dst_dict, plan,
                                                       had_saved)),
                on_error=ui(lambda exc: _show_error(str(exc))),
            )

        show_dialog(
            views.build_password_dialog(
                i18n, title, submit, lambda: pop_dialog(),
                src_prefill=src_saved, dst_prefill=dst_saved,
            )
        )

    def _start_resumed(key: str, title: str, cfg: dict,
                       src_dict: dict, dst_dict: dict, plan: dict,
                       remember_default: bool = False) -> None:
        # Resume routes through the plan screen so the user can choose which
        # folders to transfer (and adjust workers/spool) right before starting.
        nonlocal ws
        show_loading(False)
        ws = WizardState()
        ws.src, ws.dst = src_dict, dst_dict
        ws.remember = remember_default
        ws.plan_id = plan["plan_id"]
        ws.plan_folders = plan["folders"]
        ws.plan_total = plan["total"]
        ws.skip = set(cfg.get("skip", []))
        # Deliberately NOT cfg["workers"]: that records what the run used last
        # time. The current setting is the user's live "how hard to push" knob —
        # lowering it must actually take effect on resume (they can still
        # override per-run on the plan screen).
        ws.workers = backend.default_workers()
        ws.spool = cfg.get("spool", False)
        ws.resume_key = key
        ws.resume_title = title
        _render_plan()

    def _account_dict(cfg: dict, password: str, env_var: str) -> dict:
        import os

        return {
            "host": cfg["host"], "port": cfg.get("port", 993),
            "ssl": cfg.get("ssl", True), "email": cfg["email"],
            "password": password or os.environ.get(env_var, ""),
            "verify_ssl": cfg.get("verify_ssl", True),
        }

    def _show_error(message: str) -> None:
        show_loading(False)  # any failed transition must drop the spinner
        show_dialog(
            ft.AlertDialog(title=ft.Text(i18n.t("status.error")), content=ft.Text(message))
        )

    # ---- auto-update ------------------------------------------------------

    def _check_updates(manual: bool) -> None:
        if manual:
            _info_dialog(i18n.t("update.checking"))
        run_async(
            lambda: updater.check_for_update(__version__),
            on_done=ui(lambda info: _on_update_checked(info, manual)),
            on_error=ui(lambda exc: _on_update_checked(None, manual)),
        )

    def _on_update_checked(info, manual: bool) -> None:
        pop_dialog()  # clear the "checking…" dialog if it was shown
        if info is None:
            if manual:
                _info_dialog(i18n.t("update.up_to_date"))
            return
        show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text(i18n.t("app.title")),
                content=ft.Text(i18n.t("update.available", version=info.version)),
                actions=[
                    ft.TextButton(i18n.t("update.later"), on_click=lambda e: pop_dialog()),
                    ft.FilledButton(i18n.t("update.now"), on_click=lambda e: _do_update(info)),
                ],
            )
        )

    def _do_update(info) -> None:
        pop_dialog()
        _info_dialog(i18n.t("update.downloading"))
        run_async(
            lambda: updater.download_asset(info, Path.home() / "Downloads"),
            on_done=ui(lambda path: _update_downloaded(path)),
            on_error=ui(lambda exc: _info_dialog(i18n.t("update.failed"))),
        )

    def _update_downloaded(path) -> None:
        try:
            updater.open_installer(path)
        except Exception:
            _info_dialog(i18n.t("update.failed"))
            return
        _info_dialog(i18n.t("update.ready"))

    def _info_dialog(message: str) -> None:
        pop_dialog()  # never stack over a previous update dialog
        show_dialog(
            ft.AlertDialog(
                title=ft.Text(i18n.t("app.title")),
                content=ft.Text(message),
                actions=[ft.TextButton(i18n.t("update.close"), on_click=lambda e: pop_dialog())],
            )
        )

    # ---- edit connection (dialog) ----------------------------------------

    def _edit_dialog(key: str | None) -> None:
        if key is None:
            return
        cfg = backend.config_for(key)
        if not cfg:
            return
        cfg = dict(cfg)
        src_controls, src_collect = views._account_editor(
            i18n, i18n.t("account.source_title"), cfg.get("src", {}))
        dst_controls, dst_collect = views._account_editor(
            i18n, i18n.t("account.dest_title"), cfg.get("dst", {}))

        def save(_e=None) -> None:
            cfg["src"] = src_collect()
            cfg["dst"] = dst_collect()
            backend.save_config(key, cfg)
            close_then(show_dashboard)

        show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text(i18n.t("detail.edit")),
                content=ft.Column(
                    list(src_controls) + [ft.Divider()] + list(dst_controls),
                    tight=True, scroll=ft.ScrollMode.AUTO, width=420,
                ),
                actions=[
                    ft.TextButton(i18n.t("resume.cancel"),
                                  on_click=lambda e: pop_dialog()),
                    ft.FilledButton(i18n.t("detail.save"), on_click=save),
                ],
            )
        )

    def show_detail(key: str) -> None:
        select_run(key)  # detail lives in the side panel now

    def back_to_dashboard() -> None:
        show_dashboard()

    def refresh_current() -> None:
        if page.views and page.views[-1].route == "/":
            show_dashboard()

    # ---- background poll ------------------------------------------------

    async def poll() -> None:
        # Runs on the event loop (page.run_task), so every control.update() /
        # page.update() below happens on the one thread where Flet's patch
        # machinery is race-free. A background executor thread here would fight
        # the loop's click handling and desync the client (dead buttons).
        while True:
            await asyncio.sleep(0.2)
            try:
                # Heartbeat the daemon and pick up tray requests. This runs every
                # tick (even behind a dialog / while hidden) so "Show window"
                # reveals THIS window and "Quit" from the tray closes it.
                ev = backend.poll_events()
                if ev.get("quit"):
                    # Tray asked to fully quit: close the window (so its Flutter
                    # client tears down — no ghost) then exit the process.
                    _close_and_exit("tray quit")
                    return
                if ev.get("daemon_lost"):
                    # The daemon we depend on has been unreachable long enough to
                    # be considered gone (tray Quit killed it, or it crashed). A
                    # viewer with no daemon is useless and must not linger — close
                    # the window and exit so a relaunch starts cleanly.
                    _close_and_exit("daemon lost")
                    return
                if ev.get("show"):
                    applog.log("gui", "tray show received; revealing window")
                    _reveal_window()
                pump_bulk()  # start queued bulk accounts as slots free (on-loop)
                if dialog_open[0]:
                    # A modal dialog is up: pushing page.update() 5x/second
                    # races the dialog's own click round-trips and its buttons
                    # go dead. Progress can wait until the dialog closes.
                    continue
                if not page.views:
                    continue
                route = page.views[-1].route
                if route == "/":
                    snaps = backend.refresh()  # freshen cache; returns snapshots
                    sig = (views.dashboard_signature(snaps), selected_key[0])
                    if sig == render["dash_sig"]:
                        # Same rows, same statuses, same selection — only
                        # progress moved. Update values in place; leave the
                        # row/button controls untouched.
                        views.apply_main_values(render["dash_refs"], snaps,
                                                i18n, selected_key[0])
                        for key, entry in render["dash_refs"].items():
                            safe_update(entry["counter"])
                            if entry.get("bar") is not None:
                                safe_update(entry["bar"])
                            if key == "_panel":
                                safe_update(entry["folder"])
                        page.update()  # also the liveness probe: raises once the page is gone
                    elif page.views and page.views[-1].route == "/":
                        # Row set, a status, or the selection changed —
                        # buttons differ, so a full rebuild is correct (rare).
                        page.views[-1] = _main_view()
                        page.update()
            except RuntimeError:
                return  # page closed / controls unmounted
            except Exception:
                continue  # transient race — skip this frame

    # ---- wizard ---------------------------------------------------------

    def start_wizard() -> None:
        nonlocal ws
        ws = WizardState()
        go_account("source")

    def go_account(role: str, prefill: dict | None = None) -> None:
        status = ft.Text("")
        initial = dict(prefill or {})
        if role == "dest" and ws.src and not initial.get("email"):
            initial["email"] = ws.src["email"]

        handles: dict = {}

        def _to_dict(account) -> dict:
            return {"host": account.host, "port": account.port, "ssl": account.ssl,
                    "email": account.email, "password": account.password,
                    "verify_ssl": account.verify_ssl}

        def on_test(account) -> None:
            status.value = i18n.t("account.testing")
            safe_update(status)
            handles["set_busy"](True)
            show_loading(True, i18n.t("account.testing"))
            acc = _to_dict(account)
            run_async(
                lambda: backend.test_connection(acc),
                on_done=ui(lambda result: _test_done(account, acc, result)),
                on_error=ui(lambda exc: _test_done(account, acc, None, exc)),
            )

        def _stop_busy() -> None:
            show_loading(False)
            handles["set_busy"](False)

        def _test_done(account, acc: dict, result, exc: Exception | None = None) -> None:
            if exc is not None:
                _stop_busy()
                status.value = str(exc)
                safe_update(status)
                return
            if result.get("ok"):
                if role == "source":
                    _stop_busy()
                    status.value = i18n.t("account.connected")
                    safe_update(status)
                    ws.src = acc
                    ws.skip = Controller.default_skip(handles["preset_key"]())
                    go_account("dest")
                else:
                    ws.dst = acc
                    # Keep the dialog's spinner turning and show what's happening
                    # while the folder plan is built — go_plan replaces this
                    # dialog once it's ready. (The full-page loading overlay sits
                    # behind the modal, so the inline spinner is what's visible.)
                    status.value = i18n.t("loading.plan")
                    safe_update(status)
                    go_plan()
            elif result.get("kind") == "cert":
                _stop_busy()
                _cert_dialog(account)
            else:
                _stop_busy()
                status.value = i18n.t(f"error.{result.get('kind')}")
                safe_update(status)

        def _cert_dialog(account) -> None:
            def retry_unverified(e) -> None:
                pop_dialog()
                account.verify_ssl = False
                on_test(account)  # async again — no UI freeze

            dialog = ft.AlertDialog(
                modal=True,
                title=ft.Text(i18n.t("cert.title")),
                content=ft.Text(i18n.t("cert.body")),
                actions=[
                    ft.TextButton(i18n.t("cert.cancel"), on_click=lambda e: pop_dialog()),
                    ft.FilledButton(i18n.t("cert.continue"), on_click=retry_unverified),
                ],
            )
            show_dialog(dialog)

        def on_back() -> None:
            if role == "source":
                pop_dialog()  # closing the first step closes the wizard
            else:
                go_account("source")

        dlg, view_handles = views.build_account(i18n, role, initial, on_test, on_back, status)
        handles.update(view_handles)
        pop_dialog()  # replace the previous wizard step, if any
        show_dialog(dlg)

    def go_plan() -> None:
        ws.workers = backend.default_workers()

        def plan_ready(plan) -> None:
            show_loading(False)
            ws.plan_id = plan["plan_id"]
            ws.plan_folders = plan["folders"]
            ws.plan_total = plan["total"]
            _render_plan()

        show_loading(True, i18n.t("loading.plan"))
        run_async(
            lambda: backend.plan(ws.src, ws.dst, sorted(ws.skip)),
            on_done=ui(plan_ready),
            on_error=ui(lambda exc: _show_error(str(exc))),
        )

    def _render_plan() -> None:
        def on_toggle(source: str, included: bool) -> None:
            (ws.skip.discard if included else ws.skip.add)(source)
            # counts change → rebuild the dialog
            pop_dialog()
            show_dialog(_plan_dialog())

        def on_workers(n: int) -> None:
            ws.workers = n

        def on_spool(enabled: bool) -> None:
            ws.spool = enabled

        def on_remember(enabled: bool) -> None:
            ws.remember = enabled

        def _plan_dialog() -> ft.AlertDialog:
            back = pop_dialog if ws.resume_key else (lambda: go_account("dest"))
            return views.build_plan(
                i18n, ws.plan_folders, ws.skip, ws.workers, ws.spool,
                on_toggle, on_workers, on_spool, start_migration, back,
                can_remember=secrets_store.available(),
                remember=ws.remember, on_remember=on_remember,
            )

        pop_dialog()  # replace the account step / previous plan render
        show_dialog(_plan_dialog())

    def _remember_wizard_passwords() -> None:
        # The plan step's "remember" is authoritative: ticked saves the pair to
        # the OS keychain, unticked forgets any previously stored password.
        if not (ws.src and ws.dst):
            return
        if ws.remember:
            secrets_store.save_password(ws.src.get("host", ""), ws.src.get("email", ""),
                                        "source", ws.src.get("password", ""))
            secrets_store.save_password(ws.dst.get("host", ""), ws.dst.get("email", ""),
                                        "dest", ws.dst.get("password", ""))
        else:
            secrets_store.delete_password(ws.src.get("host", ""),
                                          ws.src.get("email", ""), "source")
            secrets_store.delete_password(ws.dst.get("host", ""),
                                          ws.dst.get("email", ""), "dest")

    def start_migration() -> None:
        pop_dialog()  # close the plan dialog now (commits its dismiss)
        key = ws.resume_key or f"{ws.src['email']}__{ws.dst['email']}"
        existing = next((s for s in backend.snapshot_all() if s.key == key), None)
        if existing is not None and existing.status in ("running", "stopping"):
            highlight[0] = key
            _defer(back_to_dashboard)
            return
        title = ws.resume_title or f"{ws.src['email']} → {ws.dst['email']}"
        _remember_wizard_passwords()  # keychain, if the user opted in
        try:
            key = backend.start(ws.plan_id, sorted(ws.skip), ws.workers,
                                ws.spool, title)
        except Exception as exc:  # noqa: BLE001
            _defer(lambda: _show_error(str(exc)))
            return
        highlight[0] = key
        _defer(back_to_dashboard)

    # ---- bulk ----------------------------------------------------------

    def show_bulk() -> None:
        dlg, _handles = views.build_bulk(i18n, on_start=start_bulk, on_back=pop_dialog)
        show_dialog(dlg)

    def _acc_dict(account) -> dict:
        return {"host": account.host, "port": account.port, "ssl": account.ssl,
                "email": account.email, "password": account.password,
                "verify_ssl": account.verify_ssl}

    def start_bulk(pairs: list, preset_key: str | None) -> None:
        pop_dialog()  # the bulk dialog
        # Add a queued placeholder card per account immediately, then let the
        # poll pump them onto real runs as slots free (cap = max_active).
        active_keys = {s.key for s in backend.snapshot_all()
                       if s.status in ("running", "stopping")}
        for src, dst in pairs:
            key = f"{src.email}__{dst.email}"
            if key in active_keys:
                highlight[0] = key
                continue  # already running for this pair — skip the duplicate
            backend.add_placeholder(src.email, dst.email)
            bulk_pending.append((_acc_dict(src), _acc_dict(dst), preset_key))
        back_to_dashboard()

    def pump_bulk() -> None:
        # Runs on the event loop (called from poll). Start connects until the
        # cap (active runs + in-flight connects) is reached.
        while (
            backend.active_count() + len(bulk_starting) < backend.max_active
            and bulk_pending
        ):
            src, dst, preset_key = bulk_pending.pop(0)
            key = f"{src['email']}__{dst['email']}"
            bulk_starting.add(key)
            run_async(
                lambda s=src, d=dst, pk=preset_key: _bulk_build(s, d, pk),
                on_done=ui(lambda built, k=key: _bulk_started(k, built)),
                on_error=ui(lambda exc, k=key: _bulk_failed(k, str(exc))),
            )

    def _bulk_build(src: dict, dst: dict, preset_key: str | None):
        skip = sorted(Controller.default_skip(preset_key))
        plan = backend.plan(src, dst, skip)  # raises on connect failure
        return plan, skip

    def _bulk_started(key: str, built) -> None:
        bulk_starting.discard(key)
        plan, skip = built
        backend.start(plan["plan_id"], skip, backend.default_workers())
        highlight[0] = key
        refresh_current()

    def _bulk_failed(key: str, message: str) -> None:
        bulk_starting.discard(key)
        backend.mark_failed(key, message)
        refresh_current()

    # ---- closing the window ---------------------------------------------
    # The persistent tray icon and the migration engine live in the daemon
    # (a separate process), so closing the GUI is just closing a viewer: the
    # daemon keeps migrating and its menu-bar / system-tray icon stays, ready
    # to reopen this window. So the close button simply quits the GUI.
    #
    # The in-process fallback (EEI_NO_DAEMON / no daemon) has no separate
    # process, so quitting there DOES stop active migrations — confirm first.

    def _work_pending() -> bool:
        return any(
            s.status in ("running", "stopping", "queued")
            for s in backend.snapshot_all()
        )

    def request_quit() -> None:
        if isinstance(backend, DaemonBackend):
            # The daemon keeps migrating and owns the tray, so this is just
            # closing the viewer — close the window cleanly then exit.
            _close_and_exit("menu quit")
            return
        if not _work_pending():
            page.run_task(page.window.destroy)  # no daemon: nothing to keep alive
            return

        def _quit(_e=None) -> None:
            pop_dialog()
            page.run_task(page.window.destroy)

        show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text(i18n.t("close.confirm_title")),
                content=ft.Text(i18n.t("close.confirm_body")),
                actions=[
                    ft.TextButton(i18n.t("close.background"),
                                  on_click=lambda e: pop_dialog()),
                    ft.FilledButton(i18n.t("close.quit"), on_click=_quit),
                ],
            )
        )

    # ---- menu bar ---------------------------------------------------------

    def _show_about() -> None:
        show_dialog(
            ft.AlertDialog(
                title=ft.Text(i18n.t("app.title")),
                content=ft.Text(i18n.t("about.body", version=__version__)),
                actions=[
                    ft.TextButton(
                        i18n.t("about.github"),
                        on_click=lambda e: page.launch_url(
                            "https://github.com/cKurultayKalkan/email-export-import"
                        ),
                    ),
                    ft.TextButton(
                        i18n.t("update.close"), on_click=lambda e: pop_dialog()
                    ),
                ],
            )
        )

    def _menubar(migration_extras=None) -> ft.Control:
        return views.build_menubar(
            i18n,
            on_new=start_wizard, on_bulk=show_bulk, on_quit=request_quit,
            on_dashboard=back_to_dashboard, on_settings=show_settings,
            on_check_update=lambda: _check_updates(manual=True),
            on_about=_show_about,
            migration_extras=migration_extras,
        )

    def _decorate(view: ft.View, extras: list | None = None,
                  scrollable: bool = True) -> ft.View:
        """Desktop chrome: menu bar + toolbar pinned on top, status bar at
        the bottom, the view's own content scrolling in between. `extras`
        are (icon, label, handler) page actions, shown in both the Migration
        menu and the toolbar's middle group. Pass scrollable=False for views
        that manage their own internal scrolling with expand children (a
        scrollable wrapper would give them unbounded height)."""
        body = ft.Container(
            content=ft.Column(
                list(view.controls),
                spacing=view.spacing if view.spacing is not None else 10,
                scroll=view.scroll or (ft.ScrollMode.AUTO if scrollable else None),
                expand=True,
            ),
            padding=view.padding if view.padding is not None else 10,
            expand=True,
        )
        toolbar_items: list = [
            (ft.Icons.ADD, i18n.t("menu.new"), start_wizard),
            (ft.Icons.LIBRARY_ADD, i18n.t("menu.bulk"), show_bulk),
        ]
        if extras:
            toolbar_items.append(None)
            toolbar_items += extras
        toolbar_items += [None, (ft.Icons.SETTINGS, i18n.t("nav.settings"), show_settings)]
        active = backend.active_count()
        status = (
            i18n.t("status.ready") if active == 0
            else i18n.t("tray.status", count=active)
        )
        view.controls = [
            _menubar(extras),
            views.build_toolbar(i18n, toolbar_items),
            body,
            views.build_statusbar(i18n, status, __version__),
        ]
        view.padding = 0
        view.spacing = 0
        view.scroll = None
        return view

    show_dashboard()
    page.run_task(poll)
    _check_updates(manual=False)


if __name__ == "__main__":
    main()
