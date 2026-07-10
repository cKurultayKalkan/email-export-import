from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderPreset:
    """A known provider's IMAP settings plus UX hints."""

    key: str
    name: str
    host: str
    port: int
    ssl: bool
    app_password_hint: str | None = None
    skip_folders: tuple[str, ...] = ()


@dataclass
class Account:
    """Connection settings for one mailbox."""

    host: str
    port: int
    ssl: bool
    email: str
    password: str
    verify_ssl: bool = True


@dataclass
class FolderPlan:
    """One source folder mapped onto a destination folder."""

    source: str
    dest: str
    create: bool


@dataclass
class TransferProgress:
    """Counters accumulated over a migration run."""

    migrated: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)
