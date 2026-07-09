from __future__ import annotations

from .models import ProviderPreset

# Gmail exposes every label as an IMAP folder and [Gmail]/All Mail contains
# every message; migrating those alongside the label folders would duplicate
# each message on the destination. Skipped by default (user-editable).
_GMAIL_SKIP = ("[Gmail]/All Mail", "[Gmail]/Important", "[Gmail]/Starred")

PRESETS: dict[str, ProviderPreset] = {
    "gmail": ProviderPreset(
        key="gmail",
        name="Gmail",
        host="imap.gmail.com",
        port=993,
        ssl=True,
        app_password_hint=(
            "Gmail requires an app password (not your normal password): "
            "https://myaccount.google.com/apppasswords"
        ),
        skip_folders=_GMAIL_SKIP,
    ),
    "outlook": ProviderPreset(
        key="outlook",
        name="Outlook / Office365",
        host="outlook.office365.com",
        port=993,
        ssl=True,
        app_password_hint=(
            "Outlook requires an app password: "
            "https://account.live.com/proofs/AppPassword"
        ),
    ),
    "yahoo": ProviderPreset(
        key="yahoo",
        name="Yahoo Mail",
        host="imap.mail.yahoo.com",
        port=993,
        ssl=True,
        app_password_hint=(
            "Yahoo requires an app password: "
            "https://login.yahoo.com/account/security"
        ),
    ),
    "icloud": ProviderPreset(
        key="icloud",
        name="iCloud Mail",
        host="imap.mail.me.com",
        port=993,
        ssl=True,
        app_password_hint=(
            "iCloud requires an app-specific password: "
            "https://appleid.apple.com/account/manage"
        ),
    ),
    "yandex": ProviderPreset(
        key="yandex",
        name="Yandex Mail",
        host="imap.yandex.com",
        port=993,
        ssl=True,
        app_password_hint=(
            "Yandex requires an app password: "
            "https://id.yandex.com/security/app-passwords"
        ),
    ),
}


def get_preset(key: str) -> ProviderPreset:
    """Return the preset for *key*; raises KeyError for unknown providers."""
    return PRESETS[key]


def list_presets() -> list[ProviderPreset]:
    """All presets in stable (insertion) order."""
    return list(PRESETS.values())
