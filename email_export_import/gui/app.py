from __future__ import annotations

import time
from dataclasses import dataclass, field

import flet as ft

from ..models import Account
from ..state import MigrationState
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


def main() -> None:
    ft.app(target=_page_main)


def _page_main(page: ft.Page) -> None:
    i18n = I18n()
    controller = Controller()
    manager = RunManager()
    manager.load_resumable()
    ws = WizardState()
    highlight: list[str | None] = [None]
    page.title = i18n.t("app.title")
    page.window.width = 820
    page.window.height = 680

    def close_window() -> None:
        page.run_task(page.window.close)

    def safe_update(control) -> None:
        try:
            control.update()
        except RuntimeError:
            pass  # page closed / control unmounted

    # ---- dashboard ------------------------------------------------------

    def set_locale(locale: str) -> None:
        i18n.set_locale(locale)
        show_dashboard()

    def show_dashboard() -> None:
        page.views.clear()
        page.views.append(_dashboard_view())
        page.update()

    def _dashboard_view() -> ft.View:
        hk = highlight[0]
        highlight[0] = None
        return views.build_dashboard(
            i18n, manager.snapshot_all(),
            on_new=start_wizard, on_pause=do_pause, on_resume=ask_resume,
            on_cancel=do_cancel, on_detail=show_detail, on_dismiss=do_dismiss,
            on_locale=set_locale, highlight_key=hk,
        )

    def do_pause(key: str) -> None:
        run = manager.get(key)
        if run is not None:
            run.pause()
        refresh_current()

    def do_cancel(key: str) -> None:
        run = manager.get(key)
        if run is not None:
            run.cancel()
        refresh_current()

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
            page.pop_dialog()
            run_async(
                lambda: _reconnect_and_build(cfg, src_pw, dst_pw),
                on_done=lambda built: _start_resumed(key, run, cfg, built),
                on_error=lambda exc: _show_error(str(exc)),
            )

        page.show_dialog(
            views.build_password_dialog(i18n, run.title, submit, lambda: page.pop_dialog())
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
        src_conn, dst_conn, plan = built
        run = Run(
            key=key, title=old_run.title, src_conn=src_conn, dst_conn=dst_conn,
            plans=plan.plans, state=old_run.state, workers=cfg.get("workers", 2),
            total=plan.total, skip=set(cfg.get("skip", [])),
            spool_enabled=cfg.get("spool", False),
        )
        manager.add(run)
        run.start()
        show_dashboard()

    def _account_from_cfg(cfg: dict, password: str, env_var: str) -> Account:
        import os

        return Account(
            host=cfg["host"], port=cfg["port"], ssl=cfg["ssl"], email=cfg["email"],
            password=password or os.environ.get(env_var, ""),
            verify_ssl=cfg.get("verify_ssl", True),
        )

    def _show_error(message: str) -> None:
        page.show_dialog(
            ft.AlertDialog(title=ft.Text(i18n.t("status.error")), content=ft.Text(message))
        )

    # ---- detail ---------------------------------------------------------

    detail_key: list[str | None] = [None]

    def show_detail(key: str) -> None:
        detail_key[0] = key
        run = manager.get(key)
        if run is None:
            show_dashboard()
            return
        page.views.clear()
        page.views.append(
            views.build_detail(
                i18n, run.snapshot(), on_pause=do_pause, on_resume=ask_resume,
                on_cancel=do_cancel, on_back=back_to_dashboard,
            )
        )
        page.update()

    def back_to_dashboard() -> None:
        detail_key[0] = None
        show_dashboard()

    def refresh_current() -> None:
        if detail_key[0] is not None:
            show_detail(detail_key[0])
        else:
            show_dashboard()

    # ---- background poll ------------------------------------------------

    def poll() -> None:
        while True:
            time.sleep(0.2)
            try:
                if not page.views:
                    continue
                route = page.views[-1].route
                if route == "/":
                    new_view = _dashboard_view()
                    if page.views and page.views[-1].route == "/":
                        page.views[-1] = new_view
                        page.update()
                elif route == "/detail" and detail_key[0] is not None:
                    run = manager.get(detail_key[0])
                    if run is not None:
                        new_view = views.build_detail(
                            i18n, run.snapshot(), on_pause=do_pause,
                            on_resume=ask_resume, on_cancel=do_cancel,
                            on_back=back_to_dashboard,
                        )
                        if page.views and page.views[-1].route == "/detail":
                            page.views[-1] = new_view
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
            run_async(
                lambda: controller.test_connection(account),
                on_done=lambda result: _test_done(account, result),
                on_error=lambda exc: _test_done(account, None, exc),
            )

        def _test_done(account: Account, result, exc: Exception | None = None) -> None:
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
                page.pop_dialog()
                account.verify_ssl = False
                on_test(account)  # async again — no UI freeze

            dialog = ft.AlertDialog(
                modal=True,
                title=ft.Text(i18n.t("cert.title")),
                content=ft.Text(i18n.t("cert.body")),
                actions=[
                    ft.TextButton(i18n.t("cert.cancel"), on_click=lambda e: page.pop_dialog()),
                    ft.FilledButton(i18n.t("cert.continue"), on_click=retry_unverified),
                ],
            )
            page.show_dialog(dialog)

        def on_back() -> None:
            back_to_dashboard() if role == "source" else go_account("source")

        view, view_handles = views.build_account(i18n, role, initial, on_test, on_back, status)
        handles.update(view_handles)
        page.views.clear()
        page.views.append(view)
        page.update()

    def go_plan() -> None:
        ws.workers = manager.default_workers()

        def plan_ready(plan) -> None:
            ws.plan = plan
            _render_plan()

        run_async(
            lambda: controller.build_plan(ws.src_conn, ws.dst_conn, ws.skip),
            on_done=plan_ready,
            on_error=lambda exc: _show_error(str(exc)),
        )

    def _render_plan() -> None:
        def on_toggle(source: str, included: bool) -> None:
            (ws.skip.discard if included else ws.skip.add)(source)
            # counts change → rebuild
            page.views[-1] = _plan_view()
            page.update()

        def on_workers(n: int) -> None:
            ws.workers = n

        def on_spool(enabled: bool) -> None:
            ws.spool = enabled

        def _plan_view() -> ft.View:
            return views.build_plan(
                i18n, ws.plan, ws.skip, ws.workers, ws.spool,
                on_toggle, on_workers, on_spool, start_migration,
                lambda: go_account("dest"),
            )

        page.views.clear()
        page.views.append(_plan_view())
        page.update()

    def start_migration() -> None:
        key = f"{ws.src_account.email}__{ws.dst_account.email}"
        existing = manager.get(key)
        if existing is not None and existing.is_active:
            highlight[0] = key
            back_to_dashboard()
            return
        active_plans = [p for p in ws.plan.plans if p.source not in ws.skip]
        total = sum(ws.plan.counts.get(p.source, 0) for p in active_plans)
        state = MigrationState.for_pair(ws.src_account.email, ws.dst_account.email)
        run = Run(
            key=key, title=f"{ws.src_account.email} → {ws.dst_account.email}",
            src_conn=ws.src_conn, dst_conn=ws.dst_conn, plans=active_plans,
            state=state, workers=ws.workers, total=total, skip=ws.skip,
            spool_enabled=ws.spool,
        )
        manager.add(run)
        run.start()
        highlight[0] = key
        back_to_dashboard()

    show_dashboard()
    page.run_thread(poll)


if __name__ == "__main__":
    main()
