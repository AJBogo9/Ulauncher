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
        self.backspaced: list[str] = []
        self.window: UlauncherWindow | None = None

    def query_changed(self, query_str: str) -> None:
        self.query = query_str
        self.queries.append(query_str)
        assert self.window
        results = [Result(name=f"result for {query_str}", searchable=True)] if query_str else []
        self.window.show_results(results_update(results, Query(None, query_str)))

    def activate_result(self, result: Result, _alt: bool) -> None:
        self.activated.append(result.name)

    def window_ready(self) -> None:
        pass

    def handle_backspace(self, query_str: str) -> bool:
        self.backspaced.append(query_str)
        return False

    def set_query(self, value: str, update_input: bool = True) -> None:
        self.query = value
        if update_input and self.window:
            self.window.set_input(value)


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
        window.prompt_input.set_position(-1)  # typing leaves the cursor at the end


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


def test_focus_out__closes_at_once_when_the_launched_app_takes_focus(window: Any, mocker: MockerFixture) -> None:
    timer = mocker.patch("ulauncher.ui.ulauncher_window.scheduling.timer")
    window.show()
    window.is_ready = True
    window._awaiting_app_focus = True

    window.on_focus_out()

    assert not window.get_visible()
    timer.assert_not_called()


def test_focus_out__waits_before_closing_otherwise(window: Any, mocker: MockerFixture) -> None:
    """The compositor can take focus away for a moment around the hotkey."""
    timer = mocker.patch("ulauncher.ui.ulauncher_window.scheduling.timer")
    window.show()
    window.is_ready = True

    window.on_focus_out()

    assert window.get_visible()
    timer.assert_called_once()


def test_open__gives_the_entry_keyboard_focus_back(window: Any) -> None:
    """A kept window remembers its focus widget, e.g. the preferences button after a click on it."""
    window.open()
    window.prefs_btn.grab_focus()
    window.close()

    window.open()

    assert window.get_focus() is window.prompt_input


def test_close__clears_results_when_the_entry_is_already_empty(window: Any) -> None:
    window.open()
    type_text(window, "f")
    window.flush_query()
    window.prompt_input.set_text("")  # BackSpace, then Escape before the query for "" ran

    window.close(save_query=True)

    assert not window.results_view.has_results


def test_close__hides_even_if_clearing_the_query_raises(window: Any, mocker: MockerFixture) -> None:
    window.open()
    mocker.patch.object(window.get_app(), "set_query", side_effect=RuntimeError("from a mode"))

    with pytest.raises(RuntimeError):
        window.close()

    assert not window.get_visible()


def test_enter__acts_on_the_input_a_mode_rewrote_while_flushing(window: Any) -> None:
    app = window.get_app()
    answer = app.query_changed

    def fold_home(query_str: str) -> None:
        # like the file browser, which rewrites /home/<user>/... to ~/... and renders after
        if query_str.startswith("/home/user/"):
            app.set_query("~/" + query_str.removeprefix("/home/user/"))
            return
        answer(query_str)

    app.query_changed = fold_home
    type_text(window, "/home/user/x")

    assert press(window, "Return")

    assert app.activated == ["result for ~/x"]


@pytest.mark.parametrize("change", ["shadow", "text_scaling"])
def test_is_outdated__after_a_layout_input_changed(window: Any, mocker: MockerFixture, change: str) -> None:
    assert not window.is_outdated()
    if change == "shadow":
        mocker.patch.object(window, "_get_shadow_size", return_value=window._get_shadow_size() + 7)
    else:
        mocker.patch("ulauncher.ui.ulauncher_window.get_text_scaling_factor", return_value=3.25)

    assert window.is_outdated()


def test_close__collects_garbage_once_idle(window: Any, mocker: MockerFixture) -> None:
    """A full collection walks every long-lived object; done after a close, it stays out of typing."""
    collect = mocker.patch("ulauncher.ui.ulauncher_window.gc.collect")
    window.open()
    run_idle()
    collect.reset_mock()

    window.close()
    collect.assert_not_called()
    run_idle()

    collect.assert_called_once_with()


def test_backspace__leaves_a_pending_query_pending(window: Any) -> None:
    """Deleting coalesces like typing does: BackSpace does not act on the results."""
    type_text(window, "fire")

    press(window, "BackSpace")

    assert window.get_app().queries == []


def test_backspace__asks_about_the_typed_text_while_a_query_is_pending(window: Any) -> None:
    type_text(window, "~/Doc")

    press(window, "BackSpace")

    assert window.get_app().backspaced == ["~/Doc"]


class FakeTheme:
    name = "fake"

    def __init__(self, css: str | Exception) -> None:
        self.css = css

    def get_css(self, _shadow_size: int) -> str:
        if isinstance(self.css, Exception):
            raise self.css
        return self.css


@pytest.fixture
def theme(mocker: MockerFixture) -> FakeTheme:
    theme = FakeTheme(".app { color: red; }")
    mocker.patch("ulauncher.ui.ulauncher_window.Theme.load", return_value=theme)
    return theme


def open_fully(window: Any, mocker: MockerFixture) -> None:
    """Open, then run what the first frame triggers."""
    window.open()
    mocker.patch.object(window, "get_application", return_value=window.get_app())
    window.deferred_init()


def test_is_outdated__does_not_load_the_theme(window: Any, mocker: MockerFixture, theme: FakeTheme) -> None:
    """Loading a theme scans the theme folders and reads its CSS, about 1 ms before the window shows."""
    open_fully(window, mocker)
    window.close()
    load = mocker.patch("ulauncher.ui.ulauncher_window.Theme.load", return_value=theme)

    assert not window.is_outdated()

    load.assert_not_called()


def test_open__applies_theme_css_edited_since_the_last_open(
    window: Any, mocker: MockerFixture, theme: FakeTheme
) -> None:
    open_fully(window, mocker)
    window.close()
    theme.css = ".app { color: blue; }"

    open_fully(window, mocker)

    assert "rgb(0,0,255)" in window._css_provider.to_string()


def test_open__logs_a_broken_theme_once(
    window: Any, mocker: MockerFixture, theme: FakeTheme, caplog: pytest.LogCaptureFixture
) -> None:
    theme.css = OSError("theme folder gone")
    for _ in range(3):
        open_fully(window, mocker)
        window.close()

    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1
