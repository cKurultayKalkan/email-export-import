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
from .errors import CertificateVerifyFailed, MigrationError, QuotaExceeded
from .folders import build_folder_plan
from .models import Account, ProviderPreset
from .providers import PRESETS, get_preset, list_presets
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
    interactive: bool,
    verify_ssl: bool = True,
) -> tuple[Account, ProviderPreset | None]:
    prefix = "src" if role == "Source" else "dst"
    preset: ProviderPreset | None = None
    if preset_key is not None:
        try:
            preset = get_preset(preset_key)
        except KeyError:
            console.print(
                f"[red]{role}: unknown preset '{preset_key}' "
                f"(choose {'|'.join(PRESETS)})[/red]"
            )
            raise typer.Exit(code=1)
    elif host is None and interactive:
        preset = _choose_preset(role)

    if preset is not None:
        host = host or preset.host
        port = port or preset.port
        ssl = preset.ssl
        if preset.app_password_hint:
            console.print(f"[yellow]{preset.app_password_hint}[/yellow]")

    if host is None:
        if not interactive:
            console.print(
                f"[red]{role}: --{prefix}-host or --{prefix}-preset is required with --yes[/red]"
            )
            raise typer.Exit(code=1)
        host = Prompt.ask(f"{role} IMAP host")
    if port is None:
        port = 993 if not interactive else IntPrompt.ask(f"{role} IMAP port", default=993)
    if email_addr is None:
        if not interactive:
            console.print(f"[red]{role}: --{prefix}-email is required with --yes[/red]")
            raise typer.Exit(code=1)
        email_addr = Prompt.ask(f"{role} email address")
    password = os.environ.get(password_env)
    if not password:
        if not interactive:
            console.print(f"[red]{role}: set {password_env} when running with --yes[/red]")
            raise typer.Exit(code=1)
        password = Prompt.ask(f"{role} password", password=True)
    if not verify_ssl:
        console.print(
            f"[yellow]{role}: SSL certificate verification disabled — the connection "
            "is encrypted but not protected against man-in-the-middle attacks.[/yellow]"
        )
    return (
        Account(
            host=host,
            port=port,
            ssl=ssl,
            email=email_addr,
            password=password,
            verify_ssl=verify_ssl,
        ),
        preset,
    )


def _connect(account: Account, role: str, interactive: bool) -> MailConnection:
    prefix = "src" if role == "Source" else "dst"
    while True:
        conn = MailConnection(account)
        try:
            conn.connect()
            console.print(f"[green]{role}: connected to {account.host} as {account.email}[/green]")
            return conn
        except CertificateVerifyFailed as exc:
            console.print(f"[red]{exc}[/red]")
            if not interactive:
                console.print(
                    f"[yellow]Use --no-{prefix}-verify-ssl to connect anyway "
                    "(disables man-in-the-middle protection).[/yellow]"
                )
                raise typer.Exit(code=1)
            console.print(
                "[yellow]The server presented a certificate that cannot be verified "
                "(often a self-signed certificate). The connection would stay encrypted, "
                "but without verification an attacker on the network could impersonate "
                "the server (man-in-the-middle) and capture your password. Continue only "
                "if you trust this server and network.[/yellow]"
            )
            if Confirm.ask("Continue without certificate verification?", default=False):
                account.verify_ssl = False
                console.print(
                    f"[yellow]{role}: SSL certificate verification disabled.[/yellow]"
                )
                continue
            raise typer.Exit(code=1)
        except MigrationError as exc:
            console.print(f"[red]{exc}[/red]")
            if not interactive or not Confirm.ask("Edit connection details and retry?"):
                raise typer.Exit(code=1)
            account.host = Prompt.ask("IMAP host", default=account.host)
            account.port = IntPrompt.ask("IMAP port", default=account.port)
            account.email = Prompt.ask("Email address", default=account.email)
            account.password = Prompt.ask("Password", password=True)


def _namespace_prefix(conn: MailConnection) -> str:
    """The server's personal-namespace prefix (e.g. 'INBOX.' on Courier),
    or '' when the server has none or doesn't support NAMESPACE."""
    try:
        prefix, _sep = conn.client.namespace().personal[0]
    except Exception:
        return ""
    if isinstance(prefix, bytes):
        prefix = prefix.decode()
    return prefix or ""


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
    src_verify_ssl: bool = typer.Option(
        True,
        "--src-verify-ssl/--no-src-verify-ssl",
        help="Verify the source server's TLS certificate (disable only for trusted self-signed servers)",
    ),
    dst_preset: Optional[str] = typer.Option(None, "--dst-preset", help="gmail|outlook|yahoo|icloud|yandex"),
    dst_host: Optional[str] = typer.Option(None, "--dst-host"),
    dst_port: Optional[int] = typer.Option(None, "--dst-port"),
    dst_ssl: bool = typer.Option(True, "--dst-ssl/--no-dst-ssl"),
    dst_email: Optional[str] = typer.Option(None, "--dst-email"),
    dst_verify_ssl: bool = typer.Option(
        True,
        "--dst-verify-ssl/--no-dst-verify-ssl",
        help="Verify the destination server's TLS certificate (disable only for trusted self-signed servers)",
    ),
    skip: Optional[str] = typer.Option(None, "--skip", help="Comma-separated source folders to skip (overrides preset default)"),
    yes: bool = typer.Option(False, "--yes", help="No prompts; fail instead of asking"),
    state_dir: Optional[Path] = typer.Option(None, "--state-dir", help="Override state directory (default ~/.email-export-import)"),
) -> None:
    """Migrate a mailbox from one IMAP server to another."""
    interactive = not yes

    src_account, src_preset_obj = _gather_account(
        "Source", src_preset, src_host, src_port, src_ssl, src_email, SRC_PASSWORD_ENV,
        interactive, src_verify_ssl,
    )
    src_conn = _connect(src_account, "Source", interactive)

    dst_account, _ = _gather_account(
        "Destination", dst_preset, dst_host, dst_port, dst_ssl, dst_email, DST_PASSWORD_ENV,
        interactive, dst_verify_ssl,
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
        src_conn.client.list_folders(),
        dst_conn.client.list_folders(),
        skip_set,
        dst_prefix=_namespace_prefix(dst_conn),
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
