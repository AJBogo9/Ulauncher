from __future__ import annotations

from typing import Any

import pytest
from pytest_mock import MockerFixture

from ulauncher.gi import GLib
from ulauncher.internals.query import Query
from ulauncher.internals.result import Result
from ulauncher.internals.results_update import results_update
from ulauncher.ui.ulauncher_window import UlauncherWindow


class FakeApp:
    """Answers every query with one result named after it, and records what runs."""

    def __init__(self) -> None:
        self.query = ""
        self.queries: list[str] = []
        self.activated: list[str] = []
        self.window: UlauncherWindow | None = None

    def query_changed(self, query_str: str) -> None:
        self.query = query_str
        self.queries.append(query_str)
        assert self.window
        results = [Result(name=f"result for {query_str}", searchable=True)] if query_str else []
        self.window.show_results(results_update(results, Query(None, query_str)))

    def activate_result(self, result: Result, _alt: bool) -> None:
        self.activated.append(result.name)

    def handle_backspace(self, _query_str: str) -> bool:
        return False


@pytest.fixture
def window(mocker: MockerFixture) -> Any:
    mocker.patch("ulauncher.ui.result_widget.ResultWidget.scroll_to_focus")
    app = FakeApp()
    win = UlauncherWindow()
    app.window = win
    mocker.patch.object(win, "get_app", return_value=app)
    yield win
    win.destroy()


def type_text(window: UlauncherWindow, text: str) -> None:
    for i in range(1, len(text) + 1):
        window.prompt_input.set_text(text[:i])


def press(window: UlauncherWindow, keyname: str) -> bool:
    # Gdk must load after ulauncher.ui has pinned GTK 3
    from gi.repository import Gdk

    event: Any = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.key.keyval = Gdk.keyval_from_name(keyname)
    event.key.state = Gdk.ModifierType(0)
    return window.on_input_key_press(window.prompt_input, event)


def run_idle() -> None:
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def test_typing__runs_one_query_for_keys_that_arrive_together(window: Any) -> None:
    type_text(window, "fire")
    assert window.get_app().queries == []

    run_idle()

    assert window.get_app().queries == ["fire"]


def test_enter__activates_the_result_for_the_whole_text_before_results_are_drawn(window: Any) -> None:
    type_text(window, "firef")

    assert press(window, "Return")

    assert window.get_app().activated == ["result for firef"]


@pytest.mark.parametrize("keyname", ["Down", "Tab", "Up"])
def test_navigation__acts_on_results_for_the_whole_text(window: Any, keyname: str) -> None:
    type_text(window, "fire")

    assert press(window, keyname)

    assert window.get_app().queries == ["fire"]
