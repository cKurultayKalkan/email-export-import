"""Optional password storage in the OS secure store (macOS Keychain, Windows
Credential Manager, Linux Secret Service) via keyring.

Design constraints, deliberately strict:
- OFF by default. A password is only ever stored when the user ticks
  "remember" for that account; nothing is written otherwise.
- The OS keychain only — never a file, never our own crypto. The state and
  spool files stay password-free, so they remain safe to copy.
- Fail closed and quiet: if no keychain backend is available (a headless
  Linux box with no Secret Service, say), storing silently no-ops and the
  app keeps asking for the password each time, exactly as before this
  feature existed. It must never crash a migration over a keychain hiccup.
"""
from __future__ import annotations

import subprocess
import sys

SERVICE = "email-export-import"


# ---- macOS: the `security` CLI (always present, nothing to bundle) ----------
# keyring 25.x pulls jaraco.*/more-itertools, which flet build does not bundle
# into the packaged app (same class of problem as certifi) — so the keychain
# feature would silently never work in the shipped mac app. The `security`
# command is part of macOS and talks to the same login keychain, so we use it
# directly there and keep keyring only as a fallback for other platforms.

def _security(*args: str, input_text: str | None = None):
    return subprocess.run(
        ["security", *args],
        input=input_text, capture_output=True, text=True, timeout=10,
    )


class _MacSecurity:
    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin"

    @staticmethod
    def set_password(service: str, account: str, password: str) -> None:
        # -U updates in place if the item already exists.
        r = _security("add-generic-password", "-a", account, "-s", service,
                      "-w", password, "-U")
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "security add failed")

    @staticmethod
    def get_password(service: str, account: str) -> str | None:
        r = _security("find-generic-password", "-a", account, "-s", service, "-w")
        if r.returncode != 0:
            return None
        return r.stdout.rstrip("\n")

    @staticmethod
    def delete_password(service: str, account: str) -> None:
        _security("delete-generic-password", "-a", account, "-s", service)


def _backend():
    """The password store for this platform, or None if none is usable."""
    if _MacSecurity.available():
        return _MacSecurity
    return _keyring()


def _keyring():
    try:
        import keyring

        # A usable backend? The "fail" and "null" backends raise/return None;
        # treat them as "no store available".
        from keyring.backends import fail

        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def available() -> bool:
    """True when a real OS keychain backend is present."""
    return _backend() is not None


def _account_key(host: str, email: str, role: str) -> str:
    # role distinguishes the source and destination passwords for one address.
    return f"{role}:{email}@{host}"


def save_password(host: str, email: str, role: str, password: str) -> bool:
    backend = _backend()
    if backend is None:
        return False
    try:
        backend.set_password(SERVICE, _account_key(host, email, role), password)
        return True
    except Exception:
        return False


def get_password(host: str, email: str, role: str) -> str | None:
    backend = _backend()
    if backend is None:
        return None
    try:
        return backend.get_password(SERVICE, _account_key(host, email, role))
    except Exception:
        return None


def delete_password(host: str, email: str, role: str) -> None:
    backend = _backend()
    if backend is None:
        return
    try:
        backend.delete_password(SERVICE, _account_key(host, email, role))
    except Exception:
        pass
