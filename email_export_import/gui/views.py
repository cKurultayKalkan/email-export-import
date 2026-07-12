from __future__ import annotations

from typing import Callable

import flet as ft

from ..models import Account
from ..providers import list_presets
from . import sysinfo
from .controller import PlanResult
from .i18n import I18n
from .run_manager import RunSnapshot


def _title_bar(i18n: I18n, on_settings: Callable[[], None]) -> ft.Row:
    return ft.Row(
        [
            ft.Text(i18n.t("app.title"), size=22, weight=ft.FontWeight.BOLD, expand=True),
            ft.TextButton(
                i18n.t("nav.settings"),
                icon=ft.Icons.SETTINGS,
                on_click=lambda e: on_settings(),
            ),
        ]
    )


def build_settings(
    i18n: I18n,
    data_dir: str,
    on_locale: Callable[[str], None],
    on_back: Callable[[], None],
    version: str = "",
    on_check_update: Callable[[], None] | None = None,
    max_active: int = 2,
    on_max_active: Callable[[int], None] | None = None,
    workers: int = 4,
    on_workers: Callable[[int], None] | None = None,
    rate_limit: int = 0,
    on_rate_limit: Callable[[int], None] | None = None,
    on_safe_mode: Callable[[], None] | None = None,
    tso_on: bool | None = None,
) -> ft.View:
    language = ft.Dropdown(
        label=i18n.t("settings.language"),
        value=i18n.locale,
        width=280,
        options=[
            ft.dropdown.Option("tr", "Türkçe"),
            ft.dropdown.Option("en", "English"),
        ],
        on_select=lambda e: on_locale(e.control.value),
    )
    concurrency = ft.Dropdown(
        label=i18n.t("settings.max_active"),
        value=str(max_active),
        width=280,
        options=[ft.dropdown.Option(str(n)) for n in (1, 2, 3, 4)],
        on_select=lambda e: on_max_active(int(e.control.value)) if on_max_active else None,
    )
    workers_dd = ft.Dropdown(
        label=i18n.t("settings.workers"),
        value=str(workers),
        width=280,
        options=[ft.dropdown.Option(str(n)) for n in (1, 2, 4, 8)],
        on_select=lambda e: on_workers(int(e.control.value)) if on_workers else None,
    )
    rate_dd = ft.Dropdown(
        label=i18n.t("settings.rate_limit"),
        value=str(rate_limit),
        width=280,
        options=[ft.dropdown.Option("0", i18n.t("settings.rate_unlimited"))]
        + [
            ft.dropdown.Option(str(mb * 1024 * 1024), f"{mb} MB/s")
            for mb in (10, 5, 2, 1)
        ],
        on_select=lambda e: on_rate_limit(int(e.control.value)) if on_rate_limit else None,
    )
    controls: list[ft.Control] = [
        ft.Text(i18n.t("settings.title"), size=20, weight=ft.FontWeight.BOLD),
        language,
        ft.Divider(),
        ft.Text(i18n.t("settings.load_title"), weight=ft.FontWeight.BOLD, size=13),
        concurrency,
        ft.Text(i18n.t("settings.max_active_note"), size=12),
        workers_dd,
        ft.Text(i18n.t("settings.workers_note"), size=12),
        rate_dd,
        ft.Text(i18n.t("settings.rate_limit_note"), size=12),
        ft.Row(
            [
                ft.OutlinedButton(
                    i18n.t("settings.safe_mode"),
                    icon=ft.Icons.SHIELD_OUTLINED,
                    on_click=lambda e: on_safe_mode() if on_safe_mode else None,
                ),
                ft.Text(i18n.t("settings.safe_mode_note"), size=12, expand=True),
            ]
        ),
    ]
    if tso_on:
        # Only shown when we positively read TSO=1 on macOS. Sustained bulk
        # upload with TSO on has been seen panicking the kernel's send path.
        controls.append(
            ft.Container(
                bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.AMBER),
                border_radius=6,
                padding=10,
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Icon(ft.Icons.WARNING_AMBER, color=ft.Colors.AMBER),
                                ft.Text(i18n.t("settings.tso_title"),
                                        weight=ft.FontWeight.BOLD, size=13, expand=True),
                            ],
                            spacing=8,
                        ),
                        ft.Text(i18n.t("settings.tso_body"), size=12),
                        ft.Text(sysinfo.TSO_DISABLE_COMMAND, size=12,
                                selectable=True, font_family="monospace"),
                    ],
                    spacing=6,
                ),
            )
        )
    controls += [
        ft.Divider(),
        ft.Text(f"{i18n.t('settings.version')}: {version}", size=13),
        ft.TextButton(
            i18n.t("settings.check_updates"),
            icon=ft.Icons.SYSTEM_UPDATE,
            on_click=lambda e: on_check_update() if on_check_update else None,
        ),
        ft.Divider(),
        ft.Text(i18n.t("settings.data_location"), weight=ft.FontWeight.BOLD, size=13),
        ft.Text(data_dir, size=12, selectable=True),
        ft.Text(i18n.t("settings.data_note"), size=12),
        ft.Row(
            [ft.TextButton(i18n.t("detail.back"), on_click=lambda e: on_back())],
            alignment=ft.MainAxisAlignment.END,
        ),
    ]
    return ft.View(route="/settings", controls=controls, padding=24, spacing=14)


