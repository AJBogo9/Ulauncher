from __future__ import annotations

from typing import Callable, Iterator
from unittest.mock import MagicMock, PropertyMock

from pytest_mock import MockerFixture

from ulauncher.core import UlauncherCore
from ulauncher.internals import effects
from ulauncher.internals.query import Query
from ulauncher.internals.result import Result


class TestStreamingResults:
    """Core wires streamed RENDER_RESULTS through the buffer; ResultBuffer owns the batching
    behavior itself (see tests/internals/test_result_buffer.py)."""

    def test_streamed_render_stamps_query_and_pick(self, mocker: MockerFixture) -> None:
        core = UlauncherCore()
        core.query = Query(None, "foo")
        mocker.patch.object(UlauncherCore, "last_query_result_pick", new_callable=PropertyMock, return_value="picked")
        outer = MagicMock()
        emit = core._mode_callback(None, outer)  # valid_mode=None skips the active-mode staleness guard

        r = Result(name="a")
        emit(effects.render_results([r], append=False, final=True))

        update = outer.call_args.args[0]
        assert update["results"] == [r]
        assert update["append"] is False
        assert str(update["query"]) == "foo"
        assert update["selected_name"] == "picked"

    def test_activating_drops_pending_stream_paint(self, mocker: MockerFixture) -> None:
        # A throttled paint armed mid-stream must not fire over what an activation renders next.
        core = UlauncherCore()
        captured: list[Callable[[], None]] = []
        mocker.patch(
            "ulauncher.internals.result_buffer.scheduling.timer",
            side_effect=lambda _d, fn: (captured.append(fn), MagicMock())[1],
        )
        render = MagicMock()
        core._mode_callback(None, render)(effects.render_results([Result(name="a")], final=False))
        assert captured  # a flush was scheduled
        render.assert_not_called()  # but it hasn't painted yet

        core.activate_result(Result(name="a"), MagicMock(), alt=True)
        captured[0]()  # the stale flush fires after activation
        render.assert_not_called()


def test_load_triggers__hands_a_dropped_keyword_to_the_mode_that_lost_it(mocker: MockerFixture) -> None:
    from ulauncher.internals.result import KeywordTrigger
    from ulauncher.modes.mode import Mode

    class KeywordMode(Mode):
        def __init__(self, keywords: list[str]) -> None:
            self.keywords = keywords
            self.changed = True

        def handle_query(self, _query: Query, _callback: Callable[[effects.EffectMessage], None]) -> None: ...

        def activate_result(
            self, _action_id: str, _result: Result, _query: Query, _callback: Callable[[effects.EffectMessage], None]
        ) -> None: ...

        def has_trigger_changes(self) -> bool:
            return self.changed

        def get_triggers(self) -> Iterator[KeywordTrigger]:
            self.changed = False
            for keyword in self.keywords:
                yield KeywordTrigger(name=f"{keyword} from {id(self)}", keyword=keyword)

    first, second = KeywordMode(["g"]), KeywordMode(["g"])
    mocker.patch("ulauncher.core.get_modes", return_value=[first, second])
    core = UlauncherCore()
    core.load_triggers()
    assert core._keyword_cache[first] == {"g": mocker.ANY}

    first.keywords = []  # e.g. the shortcut "g" was deleted
    first.changed = True
    core.load_triggers()

    assert "g" in core._keyword_cache[second]


def test_handle_backspace__uses_the_mode_of_the_text_it_gets(mocker: MockerFixture) -> None:
    """The window asks before the query for this text has run, so the last query's mode can be stale."""
    from ulauncher.modes.mode import Mode

    class PathMode(Mode):
        def matches_query_str(self, query_str: str) -> bool:
            return query_str.startswith("~/")

        def handle_backspace(self, query_str: str) -> Query | None:
            return Query(None, query_str.rsplit("/", 2)[0] + "/")

        def handle_query(self, _query: Query, _callback: Callable[[effects.EffectMessage], None]) -> None: ...

        def activate_result(
            self, _action_id: str, _result: Result, _query: Query, _callback: Callable[[effects.EffectMessage], None]
        ) -> None: ...

    mocker.patch("ulauncher.core.get_modes", return_value=[PathMode()])
    emit = mocker.patch("ulauncher.core._events.emit")
    core = UlauncherCore()
    core.set_query("", MagicMock())

    assert core.handle_backspace("~/Documents/notes/")

    emit.assert_called_once_with("app:set_query", "~/Documents/")
