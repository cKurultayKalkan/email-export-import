from __future__ import annotations

from typing import Callable

import flet as ft

from ..models import Account
from ..providers import list_presets
from .controller import PlanResult
from .i18n import I18n
from .run_manager import RunSnapshot


def _title_bar(i18n: I18n, on_locale: Callable[[str], None]) -> ft.Row:
    return ft.Row(
        [
            ft.Text(i18n.t("app.title"), size=22, weight=ft.FontWeight.BOLD, expand=True),
            ft.Dropdown(
                width=110,
                value=i18n.locale,
                options=[ft.dropdown.Option("tr", "Türkçe"), ft.dropdown.Option("en", "English")],
                on_select=lambda e: on_locale(e.control.value),
                label=i18n.t("language.label"),
            ),
        ]
    )


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
    busy = ft.ProgressRing(width=18, height=18, visible=False)

    def preset_changed(e=None):
        for p in presets:
            if p.key == preset_dd.value:
                host.value, port.value, use_ssl.value = p.host, str(p.port), p.ssl
                hint.value = p.app_password_hint or ""
                break
        else:
            hint.value = ""
        try:
            host.update(); port.update(); use_ssl.update(); hint.update()
        except RuntimeError:
            pass  # not mounted to a page yet (called during initial construction)

    preset_dd.on_select = preset_changed
    if initial.get("preset"):
        preset_changed()

    def parse_port() -> int | None:
        try:
            value = int(port.value or 993)
        except ValueError:
            return None
        return value if 1 <= value <= 65535 else None

    def account(port_value: int) -> Account:
        return Account(
            host=host.value.strip(),
            port=port_value,
            ssl=bool(use_ssl.value),
            email=email.value.strip(),
            password=password.value,
            verify_ssl=bool(initial.get("verify_ssl", True)),
        )

    def _test_clicked(e) -> None:
        p = parse_port()
        if p is None:
            port.error_text = i18n.t("account.port_invalid")
            port.update()
            return
        port.error_text = None
        try:
            port.update()
        except RuntimeError:
            pass  # not mounted to a page yet (e.g. called directly in tests)
        on_test(account(p))

    def set_busy(value: bool) -> None:
        busy.visible = value
        try:
            busy.update()
        except RuntimeError:
            pass

    controls = [
        ft.Text(i18n.t(f"account.{'source' if role == 'source' else 'dest'}_title"),
                size=18, weight=ft.FontWeight.BOLD),
        preset_dd, host, ft.Row([port, use_ssl]), email, password, hint, status_text,
        ft.Row(
            [
                ft.TextButton(i18n.t("account.back"), on_click=lambda e: on_back()),
                busy,
                ft.FilledButton(i18n.t("account.test"), on_click=_test_clicked),
            ],
            alignment=ft.MainAxisAlignment.END,
        ),
    ]
    return ft.View(route=f"/{role}", controls=controls, padding=24, spacing=12), {
        "account": account,
        "preset_key": lambda: preset_dd.value if preset_dd.value != custom_key else None,
        "set_busy": set_busy,
    }


