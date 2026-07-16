import json
import re

from email_export_import.gui import i18n as i18n_mod
from email_export_import.gui.i18n import LOCALES_DIR, I18n, available_locales

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _load(name: str) -> dict:
    return json.loads((LOCALES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def test_every_locale_has_the_english_key_set():
    en = _load("en")
    assert en  # not empty
    for name in available_locales():
        table = _load(name)
        missing = set(en) - set(table)
        extra = set(table) - set(en)
        assert not missing, f"{name}.json is missing keys: {sorted(missing)}"
        assert not extra, f"{name}.json has unknown keys: {sorted(extra)}"


def test_every_locale_preserves_placeholders():
    # A translation that drops or renames a {token} would crash .format() at
    # runtime — every locale must carry exactly the same placeholders per key.
    en = _load("en")
    for name in available_locales():
        if name == "en":
            continue
        table = _load(name)
        for key, en_val in en.items():
            want = set(_PLACEHOLDER.findall(en_val))
            got = set(_PLACEHOLDER.findall(table[key]))
            assert want == got, f"{name}.json[{key}]: placeholders {got} != {want}"


def test_available_locales_matches_disk_and_has_core():
    expected = {p.stem for p in LOCALES_DIR.glob("*.json")}
    assert set(available_locales()) == expected
    assert {"en", "tr"} <= expected
    assert all(re.fullmatch(r"[a-z]{2}", loc) for loc in expected)


def test_auto_detects_os_language_when_nothing_saved(tmp_path, monkeypatch):
    # First launch, nothing saved: a shipped OS language is used...
    monkeypatch.setattr(i18n_mod, "_system_locale", lambda: "tr")
    assert I18n(prefs_path=tmp_path / "a.json").locale == "tr"
    # ...an OS language we don't ship falls back to English.
    monkeypatch.setattr(i18n_mod, "_system_locale", lambda: "xx")
    assert I18n(prefs_path=tmp_path / "b.json").locale == "en"


def test_saved_preference_overrides_os_language(tmp_path, monkeypatch):
    monkeypatch.setattr(i18n_mod, "_system_locale", lambda: "tr")
    prefs = tmp_path / "gui.json"
    I18n(locale="en", prefs_path=prefs).set_locale("en")  # user chose English
    assert I18n(prefs_path=prefs).locale == "en"  # not the OS's Turkish


def test_t_translates_and_formats(tmp_path):
    i = I18n(locale="tr", prefs_path=tmp_path / "gui.json")
    en = I18n(locale="en", prefs_path=tmp_path / "gui.json")
    assert i.t("app.title") != ""
    # app.title is the product name, intentionally identical in every locale —
    # use a UI string to prove translations actually differ.
    assert i.t("dash.heading") != en.t("dash.heading")
    assert "5" in en.t("plan.total", count=5)


def test_missing_key_falls_back_to_key(tmp_path):
    i = I18n(locale="tr", prefs_path=tmp_path / "gui.json")
    assert i.t("no.such.key") == "no.such.key"


def test_set_locale_persists(tmp_path):
    prefs = tmp_path / "gui.json"
    i = I18n(locale="en", prefs_path=prefs)
    i.set_locale("tr")
    assert (prefs.stat().st_mode & 0o777) == 0o600
    assert I18n(prefs_path=prefs).locale == "tr"


def test_set_locale_rejects_unknown(tmp_path):
    import pytest

    i = I18n(locale="en", prefs_path=tmp_path / "gui.json")
    with pytest.raises(ValueError):
        i.set_locale("xx")
    assert i.locale == "en"  # unchanged


def test_constructor_unknown_locale_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(i18n_mod, "_system_locale", lambda: None)
    i = I18n(locale="xx", prefs_path=tmp_path / "gui.json")
    assert i.locale == "en"
