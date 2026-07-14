from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import flet as ft

from .. import __version__
from ..models import Account
from ..state import MigrationState
from . import prefs
from . import sysinfo
from . import tray
from . import updater
from . import views
from .async_ops import run_async
from .controller import Controller
from .i18n import I18n
from .run_manager import Run, RunManager


@dataclass
class WizardState:
    src_account: Account | None = None
    dst_account: Account | None = None
    src_conn: object = None
    dst_conn: object = None
    plan: object = None
    skip: set[str] = field(default_factory=set)
    workers: int = 4
    spool: bool = False
    # Set when the plan screen is entered from Resume (reuse the existing state
    # and key instead of minting a new session).
    resume_state: object = None
    resume_key: str | None = None
    resume_title: str | None = None


def main() -> None:
    ft.app(target=_page_main)


def _page_main(page: ft.Page) -> None:
    i18n = I18n()
    controller = Controller()
    manager = RunManager()
    _prefs = prefs.load_prefs(i18n._prefs_path)
    manager.max_active = _prefs.get("max_active", 2)
    manager.workers = _prefs.get("workers", 4)
    manager.rate_limit = _prefs.get("rate_limit", 0)
    manager.load_resumable()
    manager.load_completed()  # keep finished migrations visible as done cards
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
        pop_dialog()
        show_dashboard()  # chrome + rows in the new language
        show_settings()   # reopen the dialog, re-rendered

    def set_max_active(n: int) -> None:
        manager.max_active = n
        prefs.save_pref(i18n._prefs_path, "max_active", n)

    def set_workers(n: int) -> None:
        manager.workers = n
        prefs.save_pref(i18n._prefs_path, "workers", n)

    def set_rate_limit(n: int) -> None:
        manager.rate_limit = n
        prefs.save_pref(i18n._prefs_path, "rate_limit", n)

    def safe_mode() -> None:
        """One click to the gentlest settings: a single connection, one run at a
        time, and a paced upload. For machines whose network stack falls over
        under sustained bulk transfer."""
        set_workers(1)
        set_max_active(1)
        set_rate_limit(2 * 1024 * 1024)
        pop_dialog()
        show_settings()  # reopen so the dropdowns show the new values

    def show_settings() -> None:
        from ..state import DEFAULT_BASE_DIR

        show_dialog(views.build_settings(
            i18n, str(DEFAULT_BASE_DIR), on_locale=set_locale,
            on_back=pop_dialog, version=__version__,
            on_check_update=lambda: _check_updates(manual=True),
            max_active=manager.max_active, on_max_active=set_max_active,
            workers=manager.workers, on_workers=set_workers,
            rate_limit=manager.rate_limit, on_rate_limit=set_rate_limit,
            on_safe_mode=safe_mode, tso_on=sysinfo.tso_enabled(),
        ))

    def show_dashboard() -> None:
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
        snaps = manager.snapshot_all()
        if highlight[0] is not None:
            selected_key[0] = highlight[0]
            highlight[0] = None
        if selected_key[0] is None or not any(
            s.key == selected_key[0] for s in snaps
        ):
            selected_key[0] = snaps[0].key if snaps else None
        sel = next((s for s in snaps if s.key == selected_key[0]), None)
        run = manager.get(sel.key) if sel is not None else None
        cfg = run.state.config if run is not None else None
        refs: dict = {}
        view = views.build_main(
            i18n, snaps, selected_key[0], cfg,
            on_select=select_run,
            on_pause=do_pause, on_resume=ask_resume, on_cancel=do_cancel,
            on_dismiss=do_dismiss,
            on_edit=lambda: _edit_dialog(selected_key[0]),
            refs=refs,
            folder_counts=run.state.folder_done_counts() if run is not None else None,
            last_run=run.state.last_run if run is not None else None,
        )
        render["dash_refs"] = refs
        render["dash_sig"] = (views.dashboard_signature(snaps), selected_key[0])
        return _decorate(view, _main_extras(sel), scrollable=False)

    def do_pause(key: str) -> None:
        run = manager.get(key)
        if run is not None:
            run.pause()
        refresh_current()

    def do_cancel(key: str) -> None:
        # Cancelling is a click away on every card, so guard it behind an
        # "are you sure?" confirmation — a misclick must not discard a run.
        run = manager.get(key)
        if run is None:
            return

        def confirm(_e=None) -> None:
            pop_dialog()
            run.cancel()
            refresh_current()

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
        manager.remove(key)
        show_dashboard()

    # ---- resume ---------------------------------------------------------

    def ask_resume(key: str) -> None:
        run = manager.get(key)
        if run is None:
            return
        cfg = run.state.config or {}

        def submit(src_pw: str, dst_pw: str) -> None:
            pop_dialog()
            # Reconnecting + reading folders can take a while on a slow or
            # rate-limiting server — show the spinner so it never looks frozen.
            show_loading(True, i18n.t("account.testing"))
            run_async(
                lambda: _reconnect_and_build(cfg, src_pw, dst_pw),
                on_done=ui(lambda built: _start_resumed(key, run, cfg, built)),
                on_error=ui(lambda exc: _show_error(str(exc))),
            )

        show_dialog(
            views.build_password_dialog(i18n, run.title, submit, lambda: pop_dialog())
        )

    def _reconnect_and_build(cfg: dict, src_pw: str, dst_pw: str):
        src = _account_from_cfg(cfg["src"], src_pw, "EEI_SRC_PASSWORD")
        dst = _account_from_cfg(cfg["dst"], dst_pw, "EEI_DST_PASSWORD")
        src_result = controller.test_connection(src)
        if not src_result.ok:
            raise RuntimeError(src_result.message or "source connection failed")
        try:
            dst_result = controller.test_connection(dst)
            if not dst_result.ok:
                raise RuntimeError(dst_result.message or "destination connection failed")
            try:
                plan = controller.build_plan(
                    src_result.conn, dst_result.conn, set(cfg.get("skip", []))
                )
            except Exception:
                dst_result.conn.close()
                raise
        except Exception:
            src_result.conn.close()
            raise
        return src_result.conn, dst_result.conn, plan

    def _start_resumed(key: str, old_run: Run, cfg: dict, built) -> None:
        # Resume routes through the plan screen so the user can choose which
        # folders to transfer (and adjust workers/spool) right before starting.
        nonlocal ws
        show_loading(False)
        src_conn, dst_conn, plan = built
        ws = WizardState()
        ws.src_account = src_conn.account
        ws.dst_account = dst_conn.account
        ws.src_conn = src_conn
        ws.dst_conn = dst_conn
        ws.plan = plan
        ws.skip = set(cfg.get("skip", []))
        # Deliberately NOT cfg["workers"]: that records what the run used last
        # time. The current setting is the user's live "how hard to push" knob —
        # lowering it must actually take effect on resume (they can still
        # override per-run on the plan screen).
        ws.workers = manager.default_workers()
        ws.spool = cfg.get("spool", False)
        ws.resume_state = old_run.state
        ws.resume_key = key
        ws.resume_title = old_run.title
        _render_plan()

    def _account_from_cfg(cfg: dict, password: str, env_var: str) -> Account:
        import os

        return Account(
            host=cfg["host"], port=cfg["port"], ssl=cfg["ssl"], email=cfg["email"],
            password=password or os.environ.get(env_var, ""),
            verify_ssl=cfg.get("verify_ssl", True),
        )

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
        run = manager.get(key)
        if run is None or not run.state.config:
            return
        cfg = dict(run.state.config)
        src_controls, src_collect = views._account_editor(
            i18n, i18n.t("account.source_title"), cfg.get("src", {}))
        dst_controls, dst_collect = views._account_editor(
            i18n, i18n.t("account.dest_title"), cfg.get("dst", {}))

        def save(_e=None) -> None:
            cfg["src"] = src_collect()
            cfg["dst"] = dst_collect()
            run.state.set_config(cfg)
            run.state.flush()
            pop_dialog()
            show_dashboard()

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
                    snaps = manager.snapshot_all()
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
        if role == "dest" and ws.src_account and not initial.get("email"):
            initial["email"] = ws.src_account.email

        handles: dict = {}

        def on_test(account: Account) -> None:
            status.value = i18n.t("account.testing")
            safe_update(status)
            handles["set_busy"](True)
            show_loading(True, i18n.t("account.testing"))
            run_async(
                lambda: controller.test_connection(account),
                on_done=ui(lambda result: _test_done(account, result)),
                on_error=ui(lambda exc: _test_done(account, None, exc)),
            )

        def _test_done(account: Account, result, exc: Exception | None = None) -> None:
            show_loading(False)
            handles["set_busy"](False)
            if exc is not None:
                status.value = str(exc)
                safe_update(status)
                return
            if result.ok:
                status.value = i18n.t("account.connected")
                safe_update(status)
                if role == "source":
                    ws.src_account, ws.src_conn = account, result.conn
                    ws.skip = controller.default_skip(handles["preset_key"]())
                    go_account("dest")
                else:
                    ws.dst_account, ws.dst_conn = account, result.conn
                    go_plan()
            elif result.kind == "cert":
                _cert_dialog(account)
            else:
                status.value = i18n.t(f"error.{result.kind}")
                safe_update(status)

        def _cert_dialog(account: Account) -> None:
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
        ws.workers = manager.default_workers()

        def plan_ready(plan) -> None:
            show_loading(False)
            ws.plan = plan
            _render_plan()

        show_loading(True, i18n.t("loading.plan"))
        run_async(
            lambda: controller.build_plan(ws.src_conn, ws.dst_conn, ws.skip),
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

        def _plan_dialog() -> ft.AlertDialog:
            back = pop_dialog if ws.resume_key else (lambda: go_account("dest"))
            return views.build_plan(
                i18n, ws.plan, ws.skip, ws.workers, ws.spool,
                on_toggle, on_workers, on_spool, start_migration, back,
            )

        pop_dialog()  # replace the account step / previous plan render
        show_dialog(_plan_dialog())

    def start_migration() -> None:
        pop_dialog()  # the plan dialog
        key = ws.resume_key or f"{ws.src_account.email}__{ws.dst_account.email}"
        existing = manager.get(key)
        if existing is not None and existing.is_active:
            highlight[0] = key
            back_to_dashboard()
            return
        active_plans = [p for p in ws.plan.plans if p.source not in ws.skip]
        total = sum(ws.plan.counts.get(p.source, 0) for p in active_plans)
        state = ws.resume_state or MigrationState.for_pair(
            ws.src_account.email, ws.dst_account.email
        )
        title = ws.resume_title or f"{ws.src_account.email} → {ws.dst_account.email}"
        run = Run(
            key=key, title=title,
            src_conn=ws.src_conn, dst_conn=ws.dst_conn, plans=active_plans,
            state=state, workers=ws.workers, total=total, skip=ws.skip,
            spool_enabled=ws.spool, rate_limit=manager.rate_limit,
        )
        manager.add(run)
        run.start()
        highlight[0] = key
        back_to_dashboard()

    # ---- bulk ----------------------------------------------------------

    def show_bulk() -> None:
        dlg, _handles = views.build_bulk(i18n, on_start=start_bulk, on_back=pop_dialog)
        show_dialog(dlg)

    def start_bulk(pairs: list, preset_key: str | None) -> None:
        pop_dialog()  # the bulk dialog
        # Add a queued placeholder card per account immediately, then let the
        # poll pump them onto real runs as slots free (cap = manager.max_active).
        for src, dst in pairs:
            key = f"{src.email}__{dst.email}"
            existing = manager.get(key)
            if existing is not None and existing.is_active:
                highlight[0] = key
                continue  # already running for this pair — skip the duplicate
            state = MigrationState.for_pair(src.email, dst.email, base_dir=manager.state_dir)
            placeholder = Run(
                key=key, title=f"{src.email} → {dst.email}",
                src_conn=None, dst_conn=None, plans=None, state=state,
                workers=manager.default_workers(), total=0,
            )
            manager.add(placeholder)
            bulk_pending.append((src, dst, preset_key))
        back_to_dashboard()

    def pump_bulk() -> None:
        # Runs on the event loop (called from poll). Start connects until the
        # cap (active runs + in-flight connects) is reached.
        while (
            manager.active_count() + len(bulk_starting) < manager.max_active
            and bulk_pending
        ):
            src, dst, preset_key = bulk_pending.pop(0)
            key = f"{src.email}__{dst.email}"
            bulk_starting.add(key)
            run_async(
                lambda s=src, d=dst, pk=preset_key: _bulk_connect(s, d, pk),
                on_done=ui(lambda built, k=key: _bulk_started(k, built)),
                on_error=ui(lambda exc, k=key: _bulk_failed(k, str(exc))),
            )

    def _bulk_connect(src, dst, preset_key):
        src_result = controller.test_connection(src)
        if not src_result.ok:
            raise RuntimeError(src_result.message or "source connection failed")
        try:
            dst_result = controller.test_connection(dst)
            if not dst_result.ok:
                raise RuntimeError(dst_result.message or "destination connection failed")
            try:
                skip = controller.default_skip(preset_key)
                plan = controller.build_plan(src_result.conn, dst_result.conn, skip)
            except Exception:
                dst_result.conn.close()
                raise
        except Exception:
            src_result.conn.close()
            raise
        return src_result.conn, dst_result.conn, plan, skip

    def _bulk_started(key: str, built) -> None:
        bulk_starting.discard(key)
        src_conn, dst_conn, plan, skip = built
        active_plans = [p for p in plan.plans if p.source not in skip]
        total = sum(plan.counts.get(p.source, 0) for p in active_plans)
        run = Run(
            key=key, title=f"{src_conn.account.email} → {dst_conn.account.email}",
            src_conn=src_conn, dst_conn=dst_conn, plans=active_plans,
            state=MigrationState.for_pair(
                src_conn.account.email, dst_conn.account.email, base_dir=manager.state_dir
            ),
            workers=manager.default_workers(), total=total, skip=set(skip),
            rate_limit=manager.rate_limit,
        )
        manager.add(run)  # replaces the queued placeholder
        run.start()
        highlight[0] = key
        refresh_current()

    def _bulk_failed(key: str, message: str) -> None:
        bulk_starting.discard(key)
        run = manager.get(key)
        if run is not None:
            run.mark_failed(message)
        refresh_current()

    # ---- close-to-background --------------------------------------------
    # Closing the window while migrations are running (or queued) must not
    # silently kill them. Intercept the close: offer to keep working
    # minimized — the process stays alive in the Dock / task bar and clicking
    # its icon restores the window. With nothing active, close just quits.
    # (Hiding the window entirely is not an option: in Flet desktop the
    # session dies as soon as window.visible goes False — minimize is the
    # one background mode that keeps the engine running.)

    def _work_pending() -> bool:
        return any(
            s.status in ("running", "stopping", "queued")
            for s in manager.snapshot_all()
        )

    def _quit_app() -> None:
        page.run_task(page.window.destroy)

    def _close_to_background(_e=None) -> None:
        pop_dialog()
        page.window.minimized = True
        page.update()

    def _close_and_quit(_e=None) -> None:
        pop_dialog()
        _quit_app()

    def request_quit() -> None:
        # Shared by the window close button and the menu's Quit: with work
        # active, ask (background vs quit); idle, just quit.
        if not _work_pending():
            _quit_app()
            return
        show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text(i18n.t("close.confirm_title")),
                content=ft.Text(i18n.t("close.confirm_body")),
                actions=[
                    ft.TextButton(i18n.t("close.quit"), on_click=_close_and_quit),
                    ft.FilledButton(
                        i18n.t("close.background"), on_click=_close_to_background
                    ),
                ],
            )
        )

    def _on_window_event(e) -> None:
        if getattr(e, "type", None) != ft.WindowEventType.CLOSE:
            return
        if tray_handle is not None:
            # A menu-bar icon is present (packaged macOS app): the close
            # button sends the app to the background for real — windows
            # hidden AND the Dock icon dropped; only the status item stays.
            # Quitting is an explicit menu action, guarded by request_quit.
            if not tray.hide_app():
                page.window.minimized = True  # fallback: at least minimize
                page.update()
        else:
            request_quit()

    page.window.prevent_close = True
    page.window.on_event = _on_window_event

    # ---- macOS status item (packaged app only; inert elsewhere) ----------

    def _restore_window() -> None:
        tray.show_app()  # Dock icon back + unhide (no-op when not hidden)
        page.window.minimized = False
        page.update()

    def _tray_quit() -> None:
        _restore_window()  # the busy/quit dialog must be visible
        request_quit()

    tray_handle = tray.start_status_item(
        i18n.t("app.title"),
        [
            (lambda item: i18n.t("tray.status", count=manager.active_count()), None),
            (i18n.t("tray.show"), ui(_restore_window)),
            (i18n.t("menu.quit"), ui(_tray_quit)),
        ],
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
        active = manager.active_count()
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
