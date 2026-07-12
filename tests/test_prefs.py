import json

from email_export_import.gui import prefs


def test_save_pref_merges_and_preserves_other_keys(tmp_path):
    p = tmp_path / "gui.json"
    prefs.save_pref(p, "locale", "tr")
    prefs.save_pref(p, "max_active", 3)
    data = json.loads(p.read_text())
    assert data == {"locale": "tr", "max_active": 3}
    assert prefs.load_prefs(p) == {"locale": "tr", "max_active": 3}


def test_load_prefs_missing_file_is_empty(tmp_path):
    assert prefs.load_prefs(tmp_path / "nope.json") == {}
