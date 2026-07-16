from __future__ import annotations

import json
import locale as locale_module
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
            name: json.loads((LOCALES_DIR / f"{name}.json").read_text(encoding="utf-8"))
            for name in available_locales()
        }
        if locale is not None and locale not in self._tables:
            locale = None
        # Precedence: explicit arg > saved preference > detected OS language >
        # English. A manual selection is persisted and always wins over the OS
        # guess, so the app stays predictable once the user has chosen.
        self.locale = (locale or self._saved_locale()
                       or self._detected_locale() or FALLBACK)

    def _saved_locale(self) -> str | None:
        try:
            saved = json.loads(self._prefs_path.read_text(encoding="utf-8")).get("locale")
        except Exception:
            return None
        return saved if saved in self._tables else None

    def _detected_locale(self) -> str | None:
        """The OS language on first launch (only when nothing is saved). Matched
        against the locales we actually ship; unknown → None → English."""
        lang = _system_locale()
        return lang if lang in self._tables else None

    def set_locale(self, locale: str) -> None:
        if locale not in self._tables:
            raise ValueError(f"unknown locale: {locale!r}")
        self.locale = locale
        from . import prefs

        prefs.save_pref(self._prefs_path, "locale", locale)

    def t(self, key: str, **fmt) -> str:
        text = self._tables.get(self.locale, {}).get(key)
        if text is None:
            text = self._tables.get(FALLBACK, {}).get(key)
        if text is None:
            return key
        return text.format(**fmt) if fmt else text
