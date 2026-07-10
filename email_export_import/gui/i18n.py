from __future__ import annotations

import json
import locale as locale_module
import os
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
DEFAULT_PREFS_PATH = Path.home() / ".email-export-import" / "gui.json"
FALLBACK = "en"


def available_locales() -> list[str]:
    return sorted(p.stem for p in LOCALES_DIR.glob("*.json"))


def _system_locale() -> str | None:
    try:
        lang = locale_module.getlocale()[0] or ""
    except Exception:
        return None
    return lang.split("_")[0].lower() or None


class I18n:
    """Tiny translation layer: active locale -> English -> the key itself."""

    def __init__(self, locale: str | None = None, prefs_path: Path | None = None) -> None:
        self._prefs_path = prefs_path or DEFAULT_PREFS_PATH
        self._tables = {
            name: json.loads((LOCALES_DIR / f"{name}.json").read_text())
            for name in available_locales()
        }
        self.locale = (
            locale
            or self._saved_locale()
            or (_system_locale() if _system_locale() in self._tables else None)
            or FALLBACK
        )

    def _saved_locale(self) -> str | None:
        try:
            saved = json.loads(self._prefs_path.read_text()).get("locale")
        except Exception:
            return None
        return saved if saved in self._tables else None

    def set_locale(self, locale: str) -> None:
        self.locale = locale
        self._prefs_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            self._prefs_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"locale": locale}))
        os.chmod(self._prefs_path, 0o600)

    def t(self, key: str, **fmt) -> str:
        text = self._tables.get(self.locale, {}).get(key)
        if text is None:
            text = self._tables.get(FALLBACK, {}).get(key)
        if text is None:
            return key
        return text.format(**fmt) if fmt else text
