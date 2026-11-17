"""End-to-end typing benchmark for Ulauncher in a private headless gnome-shell.

Usage (from the repo root, with a clean environment):

    env -i HOME=$HOME PATH=/usr/bin:/bin LANG=C.UTF-8 python3 scripts/perf/harness.py snapshot
    env -i HOME=$HOME PATH=/usr/bin:/bin LANG=C.UTF-8 python3 scripts/perf/harness.py run \
        --tree base=/tmp/ulauncher-base --tree new=.

`snapshot` copies the user's Ulauncher, dconf and app dirs into .perf/profile once. `run` starts a
private dbus-daemon (with a curated service list and no system bus), a headless gnome-shell with a
virtual monitor, a stand-in "previous app" that holds focus, and Ulauncher from each tree through
probe_ulauncher.py. Keys are injected with org.gnome.Mutter.RemoteDesktop on the private bus, so
nothing reaches the real desktop. Each trial: toggle, wait 0 to 100 ms, type the word at 20 to
70 ms per key, press Enter. Trees alternate per round to spread machine noise.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROFILE = REPO / ".perf" / "profile"
RESULTS = REPO / ".perf" / "results"
SYSTEM_PYTHON = "/usr/bin/python3"
VENV_PYTHON = REPO / ".venv" / "bin" / "python"
HOME = Path(os.environ["HOME"])
# The live session's values (read from the running ulauncher.service process on 2026-11-17)
XDG_DATA_DIRS = (
    f"{HOME}/.local/share/flatpak/exports/share:/var/lib/flatpak/exports/share:/usr/local/share/:/usr/share/"
    ":/var/lib/snapd/desktop"
)
# Only these D-Bus services can be activated on the private bus. Everything else (portals,
# gsconnect, evolution, the installed io.ulauncher.Ulauncher) stays out of the session.
SERVICES = (
    "ca.desrt.dconf.service",
    "org.a11y.Bus.service",
    "org.freedesktop.IBus.service",
    "org.gnome.Shell.Notifications.service",
)
WAYLAND_DISPLAY = "ulh-0"
UL_BUS = "io.ulauncher.Ulauncher"
RD_BUS = "org.gnome.Mutter.RemoteDesktop"
KEYSYM_RETURN = 0xFF0D
KEYSYM_ESCAPE = 0xFF1B
LAUNCHER_WIN = "UlauncherWindow"


def now() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def keysym(char: str) -> int:
    return ord(char) if char != " " else 0x20


def ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 2)


class LogTail:
    """Reads the JSON lines appended to a file since the last call."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.pos = 0
        self.events: list[dict[str, Any]] = []

    def poll(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open() as f:
            f.seek(self.pos)
            chunk = f.read()
        # keep a partial last line for the next poll
        end = chunk.rfind("\n") + 1
        self.pos += len(chunk[:end].encode())
        new = [json.loads(line) for line in chunk[:end].splitlines() if line.strip()]
        self.events.extend(new)
        return new

    def wait_for(self, pred: Any, timeout: float) -> dict[str, Any] | None:
        deadline = now() + timeout
        for ev in self.events:
            if pred(ev):
                return ev
        while now() < deadline:
            for ev in self.poll():
                if pred(ev):
                    return ev
            time.sleep(0.005)
        return None


def snapshot() -> None:
    """Copy the user's dirs that Ulauncher and GTK read into .perf/profile."""
    if PROFILE.exists():
        shutil.rmtree(PROFILE)
    copies = {
        HOME / ".config/dconf/user": PROFILE / "config/dconf/user",
        HOME / ".config/ulauncher": PROFILE / "config/ulauncher",
        HOME / ".config/gtk-3.0": PROFILE / "config/gtk-3.0",
        HOME / ".local/state/ulauncher": PROFILE / "state/ulauncher",
        HOME / ".local/share/ulauncher": PROFILE / "data/ulauncher",
        HOME / ".local/share/applications": PROFILE / "data/applications",
        HOME / ".local/share/icons": PROFILE / "data/icons",
        HOME / ".local/share/fonts": PROFILE / "data/fonts",
    }
    for src, dst in copies.items():
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns("*.log"))
        else:
            shutil.copy2(src, dst)
    os.chmod(PROFILE, 0o700)
    print(f"snapshot written to {PROFILE}")


