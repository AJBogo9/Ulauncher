"""Stand-in GTK app for the harness, in one of two roles.

previous: the app the user was in before pressing the hotkey. It holds focus at the start of a
  trial and logs every key it receives, which are keys lost to the launcher.
launched: started by the launch stub in place of the real app, with the activation token in its
  environment. It logs when it gets focus, then exits.
"""

from __future__ import annotations

import argparse
import json
import time

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


def now() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("previous", "launched"))
    parser.add_argument("--log", required=True)
    parser.add_argument("--exit-after-focus", type=float, default=0.15)
    args = parser.parse_args()
    log = open(args.log, "a", buffering=1)  # noqa: SIM115

    def emit(ev: str, **fields: object) -> None:
        log.write(json.dumps({"ev": ev, "role": args.role, **fields}) + "\n")

    window = Gtk.Window(title=f"ulh-{args.role}")
    window.set_default_size(1200, 800)
    window.add(Gtk.Label(label=args.role))

    def on_key(_w: Gtk.Widget, event: Gdk.EventKey) -> bool:
        emit("key", t=now(), keyval=event.keyval, name=Gdk.keyval_name(event.keyval))
        return True

    def on_focus(_w: Gtk.Widget, event: Gdk.EventFocus) -> bool:
        emit("focus", t=now(), focus_in=bool(event.in_))
        if event.in_ and args.role == "launched":
            GLib.timeout_add(int(args.exit_after_focus * 1000), Gtk.main_quit)
        return False

    window.connect("key-press-event", on_key)
    window.connect("focus-in-event", on_focus)
    window.connect("focus-out-event", on_focus)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    emit("start", t=now())
    if args.role == "launched":
        GLib.timeout_add(3000, Gtk.main_quit)  # never outlive a trial when focus never comes
    Gtk.main()


if __name__ == "__main__":
    main()
