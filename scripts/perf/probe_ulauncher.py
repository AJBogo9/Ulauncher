"""Run `ulauncher start` from a source tree with timing probes and a launch stub.

Used by harness.py inside its private headless gnome-shell. The probes hook GDK event dispatch,
GtkEntry "changed" and the frame clock of each toplevel, so they keep working when the window
code is refactored. Two monkeypatches are name-based and skip themselves when the name is gone:
ResultsView.render (to know which query the shown results belong to) and launch_detached (to
record the launch instead of starting the app).

Every probe writes one JSON line to $ULH_PROBE_LOG with CLOCK_MONOTONIC seconds, the clock the
harness also uses.
"""

from __future__ import annotations

import json
import os
import sys
import time

TREE = os.environ["ULH_TREE"]
LOG = open(os.environ["ULH_PROBE_LOG"], "a", buffering=1)  # noqa: SIM115
FAKE_LAUNCH = os.environ.get("ULH_FAKE_LAUNCH")  # argv (json) of the app started in place of the real one


def now() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def emit(ev: str, **fields: object) -> None:
    LOG.write(json.dumps({"ev": ev, **fields}) + "\n")


sys.argv = [os.path.join(TREE, "bin", "ulauncher"), "start", *sys.argv[1:]]
sys.path.insert(0, TREE)


def install_probes() -> bool:
    from gi.repository import Gdk, GObject, Gtk

    # strong references, so a recreated window never reuses an address and looks hooked already
    hooked_clocks: list[Gdk.FrameClock] = []
    # an unmapped window's widget may be destroyed already, so remember names from the map event
    mapped_names: dict[Gdk.Window, str] = {}

    def toplevel_name(widget: Gtk.Widget | None) -> str:
        top = widget.get_toplevel() if widget else None
        return type(top).__name__ if top else "?"

    def focused_text(widget: Gtk.Widget | None) -> str | None:
        top = widget.get_toplevel() if widget else None
        focus = top.get_focus() if isinstance(top, Gtk.Window) else None
        return focus.get_text() if isinstance(focus, Gtk.Entry) else None

    def hook_frame_clock(window: Gtk.Window) -> None:
        clock = window.get_frame_clock()
        if not clock or any(clock is c for c in hooked_clocks):
            return
        hooked_clocks.append(clock)
        name = type(window).__name__
        frame: dict[str, float] = {}

        def phase(key: str) -> None:
            frame[key] = now()

        def after_paint(*_: object) -> None:
            emit(
                "frame",
                win=name,
                t_before=frame.get("before"),
                t_layout=frame.get("layout"),
                t_paint=frame.get("paint"),
                t_after=now(),
            )
            frame.clear()

        clock.connect("before-paint", lambda *_: phase("before"))
        clock.connect("layout", lambda *_: phase("layout"))
        clock.connect("paint", lambda *_: phase("paint"))
        clock.connect("after-paint", after_paint)

    def on_event(event: Gdk.Event) -> None:
        etype = event.get_event_type()
        widget = Gtk.get_event_widget(event)
        if etype == Gdk.EventType.KEY_PRESS:
            _, keyval = event.get_keyval()
            text = focused_text(widget)
            t0 = now()
            Gtk.main_do_event(event)
            t1 = now()
            emit(
                "key",
                t0=t0,
                t1=t1,
                keyval=keyval,
                name=Gdk.keyval_name(keyval),
                text_before=text,
                win=toplevel_name(widget),
                etime=event.get_time(),
            )
            return
        t0 = now()
        Gtk.main_do_event(event)
        t1 = now()
        if etype == Gdk.EventType.FOCUS_CHANGE:
            emit("focus", t=t0, focus_in=bool(event.focus_change.in_), win=toplevel_name(widget))
        elif etype == Gdk.EventType.MAP:
            if isinstance(widget, Gtk.Window):
                hook_frame_clock(widget)
            mapped_names[event.window] = name = toplevel_name(widget)
            emit("map", t=t0, win=name)
        elif etype == Gdk.EventType.UNMAP:
            emit("unmap", t=t0, win=mapped_names.pop(event.window, None) or toplevel_name(widget))
        elif t1 - t0 > 0.004:
            emit("slow_event", t0=t0, t1=t1, type=etype.value_nick, win=toplevel_name(widget))

    Gdk.event_handler_set(on_event)

    def on_changed(entry: Gtk.Entry, *_: object) -> bool:
        emit("changed", t=now(), text=entry.get_text())
        return True

    # GtkEditable's signals are registered when GtkEntry's class is first initialized
    Gtk.Entry().destroy()
    GObject.add_emission_hook(Gtk.Entry, "changed", on_changed)

    try:
        from ulauncher.ui.results_view import ResultsView
    except ImportError:
        ResultsView = None  # noqa: N806
    if ResultsView is not None and hasattr(ResultsView, "render"):
        original_render = ResultsView.render

        def render(self: object, update: dict, *args: object, **kwargs: object) -> object:
            t0 = now()
            ret = original_render(self, update, *args, **kwargs)
            results = update["results"]
            emit(
                "render",
                t0=t0,
                t1=now(),
                query=str(update["query"]),
                n=len(results),
                first=results[0].name if results else None,
                append=bool(update.get("append")),
            )
            return ret

        ResultsView.render = render

    # The whole query: search, row rebuild and the layout it queues (layout and paint come in "frame")
    from ulauncher.ui.app import UlauncherApp

    if hasattr(UlauncherApp, "query_changed"):
        original_query_changed = UlauncherApp.query_changed

        def query_changed(self: object, query_str: str, *args: object, **kwargs: object) -> object:
            t0 = now()
            ret = original_query_changed(self, query_str, *args, **kwargs)
            emit("query", t0=t0, t1=now(), text=query_str)
            return ret

        UlauncherApp.query_changed = query_changed

    emit("ready", t=now(), pid=os.getpid())
    return False


def install_launch_stub() -> None:
    import ulauncher.modes.apps.launch_app as launch_app_module

    def launch_detached(cmd: list[str], _working_dir: str | None = None, *, startup_notify_id: str | None) -> None:
        emit("launch", t=now(), cmd=cmd, token=bool(startup_notify_id))
        if FAKE_LAUNCH:
            import subprocess

            env = dict(os.environ)
            env.pop("ULH_PROBE_LOG", None)
            if startup_notify_id:
                env["XDG_ACTIVATION_TOKEN"] = startup_notify_id
                env["DESKTOP_STARTUP_ID"] = startup_notify_id
            subprocess.Popen(json.loads(FAKE_LAUNCH), env=env, start_new_session=True)

    if hasattr(launch_app_module, "launch_detached"):
        launch_app_module.launch_detached = launch_detached
    else:
        emit("warning", msg="launch_app.launch_detached not found; launches are not stubbed")


def main() -> int:
    from ulauncher import cli
    from ulauncher.gi import GLib

    install_launch_stub()
    # GTK installs its own event handler during gtk_init, which runs inside app.run(), so the probes
    # go in from the first main loop iteration
    GLib.idle_add(install_probes, priority=GLib.PRIORITY_HIGH)
    return cli.run_command(cli.get_args())


if __name__ == "__main__":
    sys.exit(main())
