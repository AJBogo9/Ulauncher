from __future__ import annotations

import logging
import os
import re
import sys
import time
from typing import TYPE_CHECKING, Any, Callable, cast

from gi.repository import Gdk, Gtk

from ulauncher import paths
from ulauncher.gi import GLib
from ulauncher.internals.results_update import ResultsUpdate
from ulauncher.ui.helpers import layer_shell
from ulauncher.ui.helpers.monitor import get_monitor, get_monitor_geometries
from ulauncher.ui.helpers.theme import DEFAULT_THEME, Theme
from ulauncher.ui.load_icon_surface import load_icon_surface
from ulauncher.ui.results_view import ResultsView
from ulauncher.utils import scheduling
from ulauncher.utils.environment import DESKTOP_ID, is_x11_compatible
from ulauncher.utils.launch_context import take_activation_pending
from ulauncher.utils.settings import Settings

if TYPE_CHECKING:
    from ulauncher.ui.app import UlauncherApp

logger = logging.getLogger(__name__)
# Gnome Wayland doesn't allow apps to position their windows, so we render a fullscreen transparent window
# and position the visible window inside of it via margins
_use_fullscreen_to_position = DESKTOP_ID == "GNOME" and not is_x11_compatible()
# Native Wayland Mutter accepts a launched app's activation token only while the surface that requested it
# still has keyboard focus, checked when the app uses the token. XWayland startup IDs only need to be recent.
_activation_needs_focus = DESKTOP_ID == "GNOME" and not is_x11_compatible()
# Long enough for a running app to take focus, short enough not to hold the window up through a cold start
_APP_FOCUS_TIMEOUT_SEC = 0.5
# Keys that act on the results or on the query state, so a pending query must run before them
_QUERY_FLUSH_KEYS = frozenset(("BackSpace", "Up", "Down", "Tab", "ISO_Left_Tab", "Return", "KP_Enter"))


