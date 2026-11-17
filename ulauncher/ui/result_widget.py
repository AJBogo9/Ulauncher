from __future__ import annotations

import logging
from typing import Callable, cast

from gi.repository import Gdk, Gtk, Pango

from ulauncher.internals.query import Query
from ulauncher.internals.result import Result
from ulauncher.ui.helpers.monitor import get_text_scaling_factor
from ulauncher.ui.helpers.text_highlighter import highlight_text
from ulauncher.ui.load_icon_surface import load_icon_surface

ELLIPSIZE_MIN_LENGTH = 6
ELLIPSIZE_FORCE_AT_LENGTH = 20
logger = logging.getLogger(__name__)


class ResultWidget(Gtk.EventBox):
    index: int = 0
    name: str
    query: Query
    result: Result
    jump_keys: list[str]
    item_box: Gtk.EventBox
    shortcut_label: Gtk.Label
    title_box: Gtk.Box
    text_container: Gtk.Box

    def __init__(
        self,
        result: Result,
        index: int,
        query: Query,
        on_select: Callable[[int], None],
        on_activate: Callable[[int, bool], None],
        jump_keys: list[str],
    ) -> None:
        self._on_select = on_select
        self._on_activate = on_activate
        self.jump_keys = jump_keys
        # A row keeps the layout of its first result; can_show() tells which results it can take after
        self._compact = result.compact
        self._wrap = result.wrap
        self._descr_label: Gtk.Label | None = None
        text_scaling_factor = get_text_scaling_factor()
        self._icon_size = 25 if result.compact else 40
        icon_box = 25 if result.compact else 50
        inner_margin_x = int(5.0 * text_scaling_factor)
        outer_margin_x = int((23.0 if result.compact else 20.0) * text_scaling_factor)
        margin_y = (2 if result.compact else 3) * text_scaling_factor

        super().__init__()
        self.get_style_context().add_class("item-frame")
        self.connect("button-release-event", self.on_click)
        self.connect("enter_notify_event", self.on_mouse_hover)

        self.item_box = Gtk.EventBox()
        self.item_box.get_style_context().add_class("item-box")
        self.add(self.item_box)
        item_container = Gtk.Box()
        item_container.get_style_context().add_class("item-container")
        self.item_box.add(item_container)

        self._icon = Gtk.Image()
        self._icon.set_size_request(icon_box, icon_box)
        self._icon.get_style_context().add_class("item-icon")
        item_container.pack_start(self._icon, False, True, 0)

        self.text_container = Gtk.Box(
            width_request=int(350.0 * text_scaling_factor),
            margin_start=inner_margin_x,
            margin_end=inner_margin_x,
            orientation=Gtk.Orientation.VERTICAL,
            valign=Gtk.Align.CENTER,
        )
        item_container.pack_start(self.text_container, True, True, 0)

        self.shortcut_label = Gtk.Label(justify=Gtk.Justification.RIGHT, width_request=44)
        self.shortcut_label.get_style_context().add_class("item-shortcut")
        self.shortcut_label.get_style_context().add_class("item-text")
        item_container.pack_end(self.shortcut_label, False, True, 0)

        item_container.get_style_context().add_class("small-result-item")

        self.title_box = Gtk.Box()
        self.title_box.get_style_context().add_class("item-name")
        self.title_box.get_style_context().add_class("item-text")
        self.text_container.pack_start(self.title_box, False, True, 0)

        item_container.set_property("margin-start", outer_margin_x)
        item_container.set_property("margin-end", outer_margin_x)
        item_container.set_property("margin-top", margin_y)
        item_container.set_property("margin-bottom", margin_y)

        self.update(result, index, query)

    def can_show(self, result: Result) -> bool:
        """Whether update() can show this result, which needs the same layout as the row's first one."""
        return result.compact == self._compact and result.wrap == self._wrap

    def update(self, result: Result, index: int, query: Query) -> bool:
        """
        Show another result in this row, as a new row for it would.
        Returns True if it added widgets, which still need the theme CSS.
        """
        self.result = result
        self.query = query
        self.deselect()
        self._icon.set_from_surface(
            load_icon_surface(result.icon or "gtk-missing-image", self._icon_size, self.get_scale_factor())
        )
        self.set_index(index)
        # title_box should fill vertical space if there's no description
        should_expand = not result.compact and not result.description
        self.text_container.child_set_property(self.title_box, "expand", should_expand)
        added = self._update_description()
        return self.highlight_name() or added

    def _update_description(self) -> bool:
        added = False
        if self.result.description and not self.result.compact:
            if not self._descr_label:
                self._descr_label = self._make_text_label()
                self._descr_label.get_style_context().add_class("item-descr")
                self._descr_label.get_style_context().add_class("item-text")
                self.text_container.pack_start(self._descr_label, False, True, 0)
                added = True
            self._descr_label.set_text(self.result.description)
            self._descr_label.show()
        elif self._descr_label:
            self._descr_label.hide()
        return added

    def _make_text_label(self, text: str = "") -> Gtk.Label:
        if self.result.wrap:
            return Gtk.Label(label=text, hexpand=True, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        # max_width_chars=1 lets the label shrink below its natural size so ellipsizing kicks in
        return Gtk.Label(label=text, hexpand=True, max_width_chars=1, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)

    def set_index(self, index: int) -> None:
        """
        Set index for the item and assign shortcut
        """
        self.index = index
        self.shortcut_label.set_text(f"Alt+{self.jump_keys[index]}" if index < len(self.jump_keys) else "")

    def select(self) -> None:
        self.item_box.get_style_context().add_class("selected")
        self.scroll_to_focus()

    def deselect(self) -> None:
        self.item_box.get_style_context().remove_class("selected")

    def scroll_to_focus(self) -> None:
        viewport = self.get_ancestor(Gtk.Viewport)
        if not viewport or not isinstance(viewport, Gtk.Viewport):
            return
        viewport_height = viewport.get_allocation().height
        adjustment = viewport.get_vadjustment()
        scroll_y = adjustment.get_value()
        allocation = self.get_allocation()
        bottom = allocation.y + allocation.height
        if scroll_y > allocation.y:  # Scroll up if the widget is above visible area
            adjustment.set_value(allocation.y)
        elif viewport_height + scroll_y < bottom:  # Scroll down if the widget is below visible area
            adjustment.set_value(bottom - viewport_height)

    def highlight_name(self) -> bool:
        """Show the name, with the parts matching the query highlighted. Returns True if it added labels."""
        # (text, is_highlight, ellipsize), where None keeps the wrapping label's settings
        segments: list[tuple[str, bool, Pango.EllipsizeMode | None]]
        if self.result.wrap:
            # highlighting splits the name into labels that cannot reflow as one paragraph
            segments = [(self.result.name, False, None)]
        elif (highlightable_input := self.result.get_highlightable_input(str(self.query))) and (
            self.result.searchable or self.result.highlightable
        ):
            segments = []
            for label_text, is_highlight in highlight_text(highlightable_input, self.result.name):
                ellipsize_min = ELLIPSIZE_MIN_LENGTH if not is_highlight else ELLIPSIZE_FORCE_AT_LENGTH
                ellipsize = Pango.EllipsizeMode.MIDDLE if len(label_text) > ellipsize_min else Pango.EllipsizeMode.NONE
                segments.append((label_text, is_highlight, ellipsize))
        else:
            segments = [(self.result.name, False, Pango.EllipsizeMode.MIDDLE)]

        labels = cast("list[Gtk.Label]", self.title_box.get_children())
        added = False
        for i, (text, is_highlight, ellipsize) in enumerate(segments):
            if i < len(labels):
                label = labels[i]
            else:
                label = self._make_text_label() if self.result.wrap else Gtk.Label()
                # a wrapped label must fill the row so it has a width to reflow against
                self.title_box.pack_start(label, self.result.wrap, self.result.wrap, 0)
                added = True
            label.set_text(text)
            if ellipsize is not None:
                label.set_ellipsize(ellipsize)
            style = label.get_style_context()
            if is_highlight:
                style.add_class("item-highlight")
            else:
                style.remove_class("item-highlight")
            label.show()
        for label in labels[len(segments) :]:
            label.hide()
        return added

    def on_click(self, _widget: Gtk.Widget, event: Gdk.EventButton | None = None) -> None:
        alt = bool(event and event.button != 1)  # right click
        self._on_activate(self.index, alt)

    def on_mouse_hover(self, _widget: Gtk.Widget, event: Gdk.EventCrossing) -> None:
        # event.time is 0 it means the mouse didn't move, but the window scrolled behind the mouse
        if event.time:
            self._on_select(self.index)