_CUSTOM_PRESET_KEY = "__custom__"


def _fill_from_preset(presets, key, host, port, use_ssl, hint) -> None:
    """Copy a provider preset's server settings into the given controls (values
    only — the caller decides when/whether to push .update())."""
    for p in presets:
        if p.key == key:
            host.value, port.value, use_ssl.value = p.host, str(p.port), p.ssl
            hint.value = p.app_password_hint or ""
            return
    hint.value = ""


def _preset_dropdown(i18n: I18n, presets, value: str) -> ft.Dropdown:
    return ft.Dropdown(
        label=i18n.t("account.provider"),
        value=value,
        options=[ft.dropdown.Option(p.key, p.name) for p in presets]
        + [ft.dropdown.Option(_CUSTOM_PRESET_KEY, i18n.t("account.custom"))],
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
    custom_key = _CUSTOM_PRESET_KEY
    preset_dd = _preset_dropdown(i18n, presets, initial.get("preset", custom_key))
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
        _fill_from_preset(presets, preset_dd.value, host, port, use_ssl, hint)
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


def _parse_port(raw: str | None) -> int | None:
    try:
        value = int((raw or "993").strip())
    except (ValueError, AttributeError):
        return None
    return value if 1 <= value <= 65535 else None


def build_bulk(
    i18n: I18n,
    on_start: Callable[[list[tuple[Account, Account]], str | None], None],
    on_back: Callable[[], None],
) -> tuple[ft.View, dict]:
    """Bulk entry: one source provider + one destination server, then a row per
    account (email + both passwords). collect() validates and returns
    (source, destination) Account pairs; Start-all hands the valid pairs on."""
    presets = list_presets()
    custom_key = _CUSTOM_PRESET_KEY

    preset_dd = _preset_dropdown(i18n, presets, custom_key)
    src_host = ft.TextField(label=i18n.t("account.host"), expand=True)
    src_port = ft.TextField(label=i18n.t("account.port"), value="993", width=110)
    src_ssl = ft.Checkbox(label=i18n.t("account.ssl"), value=True)
    hint = ft.Text("", size=12, color=ft.Colors.AMBER)

    def preset_changed(e=None):
        _fill_from_preset(presets, preset_dd.value, src_host, src_port, src_ssl, hint)
        try:
            src_host.update(); src_port.update(); src_ssl.update(); hint.update()
        except RuntimeError:
            pass  # not mounted yet

    preset_dd.on_select = preset_changed

    dst_host = ft.TextField(label=i18n.t("account.host"), expand=True)
    dst_port = ft.TextField(label=i18n.t("account.port"), value="993", width=110)
    dst_ssl = ft.Checkbox(label=i18n.t("account.ssl"), value=True)
    dst_verify = ft.Checkbox(label=i18n.t("account.verify_ssl"), value=True)
    dst = {"host": dst_host, "port": dst_port, "ssl": dst_ssl, "verify": dst_verify}

    status = ft.Text("", size=12, color=ft.Colors.RED)
    rows: list[dict] = []
    rows_column = ft.Column(spacing=8)

    def _remove(row: dict) -> None:
        if len(rows) <= 1:
            return  # always keep one row
        rows.remove(row)
        rows_column.controls.remove(row["control"])
        try:
            rows_column.update()
        except RuntimeError:
            pass

    def add_row(e=None) -> dict:
        email = ft.TextField(label=i18n.t("bulk.email"), expand=True)
        src_pw = ft.TextField(
            label=i18n.t("bulk.src_password"), password=True, can_reveal_password=True, width=180
        )
        dst_pw = ft.TextField(
            label=i18n.t("bulk.dst_password"), password=True, can_reveal_password=True, width=180
        )
        row = {"email": email, "src_pw": src_pw, "dst_pw": dst_pw}
        row["control"] = ft.Row(
            [email, src_pw, dst_pw,
             ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, tooltip=i18n.t("bulk.remove_row"),
                           on_click=lambda _e, r=row: _remove(r))],
            alignment=ft.MainAxisAlignment.START,
        )
        rows.append(row)
        rows_column.controls.append(row["control"])
        try:
            rows_column.update()
        except RuntimeError:
            pass
        return row

    add_row()  # start with one empty row

    def preset_key() -> str | None:
        return preset_dd.value if preset_dd.value != custom_key else None

    def collect() -> list[tuple[Account, Account]]:
        sp = _parse_port(src_port.value)
        dp = _parse_port(dst_port.value)
        if not (src_host.value or "").strip() or sp is None:
            raise ValueError(i18n.t("bulk.dest_invalid"))
        if not (dst_host.value or "").strip() or dp is None:
            raise ValueError(i18n.t("bulk.dest_invalid"))
        pairs: list[tuple[Account, Account]] = []
        for idx, row in enumerate(rows, start=1):
            email = (row["email"].value or "").strip()
            src_pw = row["src_pw"].value or ""
            dst_pw = row["dst_pw"].value or ""
            if not email and not src_pw and not dst_pw:
                continue  # blank row — skip
            if not (email and src_pw and dst_pw):
                raise ValueError(i18n.t("bulk.row_invalid", n=idx))
            src = Account(host=src_host.value.strip(), port=sp, ssl=bool(src_ssl.value),
                          email=email, password=src_pw, verify_ssl=True)
            dst_acc = Account(host=dst_host.value.strip(), port=dp, ssl=bool(dst_ssl.value),
                              email=email, password=dst_pw, verify_ssl=bool(dst_verify.value))
            pairs.append((src, dst_acc))
        if not pairs:
            raise ValueError(i18n.t("bulk.no_rows"))
        return pairs

    def start_clicked(e=None) -> None:
        try:
            pairs = collect()
        except ValueError as exc:
            status.value = str(exc)
            try:
                status.update()
            except RuntimeError:
                pass
            return
        on_start(pairs, preset_key())

    controls = [
        ft.Text(i18n.t("bulk.title"), size=18, weight=ft.FontWeight.BOLD),
        ft.Text(i18n.t("bulk.source_title"), weight=ft.FontWeight.BOLD, size=13),
        preset_dd,
        ft.Row([src_host, src_port, src_ssl]),
        hint,
        ft.Divider(),
        ft.Text(i18n.t("bulk.dest_title"), weight=ft.FontWeight.BOLD, size=13),
        ft.Row([dst_host, dst_port, dst_ssl, dst_verify]),
        ft.Divider(),
        ft.Text(i18n.t("bulk.accounts_title"), weight=ft.FontWeight.BOLD, size=13),
        rows_column,
        ft.TextButton(i18n.t("bulk.add_row"), icon=ft.Icons.ADD, on_click=add_row),
        status,
        ft.Row(
            [
                ft.TextButton(i18n.t("account.back"), on_click=lambda e: on_back()),
                ft.FilledButton(i18n.t("bulk.start_all"), on_click=start_clicked),
            ],
            alignment=ft.MainAxisAlignment.END,
        ),
    ]
    view = ft.View(route="/bulk", controls=controls, padding=24, spacing=12,
                   scroll=ft.ScrollMode.AUTO)
    return view, {
        "collect": collect,
        "preset_key": preset_key,
        "rows": lambda: rows,
        "_add_row": add_row,
        "_src": {"host": src_host, "port": src_port, "ssl": src_ssl},
        "_dst": dst,
        "status": status,
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
            ft.Text(i18n.t("plan.preserve_info"), size=12, italic=True),
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
    "stopping": ft.Colors.AMBER,
    "queued": ft.Colors.GREY,
    "paused": ft.Colors.AMBER,
    "done": ft.Colors.GREEN,
    "error": ft.Colors.RED,
    "cancelled": ft.Colors.GREY,
}


