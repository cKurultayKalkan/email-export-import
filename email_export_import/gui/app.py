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
    spool: bool = False
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

    def close_window() -> None:
        # Window.close() is async in the installed Flet version; on_click
        # handlers here are plain sync callables, so schedule it as a task
        # instead of calling (and discarding) the coroutine directly.
        page.run_task(page.window.close)

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
        ws.spool = cfg.get("spool", False)
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
                    if ws.resume_session is None:
                        # New migration: seed the skip list from the source
                        # preset (e.g. Gmail's duplicate label views) so it
                        # doesn't have to be discovered the hard way — a
                        # doubled mailbox. Resume keeps its saved skip list.
                        ws.skip = controller.default_skip(handles["preset_key"]())
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
                page.pop_dialog()
                account.verify_ssl = False
                on_test(account)

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
            go_welcome() if role == "source" else go_account("source")

        view, handles = views.build_account(i18n, role, initial, on_test, on_back, status)
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
                i18n, ws.plan, ws.skip, ws.workers, ws.spool,
                on_toggle, on_workers, on_spool, start_migration,
                lambda: go_account("dest"),
            )
            page.update()

        def on_workers(n: int) -> None:
            ws.workers = n

        def on_spool(enabled: bool) -> None:
            ws.spool = enabled

        page.views.clear()
        page.views.append(
            views.build_plan(
                i18n, ws.plan, ws.skip, ws.workers, ws.spool,
                on_toggle, on_workers, on_spool, start_migration,
                lambda: go_account("dest"),
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
                         workers=ws.workers, total=total, skip=ws.skip,
                         spool=ws.spool)
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
                except RuntimeError:
                    return  # page closed / controls unmounted
                if not snap.running:
                    page.views.clear()
                    page.views.append(views.build_done(i18n, snap, on_close=close_window))
                    page.update()
                    return
                time.sleep(0.1)

        page.run_thread(poll)

    go_welcome()


if __name__ == "__main__":
    main()
