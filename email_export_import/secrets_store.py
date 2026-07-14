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

SERVICE = "email-export-import"


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
    return _keyring() is not None


def _account_key(host: str, email: str, role: str) -> str:
    # role distinguishes the source and destination passwords for one address.
    return f"{role}:{email}@{host}"


def save_password(host: str, email: str, role: str, password: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(SERVICE, _account_key(host, email, role), password)
        return True
    except Exception:
        return False


def get_password(host: str, email: str, role: str) -> str | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(SERVICE, _account_key(host, email, role))
    except Exception:
        return None


def delete_password(host: str, email: str, role: str) -> None:
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(SERVICE, _account_key(host, email, role))
    except Exception:
        pass
