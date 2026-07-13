"""Unit tests for the master-detail main view builders (ui-redesign spec)."""
import pytest

flet = pytest.importorskip("flet")
import flet as ft  # noqa: E402

from email_export_import.gui import views  # noqa: E402
from email_export_import.gui.i18n import I18n  # noqa: E402
from email_export_import.gui.run_manager import RunSnapshot  # noqa: E402

EN = I18n(locale="en")
noop = lambda *a, **k: None  # noqa: E731


def _snap(key="k1", status="running", processed=3, total=10):
    return RunSnapshot(key=key, title=f"{key}@src → {key}@dst", status=status,
                       processed=processed, total=total, current_folder="INBOX")


def _texts(control, out=None):
    out = [] if out is None else out
    v = getattr(control, "value", None)
    if isinstance(v, str):
        out.append(v)
    for ch in getattr(control, "controls", []) or []:
        _texts(ch, out)
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        _texts(content, out)
    return out


def test_run_list_renders_a_row_per_snapshot_with_refs():
    refs: dict = {}
    lst = views.build_run_list(
        EN, [_snap("a"), _snap("b", status="done", processed=10)],
        selected_key="a", on_select=noop, refs=refs,
    )
    texts = _texts(lst)
    assert any("a@src" in t for t in texts)
    assert any("b@src" in t for t in texts)
    # refs registered per row for in-place progress updates
    assert set(refs) >= {"a", "b"}
    assert refs["a"]["bar"] is not None


def test_run_list_empty_state():
    lst = views.build_run_list(EN, [], selected_key=None, on_select=noop, refs={})
    assert EN.t("dash.empty") in _texts(lst)


def test_run_list_row_click_selects():
    picked: list = []
    refs: dict = {}
    lst = views.build_run_list(EN, [_snap("a")], selected_key=None,
                               on_select=lambda k: picked.append(k), refs=refs)
    refs["a"]["row"].on_click(None)
    assert picked == ["a"]


def test_side_panel_shows_selected_run_details():
    refs: dict = {}
    panel = views.build_side_panel(
        EN, _snap("a"), config={"src": {"host": "s.x", "port": 993},
                                "dst": {"host": "d.y", "port": 993}},
        on_pause=noop, on_resume=noop, on_cancel=noop, on_dismiss=noop,
        on_edit=noop, refs=refs,
    )
    texts = _texts(panel)
    assert any("a@src" in t for t in texts)
    assert any("s.x" in t for t in texts)
    assert EN.t("status.running") in texts
    assert refs["_panel"]["bar"] is not None


def test_side_panel_placeholder_when_nothing_selected():
    panel = views.build_side_panel(EN, None, config=None, on_pause=noop,
                                   on_resume=noop, on_cancel=noop,
                                   on_dismiss=noop, on_edit=noop, refs={})
    assert EN.t("panel.none") in _texts(panel)


def test_main_view_composes_list_and_panel():
    refs: dict = {}
    view = views.build_main(
        EN, [_snap("a")], selected_key="a",
        config={"src": {"host": "s"}, "dst": {"host": "d"}},
        on_select=noop, on_pause=noop, on_resume=noop, on_cancel=noop,
        on_dismiss=noop, on_edit=noop, refs=refs,
    )
    assert view.route == "/"
    texts = _texts(view)
    assert EN.t("dash.heading") in texts  # list header
    assert "a" in refs and "_panel" in refs


def test_run_list_bar_never_indeterminate_for_inactive_rows():
    # total=0 (old state files) used to produce ProgressBar(value=None) — an
    # endlessly sweeping "connecting" animation on a paused row.
    refs: dict = {}
    views.build_run_list(
        EN, [RunSnapshot(key="a", title="t", status="paused", processed=19282,
                         total=0, current_folder=None)],
        selected_key=None, on_select=noop, refs=refs,
    )
    assert refs["a"]["bar"].value is not None


def test_side_panel_counter_without_total_is_not_slash_zero():
    refs: dict = {}
    panel = views.build_side_panel(
        EN, RunSnapshot(key="a", title="t", status="paused", processed=19282,
                        total=0, current_folder=None),
        config=None, on_pause=noop, on_resume=noop, on_cancel=noop,
        on_dismiss=noop, on_edit=noop, refs=refs,
    )
    assert refs["_panel"]["counter"].value == "19282"
    assert refs["_panel"]["bar"].value is not None
