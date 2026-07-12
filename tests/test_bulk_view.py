import pytest

flet = pytest.importorskip("flet")

from email_export_import.gui import views  # noqa: E402
from email_export_import.gui.i18n import I18n  # noqa: E402

EN = I18n(locale="en")


def _build():
    view, handles = views.build_bulk(EN, on_start=lambda pairs, pk: None, on_back=lambda: None)
    return view, handles


def test_collect_skips_blank_rows_and_builds_pairs():
    _view, h = _build()
    h["_src"]["host"].value = "src.test"
    h["_dst"]["host"].value = "dst.test"
    h["_dst"]["port"].value = "993"
    h["_add_row"]()  # now 2 rows; fill only the first
    r0 = h["rows"]()[0]
    r0["email"].value = "a@x.com"
    r0["src_pw"].value = "s"
    r0["dst_pw"].value = "d"
    pairs = h["collect"]()
    assert len(pairs) == 1
    src, dst = pairs[0]
    assert src.email == "a@x.com" and src.password == "s"
    assert dst.email == "a@x.com" and dst.host == "dst.test" and dst.password == "d"


def test_collect_no_rows_raises():
    _view, h = _build()
    h["_src"]["host"].value = "src.test"
    h["_dst"]["host"].value = "dst.test"
    with pytest.raises(ValueError):
        h["collect"]()


def test_collect_partial_row_raises():
    _view, h = _build()
    h["_src"]["host"].value = "src.test"
    h["_dst"]["host"].value = "dst.test"
    r0 = h["rows"]()[0]
    r0["email"].value = "a@x.com"  # missing both passwords
    with pytest.raises(ValueError):
        h["collect"]()


def test_collect_bad_dest_raises():
    _view, h = _build()
    h["_src"]["host"].value = "src.test"
    h["_dst"]["host"].value = ""  # no destination host
    r0 = h["rows"]()[0]
    r0["email"].value = "a@x.com"
    r0["src_pw"].value = "s"
    r0["dst_pw"].value = "d"
    with pytest.raises(ValueError):
        h["collect"]()