def _progress_controls(i18n: I18n, snap: RunSnapshot):
    """Build the progress control and return (container, bar_or_None, counter).

    The bar/counter references let the dashboard poll update values in place
    instead of rebuilding the view — rebuilding recreates every button, which
    breaks hover/click on the cards. Callers that don't poll ignore the refs.
    """
    if snap.total > 0:
        bar = ft.ProgressBar(value=snap.processed / snap.total)
        counter = ft.Text(f"{snap.processed} / {snap.total}", size=12)
        return ft.Column([bar, counter], spacing=4), bar, counter
    counter = ft.Text(i18n.t("dash.migrated_only", count=snap.processed), size=12)
    return counter, None, counter


def dashboard_signature(snapshots: list[RunSnapshot]) -> tuple:
    """Structural fingerprint: which cards exist and each card's status. When
    this is unchanged between polls, only progress values changed, so the poll
    updates them in place and never rebuilds the (button-bearing) cards."""
    return tuple((s.key, s.status) for s in snapshots)


def detail_signature(snap: RunSnapshot) -> tuple:
    return (snap.key, snap.status)


def apply_dashboard_values(refs: dict, snapshots: list[RunSnapshot], i18n: I18n) -> None:
    """Mutate the held bar/counter controls' values in place (no .update())."""
    for s in snapshots:
        entry = refs.get(s.key)
        if entry is None:
            continue
        if entry["bar"] is not None and s.total > 0:
            entry["bar"].value = s.processed / s.total
            entry["counter"].value = f"{s.processed} / {s.total}"
        else:
            entry["counter"].value = i18n.t("dash.migrated_only", count=s.processed)


