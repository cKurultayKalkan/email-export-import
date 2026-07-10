import json

from email_export_import.gui.i18n import LOCALES_DIR, I18n, available_locales


def test_locale_files_have_identical_keys():
    tr = json.loads((LOCALES_DIR / "tr.json").read_text())
    en = json.loads((LOCALES_DIR / "en.json").read_text())
    assert set(tr) == set(en)
    assert tr  # not empty


def test_available_locales():
    assert set(available_locales()) == {"en", "tr"}


def test_t_translates_and_formats(tmp_path):
    i = I18n(locale="tr", prefs_path=tmp_path / "gui.json")
    en = I18n(locale="en", prefs_path=tmp_path / "gui.json")
    assert i.t("app.title") != ""
    assert i.t("app.title") != en.t("app.title")  # actually translated
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


def test_constructor_unknown_locale_falls_back(tmp_path):
    i = I18n(locale="xx", prefs_path=tmp_path / "gui.json")
    assert i.locale in {"en", "tr"}
