from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Iterator

from gi.repository import Gdk, Gtk

from ulauncher.utils import scheduling

if TYPE_CHECKING:
    from ulauncher.internals.query import Query
    from ulauncher.internals.result import Result
    from ulauncher.internals.results_update import ResultsUpdate
    from ulauncher.ui.result_widget import ResultWidget
    from ulauncher.utils.settings import Settings

logger = logging.getLogger(__name__)
# Rows past the visible height are built this many per idle callback, so a key that arrives in
# between waits for one chunk at most
_IDLE_ROWS_CHUNK = 4


class ResultsView(Gtk.ScrolledWindow):
    """Scrollable list of results, owning the result widgets and the selection within them."""

    _has_wrapped_results = False
    _index = 0
    _user_selected = False  # True once the user actively moved the selection (keyboard/mouse)
    _query = ""

    def __init__(
        self,
        settings: Settings,
        apply_css: Callable[[Gtk.Widget], None],
        activate_result: Callable[[bool], None],
    ) -> None:
        super().__init__(
            can_focus=False,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
            shadow_type=Gtk.ShadowType.IN,
        )
        self._settings = settings
        self._apply_css = apply_css
        self._activate_result = activate_result
        # The rows that show the current results: the first ones of _rows
        self._widgets: list[ResultWidget] = []
        # Every row in the box, in order. Rows are reused for later queries and hidden while unused.
        self._rows: list[ResultWidget] = []
        # Results after the built rows, built when idle or as soon as something needs them
        self._unbuilt: list[Result] = []
        self._unbuilt_query: Query | None = None
        self._pending_rows: scheduling.Context | None = None
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._box.get_style_context().add_class("result-box")
        self._box.connect("size-allocate", self._fit_results_height)
        self.add(self._box)
        self.connect("destroy", lambda *_: self._drop_unbuilt())
        # Once, while the box is empty: rows get the CSS as they are built, and adding it again to
        # every reused row would restyle them all
        self._apply_css(self._box)
        # rows manage their own visibility, so this is the only show_all
        self.show_all()

    @property
    def has_results(self) -> bool:
        return bool(self._widgets)

    def set_max_height(self, height: int) -> None:
        self.set_property("max-content-height", height)

    def render(self, update: ResultsUpdate) -> None:
        # A new query resets navigation; streamed batches of the same query keep the user's pick.
        if str(update["query"]) != self._query:
            self._query = str(update["query"])
            self._user_selected = False

        if update["append"] and self._widgets:
            self._append_results(update)
        else:
            self._replace_results(update)

    def get_active_result(self) -> Result | None:
        selected = self._selected
        return selected.result if selected else None

    def select(self, index: int) -> None:
        self._select(index)
        self._user_selected = True

    def go_up(self) -> None:
        self.select((self._index or self._count()) - 1)

    def go_down(self) -> None:
        next_index = self._index + 1
        self.select(next_index if next_index < self._count() else 0)

    def _count(self) -> int:
        return len(self._widgets) + len(self._unbuilt)

    def _replace_results(self, update: ResultsUpdate) -> None:
        previous_pick = self.get_active_result() if self._user_selected else None
        self._drop_unbuilt()
        self._widgets = []
        self._index = 0

        result_list = update["results"][: self._limit()]
        # stock sizing works for single-line results; only wrapped ones need _fit_results_height
        self._has_wrapped_results = any(result.wrap for result in result_list)
        if not self._has_wrapped_results:
            self.set_min_content_height(-1)  # restore stock sizing

        if not result_list:
            # Hide the scroll container when there are no results since it normally takes up a
            # minimum amount of space even if it is empty.
            self._user_selected = False
            self.hide()
            logger.debug("Hiding results container, no results found")
            return

        self._box.set_margin_bottom(10)
        self._box.set_margin_top(3)
        self._unbuilt = list(result_list)
        self._unbuilt_query = update["query"]
        self._build_visible_rows()
        for row in self._rows[len(self._widgets) :]:
            row.hide()
        if self._unbuilt:
            self._pending_rows = scheduling.run_when_idle(self._build_rows_when_idle)
        self._apply_selection(update["selected_name"], previous_pick)
        self.show()
        logger.debug("Render %s results", len(self._widgets))

    def _append_results(self, update: ResultsUpdate) -> None:
        new_results = update["results"][: max(0, self._limit() - self._count())]
        if not new_results:
            return
        if any(result.wrap for result in new_results):
            self._has_wrapped_results = True
        self._unbuilt.extend(new_results)
        self._unbuilt_query = update["query"]
        self._build_rows(len(self._unbuilt))
        # keep the user's pick; only (re)evaluate the default when they haven't navigated
        if not self._user_selected:
            self._apply_selection(update["selected_name"], None)
        self.show()

    def _build_rows(self, count: int) -> list[ResultWidget]:
        """Show the next `count` unbuilt results, in reused rows where the layout allows it."""
        query = self._unbuilt_query
        if query is None:  # nothing was ever queued
            return []
        built = []
        for result in self._unbuilt[:count]:
            index = len(self._widgets)
            reusable = self._rows[index] if index < len(self._rows) else None
            if reusable and reusable.can_show(result):
                widget = reusable
                if widget.update(result, index, query):
                    self._apply_css(widget)
                widget.show()
            else:
                widget = self._new_row(result, index, query)
            self._widgets.append(widget)
            built.append(widget)
        del self._unbuilt[:count]
        return built

    def _new_row(self, result: Result, index: int, query: Query) -> ResultWidget:
        from ulauncher.ui.result_widget import ResultWidget

        widget = ResultWidget(
            result, index, query, self.select, self._select_and_activate, self._settings.get_jump_keys()
        )
        self._box.add(widget)
        if index < len(self._rows):  # replaces a row with another layout
            self._box.reorder_child(widget, index)
            self._rows[index].destroy()
            self._rows[index] = widget
        else:
            self._rows.append(widget)
        self._apply_css(widget)
        widget.show_all()
        return widget

    def _build_visible_rows(self) -> None:
        """Build rows until they fill the visible height, or all of them if there is no limit."""
        max_height = self.get_property("max-content-height")
        if max_height <= 0:
            self._build_rows(len(self._unbuilt))
            return
        height = 0
        while self._unbuilt and height < max_height:
            (widget,) = self._build_rows(1)
            height += widget.get_preferred_height()[1]

    def _build_rows_when_idle(self) -> None:
        self._build_rows(_IDLE_ROWS_CHUNK)
        self._pending_rows = scheduling.run_when_idle(self._build_rows_when_idle) if self._unbuilt else None

    def _ensure_built(self, index: int) -> None:
        if index >= len(self._widgets) and self._unbuilt:
            self._build_rows(index + 1 - len(self._widgets))
            if not self._unbuilt:
                self._drop_unbuilt()

    def _drop_unbuilt(self) -> None:
        if self._pending_rows:
            self._pending_rows.cancel()
            self._pending_rows = None
        self._unbuilt = []

    def _select_and_activate(self, index: int, alt: bool) -> None:
        self.select(index)
        self._activate_result(alt)

    def _apply_selection(self, selected_name: str | None, previous_pick: Result | None) -> None:
        # Keep the user's pick across a streaming replace if it is still present.
        if previous_pick:
            for index, result in enumerate(self._results()):
                if result.name == previous_pick.name:
                    self.select(index)
                    return
            self._user_selected = False
        self._select(self._index_for_name(selected_name))

    def _results(self) -> Iterator[Result]:
        """Every result in the list, built into a row or not."""
        yield from (widget.result for widget in self._widgets)
        yield from self._unbuilt

    def _select(self, index: int) -> None:
        if not self._widgets:
            return
        if not 0 <= index < self._count():
            index = 0
        self._ensure_built(index)
        if self._selected:
            self._selected.deselect()
        self._index = index
        self._widgets[index].select()

    @property
    def _selected(self) -> ResultWidget | None:
        if len(self._widgets) > self._index:
            return self._widgets[self._index]
        return None

    def _limit(self) -> int:
        return len(self._settings.get_jump_keys()) or 25

    def _index_for_name(self, name: str | None) -> int:
        """Index of the first searchable result with this name, or 0 if none matches."""
        for index, result in enumerate(self._results()):
            if result.searchable and result.name == name:
                return index
        return 0

    def _fit_results_height(self, box: Gtk.Box, allocation: Gdk.Rectangle) -> None:
        """GtkScrolledWindow measures its natural height without height-for-width,
        clipping wrapped (Result.wrap) labels - request the real height instead."""
        if not self._has_wrapped_results or allocation.width <= 0:
            return  # nothing to fit, or an early allocation pass with no usable width yet
        current_height = self.get_min_content_height()
        max_height = self.get_property("max-content-height")
        needed_height = box.get_preferred_height_for_width(allocation.width)[1]
        if max_height > 0:
            needed_height = min(needed_height, max_height)
        if abs(needed_height - current_height) > 1:  # tolerance: a scrollbar can shift the width by ~1px
            self.set_min_content_height(needed_height)
            # the in-progress allocation pass ignores the new request
            scheduling.run_when_idle(self.queue_resize)
