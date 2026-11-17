from __future__ import annotations

import logging
import os
import sys

from ulauncher.gi import GLib
from ulauncher.utils.lru_cache import lru_cache

logger = logging.getLogger(__name__)


def cgroup_in_unit(cgroups: str, unit: str) -> bool:
    """Whether /proc/<pid>/cgroup content places the process in this systemd unit."""
    return any(unit in line.rpartition(":")[2].split("/") for line in cgroups.splitlines())


@lru_cache(maxsize=None)
def _runs_in_ulauncher_unit() -> bool:
    # Launched apps must leave the unit's cgroup, or stopping the unit kills them too. Asking
    # systemctl whether the unit is active cost a blocking 7 ms subprocess on every launch, and a
    # process stays in its cgroup for life, so read it once.
    try:
        with open("/proc/self/cgroup") as cgroup_file:
            return cgroup_in_unit(cgroup_file.read(), "ulauncher.service")
    except OSError:
        return False


def detach_child(_data: object | None = None) -> None:
    """
    A utility function which runs in the child process launched by spawn_async
    and before execing the supplied command.

    GLib calls its `SpawnChildSetupFunc(data)` with the spawn_async user_data
    (None here). Newer PyGObject passes it, older versions call with no
    arguments, so keep the parameter optional.
    """
    # Use setsid to take "session leader" status so that the child pid is
    # definitely detached from the parent ulauncher process
    os.setsid()

    # Don't redirect the standard file descriptors unless connected to a terminal.
    if not sys.stdout.isatty():
        return

    # Reopen the stdin, stdout, and stderr file descriptors to /dev/null. This
    # ensures the stdout/stderr are no longer connected to the terminal. This
    # serves a similar purpose as the standard "nohup" command. Any processes
    # connected to the terminal will get the interrupt signal when "Ctrl-C" is
    # called, so this redirection prevents the child process from exiting when
    # ulauncher is interrupted. Unlike "nohup", stdout and stderr are not sent
    # to a file but sent to /dev/null instead.
    with open("/dev/null", "w+b") as null_fp:
        null_fd = null_fp.fileno()
        for fp in [sys.stdin, sys.stdout, sys.stderr]:
            orig_fd = fp.fileno()
            fp.close()
            os.dup2(null_fd, orig_fd)


def launch_detached(cmd: list[str], working_dir: str | None = None, *, startup_notify_id: str | None) -> None:
    use_systemd_run = _runs_in_ulauncher_unit()
    if use_systemd_run:
        cmd = ["systemd-run", "--user", "--scope", *cmd]

    env = dict(os.environ.items())
    # Make sure GDK apps aren't forced to use x11 on wayland due to ulauncher's need to run
    # under X11 for proper centering.
    if env.get("GDK_BACKEND") != "wayland":
        env.pop("GDK_BACKEND", None)
    # Same as GLib sets: X11 apps read the first, Wayland apps the second
    if startup_notify_id:
        env["DESKTOP_STARTUP_ID"] = startup_notify_id
        env["XDG_ACTIVATION_TOKEN"] = startup_notify_id

    try:
        envp = [f"{k}={v}" for k, v in env.items()]
        GLib.spawn_async(
            argv=cmd,
            envp=envp,
            flags=GLib.SpawnFlags.SEARCH_PATH_FROM_ENVP | GLib.SpawnFlags.SEARCH_PATH,
            child_setup=None if use_systemd_run else detach_child,
            # the python gi wrapper has a bug, not actually allowing to pass working_directory as None
            **({"working_directory": working_dir} if working_dir else {}),
        )
    except (TypeError, GLib.Error):
        logger.exception('Could not launch "%s"', cmd)


def open_detached(path_or_url: str) -> None:
    launch_detached(["xdg-open", path_or_url], startup_notify_id=None)