class UlauncherWindow(Gtk.ApplicationWindow):
    _css_provider: Gtk.CssProvider | None = None
    is_dragging = False
    is_focused = False
    # focus-out events before deferred_init completes are WM noise, not user intent, and
    # must not trigger close_on_focus_out (muffin 6.6 unfocuses the window mid-init: #1801)
    is_ready = False
    layer_shell_enabled = False
    settings: Settings
    _awaiting_app_focus = False

    def __init__(self, **kwargs: Any) -> None:
        logger.info("Building Ulauncher window")
        self.settings = Settings.load(force=True)
        # Settings is a shared instance updated in place, so keep a copy to tell later changes apart
        self._built_settings = dict(self.settings)
        self._applied_css: str | None = None
        self._initial_draw_handler: int | None = None
        # Timers from one open must not act on the next one
        self._timers: list[scheduling.Context] = []
        self._pending_query: scheduling.Context | None = None

        super().__init__(
            decorated=False,
            deletable=False,
            has_focus=True,
            icon_name="ulauncher",
            opacity=0,  # set to 0 so we can show the window and get keyboard input before it's fully loaded
            resizable=False,
            skip_pager_hint=True,
            skip_taskbar_hint=True,
            title="Ulauncher - Application Launcher",
            urgency_hint=True,
            window_position=Gtk.WindowPosition.CENTER,
            **kwargs,
        )
        # avoid checking layer shell support for known cases it does not apply (for performance reasons)
        if (
            not is_x11_compatible()
            and DESKTOP_ID != "GNOME"
            and self.settings.layer_shell
            and layer_shell.is_supported()
        ):
            self.layer_shell_enabled = layer_shell.enable(self)
            if self.layer_shell_enabled:
                logger.info("Layer shell support is enabled")
            else:
                logger.warning(
                    "Layer shell is not supported. If you have issues with window positioning, "
                    "ensure that your compositor supports it and that you have installed the gtk-layer-shell library"
                )

        # Widget structure
        #
        # frame (positioning container for Gnome, not affected by theme)
        # └── shadow_container (provides space for shadow when enabled)
        #     └── theme_root(.app)
        #         ├── drag_listener
        #         │   └── prompt
        #         │       ├── prompt_input (.input)
        #         │       └── prefs_btn (.prefs-btn)
        #         └── results_view (ScrolledWindow)
        #             └── results box (.result-box)
        #                 └── ResultWidget (multiple)

        self.frame = Gtk.Box(valign=Gtk.Align.START)
        self.add(self.frame)

        shadow_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=self._get_shadow_size())
        self.frame.pack_start(shadow_container, True, True, 0)

        self.theme_root = Gtk.Box(app_paintable=True, orientation=Gtk.Orientation.VERTICAL)
        shadow_container.pack_start(self.theme_root, True, True, 0)

        self.prompt = Gtk.Box()
        drag_listener = Gtk.EventBox()
        drag_listener.add(self.prompt)

        self.prompt_input = Gtk.Entry(
            can_default=True,
            can_focus=True,
            has_focus=True,
            is_focus=True,
            height_request=30,
            margin_top=15,
            margin_bottom=15,
            margin_start=20,
            margin_end=20,
            receives_default=True,
        )

        self.prefs_btn = Gtk.Button(
            name="prefs_btn",
            width_request=24,
            height_request=24,
            receives_default=False,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            margin_end=15,
        )

        self.prompt.pack_start(self.prompt_input, True, True, 0)
        self.prompt.pack_end(self.prefs_btn, False, False, 0)

        self.results_view = ResultsView(self.settings, self.apply_css, self.activate_result)

        self.theme_root.pack_start(drag_listener, False, True, 0)
        self.theme_root.pack_start(self.results_view, False, True, 0)

        self.frame.show_all()

        self._connect_signals(drag_listener)

        # Try setting a transparent background
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is None:
            logger.info("Screen does not support alpha channels")
            visual = screen.get_system_visual()

        self.set_visual(visual)

    def open(self) -> None:
        """Show the window: the first time after building it, or again after close() hid it."""
        logger.info("Opening Ulauncher window")
        self._cancel_timers()
        self.is_ready = False
        self.is_dragging = False
        self.is_focused = False
        self._awaiting_app_focus = False

        is_composited = self.get_screen().is_composited()
        logger.debug("Screen is composited: %s", is_composited)
        if not is_composited:
            # without a compositor deferred would lead to "flash of unstyled content"
            self.apply_styling()
        width_request = self.settings.base_width
        height_request = -1
        if _use_fullscreen_to_position and (layout_size := self.get_layout_size()):
            width_request = layout_size.width
            height_request = layout_size.height
        self.set_size_request(width_request, height_request)
        self.position_window()

        if self._initial_draw_handler is None:  # still connected if the last open closed before drawing
            self._initial_draw_handler = self.connect("draw", self.on_initial_draw)
        self.set_keep_above(True)
        super().show()  # MUST be called before present
        self.present()

        if self.query_str != self.prompt_input.get_text():
            self.set_input(self.query_str)

    def is_outdated(self) -> bool:
        """Whether the settings or the theme CSS changed since this window was built."""
        settings = Settings.load(force=True)
        if dict(settings) != self._built_settings:
            return True
        return self._applied_css is not None and self._applied_css != self._load_css(Theme.load(settings.theme_name))

    def _load_css(self, theme: Theme) -> str | None:
        try:
            return theme.get_css(self._get_shadow_size())
        except (OSError, ValueError, re.error):
            logger.exception('Could not load theme "%s"', theme.name)
            return None

    def _start_timer(self, delay_sec: float, callback: Callable[[], None]) -> None:
        self._timers.append(scheduling.timer(delay_sec, callback))

    def _cancel_timers(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()

    def _connect_signals(self, drag_listener: Gtk.EventBox) -> None:
        self.connect("focus-in-event", lambda *_: self.on_focus_in())
        self.connect("focus-out-event", lambda *_: self.on_focus_out())
        self.connect("button-release-event", self.on_mouse_up)
        drag_listener.connect("button-press-event", self.on_mouse_down)
        self.prompt_input.connect("changed", lambda *_: self.on_input_changed())
        self.prompt_input.connect("key-press-event", self.on_input_key_press)
        self.prefs_btn.connect("clicked", lambda *_: self.get_app().show_preferences())
        self.connect("destroy", lambda *_: self._cancel_pending_query())

    def apply_styling(self) -> None:
        """
        Apply styling.

        Note that this method is slow and should be called after the window is shown if possible.
        """
        if self.get_opacity() == 1:  # already applied styling
            return

        self.theme_root.get_style_context().add_class("app")
        self.prompt_input.get_style_context().add_class("input")
        self.prefs_btn.get_style_context().add_class("prefs-btn")
        prefs_icon_surface = load_icon_surface(f"{paths.ASSETS}/icons/gear.svg", 16, self.get_scale_factor())
        self.prefs_btn.set_image(Gtk.Image.new_from_surface(prefs_icon_surface))

        self.apply_theme()
        self.set_opacity(1)

    def deferred_init(self) -> None:
        if not self.get_application() or not self.get_visible():
            # Runs from an idle callback, so the window may already be closed.
            return
        if self.query_str:
            # select all text in the input field.
            # used when user turns off "start with blank query" setting
            self.prompt_input.select_region(0, -1)
        self.apply_styling()
        self.update_results_max_height()
        self.get_app().window_ready()
        # Refreshes the results of a kept query, and runs whatever was typed since the window opened
        self.run_query()
        self.is_ready = True

    ######################################
    # GTK Signal Handlers
    ######################################

    def on_initial_draw(self, *_: tuple[Any]) -> None:
        # ULAUNCHER_PERF_START_BOOTTIME is a perf-test probe (see `make perf` in makefile).
        # When set, report elapsed time from the caller's externally-captured /proc/uptime and
        # exit before deferred_init - this is the earliest point at which keyboard input registers.
        if t0 := os.environ.get("ULAUNCHER_PERF_START_BOOTTIME"):
            elapsed_ms = (time.clock_gettime(time.CLOCK_BOOTTIME) - float(t0)) * 1000
            sys.stdout.write(f"ULAUNCHER_PERF first_draw_ms={elapsed_ms:.2f}\n")
            sys.stdout.flush()
            if app := self.get_application():
                app.quit()
            return
        logger.info("Window shown")
        if self._initial_draw_handler is not None:
            self.disconnect(self._initial_draw_handler)
            self._initial_draw_handler = None
        scheduling.run_when_idle(self.deferred_init)

    def on_focus_out(self) -> None:
        self.is_focused = False
        if self._awaiting_app_focus:
            # The launched app took focus, which is what the window waited for
            self.close()
            return
        # The WM can briefly take focus away when a global hotkey fires, so delay the close to
        # give the window a chance to regain focus before it's actually closed
        if self.is_ready and not self.is_dragging and self.settings.close_on_focus_out:
            self._start_timer(0.07, self._close_if_unfocused)

    def _close_if_unfocused(self) -> None:
        # Runs from a timer, so the window may already be closed.
        if self.is_focused or not self.get_visible():
            return
        self.close(save_query=not self._awaiting_app_focus)

    def _close_if_open(self) -> None:
        # Runs from a timer, so the window may already be closed.
        if self.get_visible():
            self.close()

    def on_focus_in(self) -> None:
        self.is_focused = True
        if self.settings.grab_mouse_pointer:
            self.toggle_grab_pointer_device(True)

    def on_input_changed(self) -> None:
        """
        Triggered by user input
        """
        # Keys that arrive together run one query, for the text the entry holds when the main loop is idle
        if not self._pending_query:
            self._pending_query = scheduling.run_when_idle(self.run_query)

    def run_query(self) -> None:
        """Run the query for the current text now, replacing a pending one."""
        self._cancel_pending_query()
        self.get_app().query_changed(self.prompt_input.get_text())

    def flush_query(self) -> None:
        """Run the pending query now, if there is one, so the results match the typed text."""
        if self._pending_query:
            self.run_query()

    def _cancel_pending_query(self) -> None:
        if self._pending_query:
            self._pending_query.cancel()
            self._pending_query = None

    def activate_result(self, alt: bool) -> None:
        """
        Activate the selected result
        """
        if result := self.results_view.get_active_result():
            self.get_app().activate_result(result, alt)

    def on_input_key_press(self, entry_widget: Gtk.Entry, event: Gdk.EventKey) -> bool:  # noqa: PLR0911
        """
        Triggered by user key press
        Return True to stop other handlers from being invoked for the event
        """
        keyname = Gdk.keyval_name(event.keyval)
        alt = bool(event.state & Gdk.ModifierType.MOD1_MASK)
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        jump_keys = self.settings.get_jump_keys()

        use_arrow_key_aliases = len(self.settings.arrow_key_aliases) == 4  # noqa: PLR2004
        arrow_key_aliases = [*self.settings.arrow_key_aliases] if use_arrow_key_aliases else [None] * 4
        left_alias, down_alias, up_alias, right_alias = arrow_key_aliases
        if not use_arrow_key_aliases:
            logger.warning(
                "Invalid value for arrow_key_aliases: %s, expected four letters", self.settings.arrow_key_aliases
            )

        if keyname == "Escape":
            self.close(save_query=True)
            return True

        if ctrl and keyname == "comma":
            self.get_app().show_preferences()
            return True

        if ctrl or alt or keyname in _QUERY_FLUSH_KEYS:
            self.flush_query()

        if (
            keyname == "BackSpace"
            and not ctrl
            and not entry_widget.get_selection_bounds()
            and entry_widget.get_position() == len(self.query_str)
            and self.get_app().handle_backspace(self.query_str)
        ):
            return True

        if self.results_view.has_results:
            if keyname in ("Up", "ISO_Left_Tab") or (ctrl and keyname == up_alias):
                self.results_view.go_up()
                return True

            if keyname in ("Down", "Tab") or (ctrl and keyname == down_alias):
                self.results_view.go_down()
                return True

            if ctrl and keyname == left_alias:
                entry_widget.set_position(max(0, entry_widget.get_position() - 1))
                return True

            if ctrl and keyname == right_alias:
                entry_widget.set_position(entry_widget.get_position() + 1)
                return True

            if keyname in ("Return", "KP_Enter"):
                self.activate_result(alt)
                return True
            if alt and event.string in jump_keys:
                self.results_view.select(jump_keys.index(event.string))
                return True
        return False

    def on_mouse_down(self, _event_box: Gtk.EventBox, event: Gdk.EventButton) -> None:
        """
        Move the window on drag
        """
        if event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS:
            self.is_dragging = True
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)

    def on_mouse_up(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        self.is_dragging = False
        # With the fullscreen positioning the window is a transparent background, so clicks directly on it
        # means it's a background click and we should treat it as lost focus.
        is_background_click = event.window == self.get_window()
        if _use_fullscreen_to_position and is_background_click and self.settings.close_on_focus_out:
            self.close(save_query=True)
            return True
        return False

    ######################################
    # Helpers
    ######################################

    def get_app(self) -> UlauncherApp:
        return cast("UlauncherApp", self.get_application())

    @property
    def query_str(self) -> str:
        return self.get_app().query

    def apply_css(self, widget: Gtk.Widget) -> None:
        if not self._css_provider:
            self._css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider(
            widget.get_style_context(), self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        if isinstance(widget, Gtk.Container):
            widget.forall(self.apply_css)

    def _get_shadow_size(self) -> int:
        if not self.is_composited():
            return 0

        return self.settings.window_shadow

    def apply_theme(self) -> None:
        if not self._css_provider:
            self._css_provider = Gtk.CssProvider()
        theme = Theme.load(self.settings.theme_name)
        try:
            css = theme.get_css(self._get_shadow_size())
            self._css_provider.load_from_data(css.encode())
            self._applied_css = css
        except (OSError, GLib.Error, ValueError, re.error):
            logger.exception('Could not apply theme "%s"', theme.name)
            # differs from any CSS the theme loads later, so fixing the theme gets a fresh window
            self._applied_css = ""
            fallback = Theme.load(DEFAULT_THEME)
            if fallback.name != theme.name:
                logger.info('Falling back to theme "%s"', fallback.name)
                theme = fallback
                try:
                    self._css_provider.load_from_data(theme.get_css(self._get_shadow_size()).encode())
                except (OSError, GLib.Error, ValueError, re.error):
                    logger.exception('Could not apply fallback theme "%s"', theme.name)
        self.apply_css(self)
        logger.info('Applying theme "%s"', theme.name)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)

    def get_layout_size(self) -> Gdk.Rectangle | None:
        """The monitor size to lay the window out for"""
        # Mutter decides which monitor the window opens on and Wayland doesn't tell us which one
        # it picked, so use the smallest monitor to ensure the window fits on all of them. A window
        # taller than the monitor gets its top edge clamped to the work area, which would shift the
        # content down relative to the monitor.
        if _use_fullscreen_to_position:
            if not (geometries := get_monitor_geometries()):
                return None
            layout_size = Gdk.Rectangle()
            layout_size.width = min(geometry.width for geometry in geometries)
            layout_size.height = min(geometry.height for geometry in geometries)
            return layout_size

        if monitor := get_monitor(self.settings.render_on_screen != "default-monitor"):
            return monitor.get_geometry()
        return None

    def update_results_max_height(self) -> None:
        if layout_size := self.get_layout_size():
            max_height = layout_size.height * 0.8 - self.prompt.get_allocated_height()
            self.results_view.set_max_height(int(max_height))

    def position_window(self) -> None:
        if layout_size := self.get_layout_size():
            window_width = self.settings.base_width
            pos_x = (layout_size.width - window_width) / 2
            pos_y = layout_size.height * 0.1

            self.update_results_max_height()

            if _use_fullscreen_to_position:
                self.frame.set_properties(
                    margin_top=pos_y,
                    margin_bottom=pos_y,
                    margin_start=pos_x,
                    margin_end=pos_x,
                )

            elif self.layer_shell_enabled:
                layer_shell.set_vertical_position(self, pos_y)
            else:
                self.move(int(pos_x + layout_size.x), int(pos_y + layout_size.y))

    def close(self, save_query: bool = False) -> None:
        if take_activation_pending() and _activation_needs_focus and self.is_focused:
            logger.info("Waiting for the launched app to take focus")
            self._awaiting_app_focus = True
            self._start_timer(_APP_FOCUS_TIMEOUT_SEC, self._close_if_open)
            return
        logger.info("Closing Ulauncher window")
        self._cancel_timers()
        if save_query and self.settings.auto_resume:
            self.flush_query()  # the kept query is the whole typed text
        else:
            self._cancel_pending_query()
            # Clears the entry and the results while still open, so the next open starts blank
            self.get_app().set_query("")
            self.flush_query()
        if self.settings.grab_mouse_pointer:
            self.toggle_grab_pointer_device(False)
        # Hiding keeps the window for the next open. The app drops it from its open windows on "hide".
        self.hide()

    def toggle_grab_pointer_device(self, grab: bool) -> None:
        if window := self.get_window():
            seat = window.get_display().get_default_seat()
            if not window or not seat:
                logger.warning("Could not get the pointer device.")
                return

            if not grab:
                seat.ungrab()
                return

            grab_status = seat.grab(window, Gdk.SeatCapabilities.ALL_POINTING, True)
            logger.debug("Focus in event, grabbing pointer: %s", grab_status)

    def set_input(self, query_str: str) -> None:
        self.prompt_input.set_text(query_str)
        self.prompt_input.set_position(-1)

    def show_results(self, update: ResultsUpdate) -> None:
        self.results_view.render(update)
