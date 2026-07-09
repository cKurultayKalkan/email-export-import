from email_export_import.folders import build_folder_plan, translate_path


def listing(*rows):
    """rows: (name, flags_tuple, delim). Returns IMAPClient-shaped listing."""
    return [(flags, delim.encode(), name) for name, flags, delim in rows]


def test_translate_path_between_delimiters():
    assert translate_path("Work/Projects/2026", "/", ".") == "Work.Projects.2026"
    assert translate_path("INBOX", "/", ".") == "INBOX"
    assert translate_path("A.B", ".", ".") == "A.B"


def test_plan_creates_missing_folders_with_translated_names():
    src = listing(("INBOX", (), "/"), ("Work/Projects", (), "/"))
    dst = listing(("INBOX", (), "."))
    plans = build_folder_plan(src, dst)
    by_source = {p.source: p for p in plans}
    assert by_source["INBOX"].dest == "INBOX"
    assert by_source["INBOX"].create is False
    assert by_source["Work/Projects"].dest == "Work.Projects"
    assert by_source["Work/Projects"].create is True


def test_special_use_maps_to_destination_equivalent():
    src = listing(("Sent Messages", (b"\\Sent",), "/"))
    dst = listing(("Gesendet", (b"\\Sent",), "/"))
    plans = build_folder_plan(src, dst)
    assert plans == [type(plans[0])(source="Sent Messages", dest="Gesendet", create=False)]


def test_special_use_without_dest_match_falls_back_to_name():
    src = listing(("Sent Messages", (b"\\Sent",), "/"))
    dst = listing(("INBOX", (), "/"))
    plans = build_folder_plan(src, dst)
    assert plans[0].dest == "Sent Messages"
    assert plans[0].create is True


def test_skip_folders_and_noselect_are_excluded():
    src = listing(
        ("INBOX", (), "/"),
        ("[Gmail]/All Mail", (b"\\All",), "/"),
        ("[Gmail]", (b"\\Noselect",), "/"),
    )
    dst = listing(("INBOX", (), "/"))
    plans = build_folder_plan(src, dst, skip_folders={"[Gmail]/All Mail"})
    assert [p.source for p in plans] == ["INBOX"]