def build_plan(
    i18n: I18n,
    plan: PlanResult,
    skip: set[str],
    workers: int,
    spool: bool,
    on_toggle: Callable[[str, bool], None],
    on_workers: Callable[[int], None],
    on_spool: Callable[[bool], None],
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
        on_select=lambda e: on_workers(int(e.control.value)),
    )
    return ft.View(
        route="/plan",
        controls=[
            ft.Text(i18n.t("plan.title"), size=18, weight=ft.FontWeight.BOLD),
            ft.Column([table], scroll=ft.ScrollMode.AUTO, expand=True),
            ft.Row([workers_dd, ft.Text(i18n.t("plan.total", count=selected_total))]),
            ft.Checkbox(
                label=i18n.t("plan.spool"),
                value=spool,
                on_change=lambda e: on_spool(bool(e.control.value)),
            ),
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
        route="/progress",
        controls=[
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


_STATUS_COLOR = {
    "running": ft.Colors.BLUE,
    "queued": ft.Colors.GREY,
    "paused": ft.Colors.AMBER,
    "done": ft.Colors.GREEN,
    "error": ft.Colors.RED,
    "cancelled": ft.Colors.GREY,
}


def _progress_line(i18n: I18n, snap: RunSnapshot) -> ft.Control:
    if snap.total > 0:
        return ft.Column(
            [
                ft.ProgressBar(value=snap.processed / snap.total),
                ft.Text(f"{snap.processed} / {snap.total}", size=12),
            ],
            spacing=4,
        )
    return ft.Text(i18n.t("dash.migrated_only", count=snap.processed), size=12)


def _card_actions(
    i18n: I18n, snap: RunSnapshot, on_pause, on_resume, on_cancel, on_detail, on_dismiss
) -> list[ft.Control]:
    key = snap.key
    actions: list[ft.Control] = []
    if snap.status == "running":
        actions.append(ft.TextButton(i18n.t("dash.pause"), on_click=lambda e, k=key: on_pause(k)))
        actions.append(ft.TextButton(i18n.t("dash.cancel"), on_click=lambda e, k=key: on_cancel(k)))
    elif snap.status == "paused":
        actions.append(ft.FilledButton(i18n.t("dash.resume"), on_click=lambda e, k=key: on_resume(k)))
        actions.append(ft.TextButton(i18n.t("dash.cancel"), on_click=lambda e, k=key: on_cancel(k)))
    elif snap.status in ("done", "error", "cancelled"):
        actions.append(ft.TextButton(i18n.t("dash.dismiss"), on_click=lambda e, k=key: on_dismiss(k)))
    actions.append(ft.TextButton(i18n.t("dash.detail"), on_click=lambda e, k=key: on_detail(k)))
    return actions


def build_dashboard(
    i18n: I18n,
    snapshots: list[RunSnapshot],
    on_new: Callable[[], None],
    on_pause: Callable[[str], None],
    on_resume: Callable[[str], None],
    on_cancel: Callable[[str], None],
    on_detail: Callable[[str], None],
    on_dismiss: Callable[[str], None],
    on_locale: Callable[[str], None],
    highlight_key: str | None = None,
) -> ft.View:
    cards: list[ft.Control] = []
    for snap in snapshots:
        badge = ft.Text(
            i18n.t(f"status.{snap.status}"),
            color=_STATUS_COLOR.get(snap.status),
            weight=ft.FontWeight.BOLD,
            size=12,
        )
        body = [
            ft.Row([ft.Text(snap.title, weight=ft.FontWeight.BOLD, expand=True), badge]),
            _progress_line(i18n, snap),
        ]
        if snap.error_kind == "quota":
            body.append(ft.Text(i18n.t("done.quota"), color=ft.Colors.RED, size=12))
        elif snap.error_kind == "fatal":
            body.append(ft.Text(snap.error_message or "", color=ft.Colors.RED, size=12))
        body.append(
            ft.Row(
                _card_actions(i18n, snap, on_pause, on_resume, on_cancel, on_detail, on_dismiss),
                alignment=ft.MainAxisAlignment.END,
            )
        )
        cards.append(
            ft.Card(
                content=ft.Container(ft.Column(body, spacing=8), padding=12),
                bgcolor=ft.Colors.PRIMARY_CONTAINER if snap.key == highlight_key else None,
            )
        )
    if not cards:
        cards.append(ft.Text(i18n.t("dash.empty")))

    controls: list[ft.Control] = [
        _title_bar(i18n, on_locale),
        ft.Text(i18n.t("dash.heading"), size=18, weight=ft.FontWeight.BOLD),
    ]
    controls.extend(cards)
    controls.append(ft.FilledButton(i18n.t("dash.new"), on_click=lambda e: on_new()))
    return ft.View(
        route="/", controls=controls, padding=24, spacing=16, scroll=ft.ScrollMode.AUTO
    )


def build_detail(
    i18n: I18n,
    snap: RunSnapshot,
    on_pause: Callable[[str], None],
    on_resume: Callable[[str], None],
    on_cancel: Callable[[str], None],
    on_back: Callable[[], None],
) -> ft.View:
    controls: list[ft.Control] = [
        ft.Text(snap.title, size=18, weight=ft.FontWeight.BOLD),
        ft.Text(i18n.t(f"status.{snap.status}"), color=_STATUS_COLOR.get(snap.status)),
        _progress_line(i18n, snap),
        ft.Text(snap.current_folder or "", size=12),
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
                    height=180,
                )
            )
    if snap.spool_pending:
        controls.append(ft.Text(i18n.t("done.spool_pending", count=snap.spool_pending), size=12))
    controls.append(ft.Text(i18n.t("done.resume_hint"), size=12))
    controls.append(
        ft.Row(
            _card_actions(i18n, snap, on_pause, on_resume, on_cancel, lambda k: None, lambda k: None)[:-1]
            + [ft.TextButton(i18n.t("detail.back"), on_click=lambda e: on_back())],
            alignment=ft.MainAxisAlignment.END,
        )
    )
    return ft.View(route="/detail", controls=controls, padding=24, spacing=12)


def build_password_dialog(
    i18n: I18n,
    title: str,
    on_submit: Callable[[str, str], None],
    on_cancel: Callable[[], None],
) -> ft.AlertDialog:
    src_pw = ft.TextField(
        label=i18n.t("resume.src_password"), password=True, can_reveal_password=True
    )
    dst_pw = ft.TextField(
        label=i18n.t("resume.dst_password"), password=True, can_reveal_password=True
    )
    return ft.AlertDialog(
        modal=True,
        title=ft.Text(i18n.t("resume.title")),
        content=ft.Column([ft.Text(title, size=12), src_pw, dst_pw], tight=True, spacing=8),
        actions=[
            ft.TextButton(i18n.t("cert.cancel"), on_click=lambda e: on_cancel()),
            ft.FilledButton(
                i18n.t("resume.go"),
                on_click=lambda e: on_submit(src_pw.value or "", dst_pw.value or ""),
            ),
        ],
    )