@dataclass
class Session:
    """A private dbus-daemon plus headless gnome-shell plus the previous-app stand-in."""

    keep: bool = False
    refresh: int = 100
    rt: Path = field(init=False)
    procs: list[subprocess.Popen] = field(default_factory=list)

    def __post_init__(self) -> None:
        # XDG_RUNTIME_DIR holds the wayland and bus sockets, so it needs a short path
        self.rt = Path(tempfile.mkdtemp(prefix="ulh.", dir="/tmp"))
        os.chmod(self.rt, 0o700)
        self.fake_log = self.rt / "fake.jsonl"

    @property
    def bus_address(self) -> str:
        return f"unix:path={self.rt}/bus"

    def base_env(self) -> dict[str, str]:
        rt = self.rt
        return {
            "HOME": str(rt / "home"),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
            "XDG_RUNTIME_DIR": str(rt),
            "XDG_CONFIG_HOME": str(rt / "config"),
            "XDG_DATA_HOME": str(rt / "data"),
            "XDG_CACHE_HOME": str(rt / "cache"),
            "XDG_STATE_HOME": str(rt / "state"),
            "XDG_DATA_DIRS": XDG_DATA_DIRS,
            "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
            "XDG_SESSION_DESKTOP": "ubuntu",
            "XDG_SESSION_TYPE": "wayland",
            "XDG_SESSION_CLASS": "user",
            "XDG_MENU_PREFIX": "gnome-",
            "DESKTOP_SESSION": "ubuntu",
            "DBUS_SESSION_BUS_ADDRESS": self.bus_address,
            # an empty private system bus: gnome-shell needs one, but nothing in here may reach
            # logind, GDM, bluez or NetworkManager
            "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={rt}/system-bus",
        }

    def client_env(self) -> dict[str, str]:
        return {**self.base_env(), "WAYLAND_DISPLAY": WAYLAND_DISPLAY, "GTK_MODULES": "gail:atk-bridge"}

    def _prepare_dirs(self) -> None:
        if not PROFILE.exists():
            sys.exit("no profile snapshot: run `harness.py snapshot` first")
        rt = self.rt
        for name in ("home", "cache", "dbus-services"):
            (rt / name).mkdir()
        shutil.copytree(PROFILE / "config", rt / "config", symlinks=True)
        shutil.copytree(PROFILE / "state", rt / "state", symlinks=True)
        data = rt / "data"
        data.mkdir()
        for entry in (PROFILE / "data").iterdir():
            if entry.name == "ulauncher":
                shutil.copytree(entry, data / "ulauncher", symlinks=True, ignore=shutil.ignore_patterns("extensions"))
                # 170 MB of extension code, only read
                (data / "ulauncher" / "extensions").symlink_to(entry / "extensions")
            else:
                (data / entry.name).symlink_to(entry)
        for service in SERVICES:
            (rt / "dbus-services" / service).symlink_to(f"/usr/share/dbus-1/services/{service}")
        for conf, bus_type, address, service_dir in (
            ("dbus.conf", "session", self.bus_address, f"<servicedir>{rt}/dbus-services</servicedir>"),
            ("system-bus.conf", "system", f"unix:path={rt}/system-bus", ""),
        ):
            (rt / conf).write_text(
                '<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"\n'
                ' "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">\n'
                f"<busconfig><type>{bus_type}</type><keep_umask/><listen>{address}</listen>{service_dir}"
                '<policy context="default"><allow send_destination="*" eavesdrop="true"/>'
                '<allow eavesdrop="true"/><allow own="*"/></policy></busconfig>\n'
            )

    def spawn(self, argv: list[str], env: dict[str, str], log_name: str) -> subprocess.Popen:
        log = (self.rt / log_name).open("ab")
        proc = subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(proc)
        return proc

    def start(self) -> None:
        self._prepare_dirs()
        env = self.base_env()
        for conf, sock in (("dbus.conf", "bus"), ("system-bus.conf", "system-bus")):
            self.spawn(
                ["dbus-daemon", f"--config-file={self.rt}/{conf}", "--nofork", "--nopidfile"], env, f"{conf}.log"
            )
            wait_path(self.rt / sock, 5)
        self.bus = Gio.DBusConnection.new_for_address_sync(
            self.bus_address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
        # The snapshot carries the user's enabled shell extensions; none may run in here
        for key, value in (("disable-user-extensions", "true"), ("enabled-extensions", "@as []")):
            subprocess.run(["dconf", "write", f"/org/gnome/shell/{key}", value], env=env, check=True)
        self.spawn(
            [
                "gnome-shell",
                "--headless",
                "--wayland",
                "--no-x11",
                "--virtual-monitor",
                f"1920x1080@{self.refresh}",
                "--wayland-display",
                WAYLAND_DISPLAY,
            ],
            env,
            "shell.log",
        )
        wait_path(self.rt / WAYLAND_DISPLAY, 20)
        self.wait_name(RD_BUS, 20)
        self.wait_name("org.gnome.Shell", 20)
        self.spawn(
            [SYSTEM_PYTHON, str(HERE / "fake_app.py"), "previous", "--log", str(self.fake_log)],
            self.client_env(),
            "fake-previous.log",
        )
        self.fake = LogTail(self.fake_log)
        session_path = self.call(RD_BUS, "/org/gnome/Mutter/RemoteDesktop", RD_BUS, "CreateSession", None)[0]
        self.rd_session = session_path
        self.call(RD_BUS, session_path, f"{RD_BUS}.Session", "Start", None)
        # The shell opens the overview at startup, and the overview keeps keyboard focus. The
        # OverviewActive property ignores writes from here, but Escape closes it.
        deadline = now() + 20
        while not self.fake.wait_for(lambda e: e["ev"] == "focus" and e["role"] == "previous" and e["focus_in"], 0.5):
            if now() > deadline:
                raise RuntimeError("the previous-app stand-in never got focus")
            with contextlib.suppress(GLib.Error):  # the name is owned before the object is exported
                if self.get_shell_prop("OverviewActive"):
                    self.key(KEYSYM_ESCAPE, True)
                    self.key(KEYSYM_ESCAPE, False)

    def call(self, name: str, path: str, iface: str, method: str, args: GLib.Variant | None) -> Any:
        reply = self.bus.call_sync(name, path, iface, method, args, None, Gio.DBusCallFlags.NONE, 5000, None)
        return reply.unpack() if reply else None

    def get_shell_prop(self, prop: str) -> Any:
        return self.call(
            "org.gnome.Shell",
            "/org/gnome/Shell",
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", ("org.gnome.Shell", prop)),
        )[0]

    def has_name(self, name: str) -> bool:
        return bool(
            self.call(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                GLib.Variant("(s)", (name,)),
            )[0]
        )

    def wait_name(self, name: str, timeout: float, present: bool = True) -> None:
        deadline = now() + timeout
        while now() < deadline:
            if self.has_name(name) == present:
                return
            time.sleep(0.02)
        raise RuntimeError(f"timed out waiting for {name} to {'appear' if present else 'go away'}")

    def key(self, sym: int, pressed: bool) -> None:
        self.call(
            RD_BUS,
            self.rd_session,
            f"{RD_BUS}.Session",
            "NotifyKeyboardKeysym",
            GLib.Variant("(ub)", (sym, pressed)),
        )

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.call(RD_BUS, self.rd_session, f"{RD_BUS}.Session", "Stop", None)
        for proc in reversed(self.procs):
            terminate(proc)
        # D-Bus activated services and extension processes are not our children; find them by
        # the private runtime dir in their environment
        for pid in pids_with_env(f"XDG_RUNTIME_DIR={self.rt}"):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
        time.sleep(0.3)
        for pid in pids_with_env(f"XDG_RUNTIME_DIR={self.rt}"):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        if not self.keep:
            shutil.rmtree(self.rt, ignore_errors=True)


def terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(3)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(3)


def pids_with_env(needle: str) -> list[int]:
    found = []
    raw = needle.encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        with contextlib.suppress(OSError):
            if raw in (entry / "environ").read_bytes().split(b"\0"):
                found.append(int(entry.name))
    return found


def wait_path(path: Path, timeout: float) -> None:
    deadline = now() + timeout
    while not path.exists():
        if now() > deadline:
            raise RuntimeError(f"timed out waiting for {path}")
        time.sleep(0.02)


@dataclass
class Launcher:
    """One Ulauncher process from a source tree, running inside a Session."""

    session: Session
    label: str
    tree: Path
    experiments: str = ""

    def start(self) -> None:
        if self.session.has_name(UL_BUS):
            raise RuntimeError("another Ulauncher still owns the bus name")
        self.log_path = self.session.rt / f"probe-{self.label}.jsonl"
        env = {
            **self.session.client_env(),
            "ULH_TREE": str(self.tree),
            "ULH_PROBE_LOG": str(self.log_path),
            "ULH_FAKE_LAUNCH": json.dumps(
                [SYSTEM_PYTHON, str(HERE / "fake_app.py"), "launched", "--log", str(self.session.fake_log)]
            ),
            "ULAUNCHER_SYSTEM_DATA_DIR": str(self.tree / "data"),
            "PYTHONPATH": str(self.tree),
            "ULH_EXPERIMENTS": self.experiments,
        }
        self.proc = self.session.spawn(
            [str(VENV_PYTHON), str(HERE / "probe_ulauncher.py")], env, f"ulauncher-{self.label}.log"
        )
        self.probe = LogTail(self.log_path)
        if not self.probe.wait_for(lambda e: e["ev"] == "ready", 20):
            raise RuntimeError(
                f"{self.label}: probes never installed, see {self.session.rt}/ulauncher-{self.label}.log"
            )
        self.session.wait_name(UL_BUS, 20)
        time.sleep(3)  # extensions start and the triggers warm up when idle

    def stop(self) -> None:
        terminate(self.proc)
        self.session.procs.remove(self.proc)
        self.session.wait_name(UL_BUS, 10, present=False)


def run_trial(launcher: Launcher, word: str, interval: float, delay: float) -> dict[str, Any]:
    """Toggle, type `word` and press Enter on a schedule, then collect what the probes saw."""
    session = launcher.session
    probe = launcher.probe
    probe.poll()
    session.fake.poll()
    t_toggle = now()
    toggle = subprocess.Popen([str(launcher.tree / "bin" / "ulauncher"), "toggle"], env=session.client_env())
    schedule = []  # (time, keysym, pressed, char)
    start = t_toggle + delay
    for i, char in enumerate([*word, "\n"]):
        sym = KEYSYM_RETURN if char == "\n" else keysym(char)
        schedule.append((start + i * interval, sym, True, char))
        schedule.append((start + i * interval + interval / 2, sym, False, char))
    schedule.sort(key=lambda s: s[0])
    injected = []  # (char, keysym, t_inject) for presses
    for t, sym, pressed, char in schedule:
        while (wait := t - now()) > 0:
            time.sleep(min(wait, 0.002) if wait < 0.004 else wait - 0.002)
        t_inject = now()
        session.key(sym, pressed)
        if pressed:
            injected.append((char, sym, t_inject))
    t_enter = injected[-1][2]
    toggle.wait(5)

    closed = probe.wait_for(lambda e: e["ev"] == "unmap" and e["win"] == LAUNCHER_WIN and e["t"] > t_enter, 3)
    if not closed:
        # Enter did nothing: close the window so the next trial starts from the same state
        session.key(KEYSYM_ESCAPE, True)
        session.key(KEYSYM_ESCAPE, False)
        probe.wait_for(lambda e: e["ev"] == "unmap" and e["win"] == LAUNCHER_WIN and e["t"] > t_enter, 3)
    # focus is back on the previous app once the launched stand-in exits
    session.fake.wait_for(
        lambda e: e["ev"] == "focus" and e["role"] == "previous" and e["focus_in"] and e["t"] > t_enter, 5
    )
    time.sleep(0.3)
    probe.poll()
    session.fake.poll()
    events = [e for e in probe.events if event_time(e) >= t_toggle]
    fake_events = [e for e in session.fake.events if e["t"] >= t_toggle]
    trial = analyze(word, interval, delay, t_toggle, injected, events, fake_events)
    trial["raw"] = {"t_toggle": t_toggle, "injected": injected, "events": events, "fake_events": fake_events}
    return trial


def event_time(event: dict[str, Any]) -> float:
    for key in ("t", "t0", "t_after"):
        if key in event:
            return event[key]
    return 0.0


def first(events: list[dict[str, Any]], pred: Any) -> dict[str, Any] | None:
    return next((e for e in events if pred(e)), None)


def analyze(
    word: str,
    interval: float,
    delay: float,
    t_toggle: float,
    injected: list[tuple[str, int, float]],
    events: list[dict[str, Any]],
    fake_events: list[dict[str, Any]],
) -> dict[str, Any]:
    frames = [e for e in events if e["ev"] == "frame" and e["win"] == LAUNCHER_WIN]
    keys = [e for e in events if e["ev"] == "key" and e["win"] == LAUNCHER_WIN]
    renders = [e for e in events if e["ev"] == "render"]

    presented = {e["counter"]: e["t"] for e in events if e["ev"] == "presented" and e["win"] == LAUNCHER_WIN}

    def painted_after(t: float) -> float | None:
        frame = first(frames, lambda f: f["t_after"] > t)
        return frame["t_after"] if frame else None

    def presented_after(t: float) -> float | None:
        frame = first(frames, lambda f: f["t_after"] > t)
        return presented.get(frame.get("counter")) if frame else None

    focus = first(events, lambda e: e["ev"] == "focus" and e["focus_in"] and e["win"] == LAUNCHER_WIN)
    action = first(events, lambda e: e["ev"] == "action" and e["name"] == "toggle-window")
    mapped = first(events, lambda e: e["ev"] == "map" and e["win"] == LAUNCHER_WIN)
    first_frame = frames[0] if frames else None
    key_rows = []
    used: set[int] = set()
    for i, (char, sym, t_inject) in enumerate(injected):
        # The event time is Mutter's CLOCK_MONOTONIC in whole ms when it handled the key. A received
        # key belongs to this injection if Mutter handled it before the next injection of the same
        # keysym, so a lost first "f" is never paired with the second one.
        t_next_same = next((t for _, s, t in injected[i + 1 :] if s == sym), float("inf"))
        match = None
        for j, k in enumerate(keys):
            if j not in used and k["keyval"] == sym and t_inject - 0.002 <= k["etime"] / 1000 < t_next_same - 0.002:
                match = k
                used.add(j)
                break
        prefix = word[: i + 1] if char != "\n" else word
        render = first(renders, lambda r, p=prefix, t=t_inject: r["query"] == p and r["t0"] >= t)
        row = {
            "char": char,
            "t_inject": t_inject,
            "received": match is not None,
            "queue_ms": ms(match["t0"] - t_inject) if match else None,
            "handler_ms": ms(match["t1"] - match["t0"]) if match else None,
            "text_before": match["text_before"] if match else None,
        }
        if char != "\n":
            painted = painted_after(match["t1"]) if match else None
            row["key_to_paint_ms"] = ms(painted - t_inject) if painted else None
            on_screen = presented_after(match["t1"]) if match else None
            row["key_to_screen_ms"] = ms(on_screen - t_inject) if on_screen else None
            results_painted = painted_after(render["t1"]) if render else None
            row["results_ms"] = ms(results_painted - t_inject) if results_painted else None
        key_rows.append(row)

    enter = injected[-1]
    enter_row = key_rows[-1]
    launch = first(events, lambda e: e["ev"] == "launch" and e["t"] >= enter[2])
    unmap = first(events, lambda e: e["ev"] == "unmap" and e["win"] == LAUNCHER_WIN and e["t"] >= enter[2])
    app_focus = first(fake_events, lambda e: e["role"] == "launched" and e["ev"] == "focus" and e["focus_in"])
    lost_to_previous = "".join(chr(e["keyval"]) for e in fake_events if e["role"] == "previous" and e["ev"] == "key")
    typed_rows = key_rows[:-1]
    last_char = typed_rows[-1]
    frame_ms = [ms(f["t_after"] - f["t_before"]) for f in frames if f["t_before"] and f["t_after"] > enter[2] - 2]
    typed_queries = [q for q in events if q["ev"] == "query" and q["text"] and q["t0"] < enter[2] + 1]
    # the probe's phase handlers run after GTK's, so t_layout and t_paint mark where those phases end
    paint_ms = [ms(f["t_paint"] - f["t_layout"]) for f in frames if f.get("t_layout") and f.get("t_paint")]
    return {
        "word": word,
        "interval_ms": round(interval * 1000),
        "delay_ms": round(delay * 1000),
        "toggle_to_focus_ms": ms(focus["t"] - t_toggle) if focus else None,
        "toggle_to_first_frame_ms": ms(frames[0]["t_after"] - t_toggle) if frames else None,
        "toggle_to_action_ms": ms(action["t"] - t_toggle) if action else None,
        "action_to_map_ms": ms(mapped["t"] - action["t"]) if action and mapped else None,
        "first_frame_to_focus_ms": ms(focus["t"] - first_frame["t_after"]) if focus and first_frame else None,
        "keys": key_rows,
        "text_at_enter": enter_row["text_before"],
        "lost_chars": sum(1 for r in typed_rows if not r["received"]),
        "lost_to_previous": lost_to_previous,
        "final_results_ms": last_char.get("results_ms"),
        "enter_received": enter_row["received"],
        "enter_to_launch_ms": ms(launch["t"] - enter[2]) if launch else None,
        "launch_cmd": launch["cmd"] if launch else None,
        "enter_to_hidden_ms": ms(unmap["t"] - enter[2]) if unmap else None,
        "enter_to_app_focus_ms": ms(app_focus["t"] - enter[2]) if app_focus else None,
        "max_frame_ms": max((f for f in frame_ms if f is not None), default=None),
        "queries": len(typed_queries),
        "query_ms": [ms(q["t1"] - q["t0"]) for q in typed_queries],
        "paint_ms": paint_ms,
    }


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def summarize(label: str, trials: list[dict[str, Any]], reference: dict[str, Any]) -> dict[str, Any]:
    def stats(values: list[float | None]) -> dict[str, float | int | None]:
        present = [v for v in values if v is not None]
        if not present:
            return {"n": 0, "median": None, "p90": None, "max": None}
        return {
            "n": len(present),
            "median": round(statistics.median(present), 1),
            "p90": round(pct(present, 0.9), 1),
            "max": round(max(present), 1),
        }

    char_rows = [k for t in trials for k in t["keys"] if k["char"] != "\n"]
    wrong = [t for t in trials if t["launch_cmd"] != reference[t["word"]]["launch_cmd"] or t["launch_cmd"] is None]
    return {
        "label": label,
        "trials": len(trials),
        "wrong_or_no_launch": len(wrong),
        "lost_chars": sum(t["lost_chars"] for t in trials),
        "lost_to_previous_app": sum(len(t["lost_to_previous"]) for t in trials),
        "toggle_to_focus_ms": stats([t["toggle_to_focus_ms"] for t in trials]),
        "toggle_to_first_frame_ms": stats([t["toggle_to_first_frame_ms"] for t in trials]),
        "toggle_to_action_ms": stats([t.get("toggle_to_action_ms") for t in trials]),
        "action_to_map_ms": stats([t.get("action_to_map_ms") for t in trials]),
        "first_frame_to_focus_ms": stats([t.get("first_frame_to_focus_ms") for t in trials]),
        "key_queue_ms": stats([k["queue_ms"] for k in char_rows]),
        "key_handler_ms": stats([k["handler_ms"] for k in char_rows]),
        "key_to_paint_ms": stats([k["key_to_paint_ms"] for k in char_rows]),
        "key_to_screen_ms": stats([k.get("key_to_screen_ms") for k in char_rows]),
        "final_results_ms": stats([t["final_results_ms"] for t in trials]),
        "enter_handler_ms": stats([t["keys"][-1]["handler_ms"] for t in trials]),
        "enter_to_launch_ms": stats([t["enter_to_launch_ms"] for t in trials]),
        "enter_to_hidden_ms": stats([t["enter_to_hidden_ms"] for t in trials]),
        "enter_to_app_focus_ms": stats([t["enter_to_app_focus_ms"] for t in trials]),
        "max_frame_ms": stats([t["max_frame_ms"] for t in trials]),
        "query_ms": stats([q for t in trials for q in t.get("query_ms", [])]),
        "paint_ms": stats([p for t in trials for p in t.get("paint_ms", [])]),
        "queries_per_trial": stats([t.get("queries") for t in trials]),
    }


def print_summary(summaries: list[dict[str, Any]]) -> None:
    metrics = [k for k, v in summaries[0].items() if isinstance(v, dict)]
    counts = ["trials", "wrong_or_no_launch", "lost_chars", "lost_to_previous_app"]
    rows = [(key, [str(s[key]) for s in summaries]) for key in counts]
    rows.append(("(ms) median / p90 / max", ["" for _ in summaries]))
    for key in metrics:
        cells = []
        for s in summaries:
            v = s[key]
            cells.append("-" if v["median"] is None else f"{v['median']:g} / {v['p90']:g} / {v['max']:g}")
        rows.append((key.removesuffix("_ms"), cells))
    labels = [s["label"] for s in summaries]
    width = max(len(c) for c in [*labels, *(c for _, cells in rows for c in cells)]) + 3
    print(f"\n{'':24}" + "".join(f"{label:>{width}}" for label in labels))
    for key, cells in rows:
        print(f"{key:24}" + "".join(f"{c:>{width}}" for c in cells))


def export_rev(rev: str) -> Path:
    """Unpack a commit's files into .perf/trees/<sha>, so a tree can be measured without a checkout."""
    sha = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--verify", f"{rev}^{{commit}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dest = REPO / ".perf" / "trees" / sha[:12]
    if not dest.exists():
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        archive = subprocess.Popen(["git", "-C", str(REPO), "archive", sha], stdout=subprocess.PIPE)
        subprocess.run(["tar", "-x", "-C", str(tmp)], stdin=archive.stdout, check=True)
        if archive.wait() != 0:
            raise RuntimeError(f"git archive {sha} failed")
        tmp.rename(dest)
    return dest


def run(args: argparse.Namespace) -> None:
    trees = []
    for spec in args.tree:
        label, _, path = spec.rpartition("=")
        path, _, experiments = path.partition("+")
        tree = export_rev(path.removeprefix("git:")) if path.startswith("git:") else Path(path).resolve()
        trees.append((label or tree.name, tree, experiments))
    intervals = [float(x) / 1000 for x in args.intervals.split(",")]
    delays = [float(x) / 1000 for x in args.delays.split(",")]
    words = args.words.split(",")
    session = Session(keep=args.keep, refresh=args.refresh)
    trials: dict[str, list[dict[str, Any]]] = {label: [] for label, _, _ in trees}
    references: dict[str, dict[str, Any]] = {label: {} for label, _, _ in trees}
    try:
        session.start()
        for round_no in range(args.rounds):
            order = trees if round_no % 2 == 0 else list(reversed(trees))
            for label, tree, experiments in order:
                launcher = Launcher(session, label, tree, experiments)
                launcher.start()
                try:
                    for word in words:
                        for _ in range(args.warmup):
                            run_trial(launcher, word, 0.15, 0.6)
                        ref = run_trial(launcher, word, 0.15, 0.6)
                        references[label].setdefault(word, ref)
                        for interval in intervals:
                            for delay in delays:
                                trial = run_trial(launcher, word, interval, delay)
                                trial["round"] = round_no
                                trials[label].append(trial)
                                if args.verbose:
                                    print(
                                        f"{label} r{round_no} {word} {trial['interval_ms']}ms+{trial['delay_ms']}ms:"
                                        f" focus {trial['toggle_to_focus_ms']} text@enter {trial['text_at_enter']!r}"
                                        f" launch {trial['enter_to_launch_ms']} {trial['launch_cmd']}"
                                    )
                finally:
                    launcher.stop()
    finally:
        session.stop()
    summaries = [summarize(label, trials[label], references[label]) for label, _, _ in trees]
    print_summary(summaries)
    for label, _, _ in trees:
        for word, ref in references[label].items():
            print(f"reference launch for {word!r} ({label}): {ref['launch_cmd']}")
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{'-vs-'.join(label for label, _, _ in trees)}.json"
    out.write_text(
        json.dumps({"args": vars(args), "summaries": summaries, "references": references, "trials": trials}, indent=1)
    )
    print(f"\nraw results: {out}")
    if args.keep:
        print(f"session dir kept: {session.rt}")


def reanalyze(path: Path) -> None:
    data = json.loads(path.read_text())
    summaries = []
    for label, trials in data["trials"].items():
        fresh = []
        for t in trials:
            raw = t["raw"]
            redone = analyze(
                t["word"],
                t["interval_ms"] / 1000,
                t["delay_ms"] / 1000,
                raw["t_toggle"],
                [tuple(i) for i in raw["injected"]],
                raw["events"],
                raw["fake_events"],
            )
            fresh.append({**redone, "raw": raw})
        data["trials"][label] = fresh
        summaries.append(summarize(label, fresh, data["references"][label]))
    print_summary(summaries)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot", help="copy the user's dirs into .perf/profile")
    run_parser = sub.add_parser("run", help="run the typing trials")
    run_parser.add_argument("--tree", action="append", default=[], help="label=path of a source tree (repeatable)")
    run_parser.add_argument("--words", default="firefox")
    run_parser.add_argument("--intervals", default="20,30,50,70", help="ms between keys")
    run_parser.add_argument("--delays", default="0,50,100", help="ms from toggle to the first key")
    run_parser.add_argument("--rounds", type=int, default=3)
    run_parser.add_argument("--warmup", type=int, default=2)
    run_parser.add_argument("--refresh", type=int, default=100, help="virtual monitor refresh rate")
    run_parser.add_argument("--keep", action="store_true", help="keep the session dir with all logs")
    run_parser.add_argument("-v", "--verbose", action="store_true")
    reanalyze_parser = sub.add_parser("analyze", help="recompute the summary of a results file")
    reanalyze_parser.add_argument("results", type=Path)
    args = parser.parse_args()
    if args.command == "snapshot":
        snapshot()
    elif args.command == "analyze":
        reanalyze(args.results)
    else:
        if not args.tree:
            args.tree = [f"current={REPO}"]
        run(args)


if __name__ == "__main__":
    main()