def apply_detail_values(refs: dict, snap: RunSnapshot, i18n: I18n) -> None:
    entry = refs.get("_")
    if entry is None:
        return
    if entry["bar"] is not None and snap.total > 0:
        entry["bar"].value = snap.processed / snap.total
        entry["counter"].value = f"{snap.processed} / {snap.total}"
    else:
        entry["counter"].value = i18n.t("dash.migrated_only", count=snap.processed)
    entry["folder"].value = snap.current_folder or ""


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
    elif snap.status == "cancelled":
        # A cancelled run keeps its on-disk progress, so it can be resumed
        # (reconnects from scratch) or dismissed for good.
        actions.append(ft.FilledButton(i18n.t("dash.resume"), on_click=lambda e, k=key: on_resume(k)))
        actions.append(ft.TextButton(i18n.t("dash.dismiss"), on_click=lambda e, k=key: on_dismiss(k)))
    elif snap.status in ("done", "error"):
        # A finished migration can be re-run to pick up mail that arrived since:
        # dedup skips everything already moved, so only new messages are copied.
        actions.append(ft.FilledButton(i18n.t("dash.sync"), tooltip=i18n.t("sync.hint"),
                                       on_click=lambda e, k=key: on_resume(k)))
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
    on_settings: Callable[[], None],
    on_new_bulk: Callable[[], None] = lambda: None,
    highlight_key: str | None = None,
    refs: dict | None = None,
) -> ft.View:
    cards: list[ft.Control] = []
    for snap in snapshots:
        badge = ft.Text(
            i18n.t(f"status.{snap.status}"),
            color=_STATUS_COLOR.get(snap.status),
            weight=ft.FontWeight.BOLD,
            size=12,
        )
        progress, bar, counter = _progress_controls(i18n, snap)
        if refs is not None:
            refs[snap.key] = {"bar": bar, "counter": counter}
        body = [
            ft.Row([ft.Text(snap.title, weight=ft.FontWeight.BOLD, expand=True), badge]),
            progress,
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
        _title_bar(i18n, on_settings),
        ft.Text(i18n.t("dash.heading"), size=18, weight=ft.FontWeight.BOLD),
    ]
    controls.extend(cards)
    controls.append(
        ft.Row(
            [
                ft.FilledButton(i18n.t("dash.new"), on_click=lambda e: on_new()),
                ft.OutlinedButton(
                    i18n.t("dash.new_bulk"), icon=ft.Icons.GROUP_ADD,
                    on_click=lambda e: on_new_bulk(),
                ),
            ]
        )
    )
    return ft.View(
        route="/", controls=controls, padding=24, spacing=16, scroll=ft.ScrollMode.AUTO
    )


def _yesno(i18n: I18n, value: bool) -> str:
    return i18n.t("common.yes") if value else i18n.t("common.no")


def _account_summary(i18n: I18n, title: str, cfg: dict) -> ft.Control:
    return ft.Column(
        [
            ft.Text(title, weight=ft.FontWeight.BOLD, size=13),
            ft.Text(cfg.get("email", ""), size=12),
            ft.Text(
                f"{cfg.get('host', '')}:{cfg.get('port', '')}   "
                f"SSL: {_yesno(i18n, cfg.get('ssl', True))}   "
                f"{i18n.t('account.verify_ssl')}: {_yesno(i18n, cfg.get('verify_ssl', True))}",
                size=12,
            ),
        ],
        spacing=2,
    )


