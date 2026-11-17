from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from ulauncher.internals.query import Query
from ulauncher.internals.result import Result
from ulauncher.ui.result_widget import ResultWidget

JUMP_KEYS = ["1", "2", "3", "4", "5"]


def noop(*_args: object) -> None:
    pass


class TestResultWidget:
    @pytest.fixture(autouse=True)
    def scroll_to_focus(self, mocker: MockerFixture) -> MagicMock:
        return mocker.patch("ulauncher.ui.result_widget.ResultWidget.scroll_to_focus")

    def test_descr(self) -> None:
        assert len(ResultWidget(Result(), 0, Query("", None), noop, noop, JUMP_KEYS).text_container.get_children()) == 1
        res = Result(description="descr")
        assert len(ResultWidget(res, 0, Query("", None), noop, noop, JUMP_KEYS).text_container.get_children()) == 2
        res = Result(description="descr", compact=True)
        assert len(ResultWidget(res, 0, Query("", None), noop, noop, JUMP_KEYS).text_container.get_children()) == 1

    def test_select(self) -> None:
        result_wgt = ResultWidget(Result(), 0, Query("query", None), noop, noop, JUMP_KEYS)
        style = result_wgt.item_box.get_style_context()
        assert "selected" not in style.list_classes()
        result_wgt.select()
        assert "selected" in style.list_classes()
        result_wgt.deselect()
        assert "selected" not in style.list_classes()

    def test_shortcut(self) -> None:
        result_wgt = ResultWidget(Result(), 0, Query("query", None), noop, noop, JUMP_KEYS)
        assert result_wgt.shortcut_label.get_text() == "Alt+1"
        result_wgt.set_index(2)
        assert result_wgt.shortcut_label.get_text() == "Alt+3"

    def test_wrap__name_and_description_labels_wrap_instead_of_ellipsizing(self) -> None:
        from gi.repository import Gtk, Pango

        res = Result(name="long name", description="long descr", wrap=True)
        widget = ResultWidget(res, 0, Query("", None), noop, noop, JUMP_KEYS)

        name_label = cast("Gtk.Label", widget.title_box.get_children()[0])
        descr_label = cast("Gtk.Label", widget.text_container.get_children()[1])
        for label in (name_label, descr_label):
            assert label.get_line_wrap()
            assert label.get_ellipsize() == Pango.EllipsizeMode.NONE

    def test_wrap__defaults_to_single_ellipsized_line(self) -> None:
        from gi.repository import Gtk, Pango

        res = Result(name="long name", description="long descr")
        widget = ResultWidget(res, 0, Query("", None), noop, noop, JUMP_KEYS)

        name_label = cast("Gtk.Label", widget.title_box.get_children()[0])
        descr_label = cast("Gtk.Label", widget.text_container.get_children()[1])
        for label in (name_label, descr_label):
            assert not label.get_line_wrap()
            assert label.get_ellipsize() == Pango.EllipsizeMode.MIDDLE

    def test_wrap__highlighting_is_skipped(self) -> None:
        from gi.repository import Gtk

        res = Result(name="wrapped name", wrap=True, highlightable=True)
        widget = ResultWidget(res, 0, Query("wrap", None), noop, noop, JUMP_KEYS)

        # highlighting would split the name over multiple labels, which cannot wrap as one paragraph
        children = widget.title_box.get_children()
        assert len(children) == 1
        assert cast("Gtk.Label", children[0]).get_text() == "wrapped name"
        assert not any("item-highlight" in c.get_style_context().list_classes() for c in children)


def _visible_content(widget: ResultWidget) -> dict[str, object]:
    """What a row shows, ignoring hidden widgets a reused row keeps for later."""
    from gi.repository import Gtk

    def labels(container: Gtk.Container) -> list[tuple[str, bool, object, bool]]:
        return [
            (
                child.get_text(),
                "item-highlight" in child.get_style_context().list_classes(),
                child.get_ellipsize(),
                child.get_line_wrap(),
            )
            for child in container.get_children()
            if isinstance(child, Gtk.Label) and child.get_visible()
        ]

    return {
        "title": labels(widget.title_box),
        "text": labels(widget.text_container),
        "title_expands": widget.text_container.child_get_property(widget.title_box, "expand"),
        "shortcut": widget.shortcut_label.get_text(),
        "icon": widget._icon.props.surface,
        "index": widget.index,
        "result": widget.result,
        "selected": "selected" in widget.item_box.get_style_context().list_classes(),
    }


class TestResultWidgetUpdate:
    @pytest.fixture(autouse=True)
    def scroll_to_focus(self, mocker: MockerFixture) -> MagicMock:
        return mocker.patch("ulauncher.ui.result_widget.ResultWidget.scroll_to_focus")

    @pytest.mark.parametrize(
        ("before", "after", "query"),
        [
            pytest.param(
                Result(name="Firefox", searchable=True), Result(name="Files", searchable=True), "fi", id="names"
            ),
            pytest.param(
                Result(name="Terminal", description="Use the command line", searchable=True),
                Result(name="Text Editor", searchable=True),
                "te",
                id="drops_description",
            ),
            pytest.param(
                Result(name="Text Editor", searchable=True),
                Result(name="Terminal", description="Use the command line", searchable=True),
                "te",
                id="adds_description",
            ),
            pytest.param(
                Result(name="a b c d e", searchable=True),
                Result(name="abcde", searchable=True),
                "ace",
                id="fewer_highlight_segments",
            ),
            pytest.param(Result(name="x"), Result(name="Settings", searchable=True), "sts", id="more_segments"),
            pytest.param(Result(name="Firefox", searchable=True), Result(name="Plain"), "fi", id="not_highlighted"),
        ],
    )
    def test_update__shows_what_a_new_row_would(self, before: Result, after: Result, query: str) -> None:
        row = ResultWidget(before, 3, Query(None, "f"), noop, noop, JUMP_KEYS)
        row.select()

        row.update(after, 1, Query(None, query))

        fresh = ResultWidget(after, 1, Query(None, query), noop, noop, JUMP_KEYS)
        assert _visible_content(row) == _visible_content(fresh)

    @pytest.mark.parametrize(
        ("before", "after"),
        [
            (Result(name="a"), Result(name="a", compact=True)),
            (Result(name="a"), Result(name="a", wrap=True)),
            (Result(name="a", wrap=True), Result(name="a")),
        ],
    )
    def test_can_show__only_results_with_the_same_layout(self, before: Result, after: Result) -> None:
        row = ResultWidget(before, 0, Query(None, ""), noop, noop, JUMP_KEYS)
        assert not row.can_show(after)
        assert row.can_show(before)
