import pytest

from email_export_import.providers import PRESETS, get_preset, list_presets


def test_all_expected_presets_exist():
    assert set(PRESETS) == {"gmail", "outlook", "yahoo", "icloud", "yandex"}


def test_all_presets_use_ssl_993():
    for p in PRESETS.values():
        assert p.ssl is True
        assert p.port == 993


def test_all_presets_have_app_password_hint():
    for p in PRESETS.values():
        assert p.app_password_hint


def test_gmail_skip_list_covers_duplicating_labels():
    gmail = get_preset("gmail")
    assert "[Gmail]/All Mail" in gmail.skip_folders
    assert "[Gmail]/Important" in gmail.skip_folders
    assert "[Gmail]/Starred" in gmail.skip_folders


def test_hosts():
    assert get_preset("gmail").host == "imap.gmail.com"
    assert get_preset("outlook").host == "outlook.office365.com"
    assert get_preset("yahoo").host == "imap.mail.yahoo.com"
    assert get_preset("icloud").host == "imap.mail.me.com"
    assert get_preset("yandex").host == "imap.yandex.com"


def test_get_preset_unknown_raises():
    with pytest.raises(KeyError):
        get_preset("aol")


def test_list_presets_returns_all():
    assert {p.key for p in list_presets()} == set(PRESETS)