def _account_editor(i18n: I18n, title: str, cfg: dict):
    host = ft.TextField(label=i18n.t("account.host"), value=str(cfg.get("host", "")), width=300)
    port = ft.TextField(label=i18n.t("account.port"), value=str(cfg.get("port", 993)), width=110)
    ssl = ft.Checkbox(label=i18n.t("account.ssl"), value=bool(cfg.get("ssl", True)))
    verify = ft.Checkbox(label=i18n.t("account.verify_ssl"), value=bool(cfg.get("verify_ssl", True)))
    controls = [
        ft.Text(title, weight=ft.FontWeight.BOLD, size=13),
        ft.Text(cfg.get("email", ""), size=12),  # email is read-only (session key)
        ft.Row([host, port]),
        ft.Row([ssl, verify]),
    ]

    def collect() -> dict:
        raw = str(port.value or "993").strip()
        return {
            "email": cfg.get("email", ""),
            "host": (host.value or "").strip(),
            "port": int(raw) if raw.isdigit() else 993,
            "ssl": bool(ssl.value),
            "verify_ssl": bool(verify.value),
        }

    return controls, collect


def build_detail(
    i18n: I18n,
    snap: RunSnapshot,
    on_pause: Callable[[str], None],
    on_resume: Callable[[str], None],
    on_cancel: Callable[[str], None],
    on_back: Callable[[], None],
    refs: dict | None = None,
    config: dict | None = None,
    editing: bool = False,
    on_edit: Callable[[], None] | None = None,
    on_save: Callable[[dict, dict], None] | None = None,
) -> ft.View:
    progress, bar, counter = _progress_controls(i18n, snap)
    folder = ft.Text(snap.current_folder or "", size=12)
    if refs is not None:
        refs["_"] = {"bar": bar, "counter": counter, "folder": folder}
    controls: list[ft.Control] = [
        ft.Text(snap.title, size=18, weight=ft.FontWeight.BOLD),
        ft.Text(i18n.t(f"status.{snap.status}"), color=_STATUS_COLOR.get(snap.status)),
        progress,
        folder,
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

    # Connection panel: paused runs can view and edit the source/destination
    # before resuming; other statuses show a read-only summary.
    if config is not None:
        controls.append(ft.Divider())
        if editing and snap.status == "paused":
            src_ctrls, src_collect = _account_editor(
                i18n, i18n.t("account.source_title"), config.get("src", {})
            )
            dst_ctrls, dst_collect = _account_editor(
                i18n, i18n.t("account.dest_title"), config.get("dst", {})
            )
            controls.extend(src_ctrls)
            controls.extend(dst_ctrls)
            controls.append(
                ft.FilledButton(
                    i18n.t("detail.save"),
                    icon=ft.Icons.SAVE,
                    on_click=lambda e: on_save(src_collect(), dst_collect()) if on_save else None,
                )
            )
        else:
            controls.append(_account_summary(i18n, i18n.t("account.source_title"), config.get("src", {})))
            controls.append(_account_summary(i18n, i18n.t("account.dest_title"), config.get("dst", {})))
            if snap.status == "paused" and on_edit is not None:
                controls.append(
                    ft.TextButton(i18n.t("detail.edit"), icon=ft.Icons.EDIT,
                                  on_click=lambda e: on_edit())
                )

    controls.append(ft.Text(i18n.t("done.resume_hint"), size=12))
    detail_actions: list[ft.Control] = []
    if snap.status == "running":
        detail_actions.append(
            ft.TextButton(i18n.t("dash.pause"), on_click=lambda e: on_pause(snap.key))
        )
        detail_actions.append(
            ft.TextButton(i18n.t("dash.cancel"), on_click=lambda e: on_cancel(snap.key))
        )
    elif snap.status == "paused":
        detail_actions.append(
            ft.FilledButton(i18n.t("dash.resume"), on_click=lambda e: on_resume(snap.key))
        )
        detail_actions.append(
            ft.TextButton(i18n.t("dash.cancel"), on_click=lambda e: on_cancel(snap.key))
        )
    elif snap.status == "cancelled":
        detail_actions.append(
            ft.FilledButton(i18n.t("dash.resume"), on_click=lambda e: on_resume(snap.key))
        )
    elif snap.status in ("done", "error"):
        # Re-run a finished migration to pull in mail that arrived since.
        detail_actions.append(
            ft.FilledButton(i18n.t("dash.sync"), on_click=lambda e: on_resume(snap.key))
        )
    detail_actions.append(
        ft.TextButton(i18n.t("detail.back"), on_click=lambda e: on_back())
    )
    controls.append(ft.Row(detail_actions, alignment=ft.MainAxisAlignment.END))
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
            ft.TextButton(i18n.t("resume.cancel"), on_click=lambda e: on_cancel()),
            ft.FilledButton(
                i18n.t("resume.go"),
                on_click=lambda e: on_submit(src_pw.value or "", dst_pw.value or ""),
            ),
        ],
    )
