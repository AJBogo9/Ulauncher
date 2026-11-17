# Fork performance plan

This fork (AJBogo9/Ulauncher) exists to make Ulauncher as fast as possible for one workflow:
open the launcher with the hotkey, type an app name fast without looking, press Enter. Upstream
PRs are not a goal for now. Written 2026-11-17 as a handoff from the investigating session.

## Requirements

1. Enter launches the result for the whole typed text, even when Enter arrives before any result
   has been drawn. Today Enter does nothing until results exist (`ulauncher_window.py:307`).
2. No keystroke is lost once the window has focus, and hotkey to focused window is as short as
   possible. Keys typed before the window has focus go to the previous app, so open speed counts.
3. A keystroke never blocks the main loop long enough to queue the next one at 30 ms per key.
   Target: under one frame (10 ms on the 100 Hz monitor) for the work done inside the key handler.
4. Results do not reshuffle under the user beyond what the new query implies.
5. Everything else (themes, extensions, preferences) behaves as on upstream main.

## State on 2026-11-17

- Fork on GitHub, clone here, `upstream` remote = Ulauncher/Ulauncher, base `d235f0be`
  (v6.0.0-beta35 plus 38 commits). Nothing pushed yet. Commit email is the noreply address.
- `make venv` done. Tests: `env -i HOME=$HOME PATH=$PWD/.venv/bin:/usr/bin:/bin LANG=C.UTF-8
  DISPLAY=:97 GDK_BACKEND=x11 .venv/bin/pytest -p no:cacheprovider -q tests` gives 535 passed.
  Start the private X server first: `nohup setsid ~/.local/bin/Xvfb :97 -screen 0 1920x1080x24
  -nolisten tcp &`.
- The live launcher is still the PPA package 6.0.0~beta35 (`ulauncher.service`, systemd user unit).
- Rough scripts from the investigation are in `.perf/` (untracked, listed in `.git/info/exclude`):
  `bench_ulauncher.py` (offscreen search plus render timing) and `probe_live.py` (D-Bus typing probe).
- Machine: i7-11850H, GNOME Shell 46 on Wayland, two 1920x1080 monitors at scale 1 (eDP-1 60 Hz,
  HDMI-1 100 Hz), 104 apps giving 1018 searchable fields.

## Measured on the live launcher (PPA beta35)

Six words typed over D-Bus (`org.gtk.Actions.Activate("set-query")` takes the same path as a
keystroke) at 70 ms per key, with a second thread timing `org.gtk.Actions.DescribeAll` every 2 ms
to see main-loop stalls (idle round trip 0.6 ms).

| ms per keystroke | median | p90 | max |
| --- | --- | --- | --- |
| live window | 44 | 121 | 137 |
| same code offscreen (400 px results area) | 14 | | 76 |
| fuzzy scoring alone | 8 | | 16 |

20 of 60 keys took longer than the key gap. The worst queries are 2 to 4 letters long, where up
to 36 rows are built. An earlier session (2026-09-30, offscreen) found: the icon cache of 50
thrashes (537 misses in 192 keystrokes, about 2.4 ms each; 512 removed them), and reusing one
`SequenceMatcher` per text cut search from 5.1 to 4.0 ms with identical output on 99,360 pairs.

## Where the time goes (upstream main)

- `ulauncher_window.py:181`: the entry's `changed` signal runs `query_changed` synchronously, so
  search, row rebuild and layout all happen inside the key event. Nothing coalesces fast keys.
- `ulauncher_window.py:442-454`: closing destroys the window; `app.py:196` builds a new one on
  every open (widget tree, theme CSS, positioning). It is shown at opacity 0 first and styled in
  `deferred_init`. Then `app.py:85-87` (`window_ready`) reloads every trigger with `force=True`
  (about 9 ms, 6 ms of it `Gio.DesktopAppInfo.get_all`). The destroy dates back at least to
  `87632817` (2024); find out why before replacing it with hide and show.
- `results_view.py:82-84`: every query destroys all rows and builds new ones; the row cap is the
  length of `jump_keys` (`results_view.py:165`), 36 by default, while about 14 rows fit on screen.
- `load_icon_surface.py:18`: icon cache of 50.
- `fuzzy_search.py:60`: a new `SequenceMatcher` per (query, text) pair.
- On GNOME Wayland the window is a full-monitor transparent surface with the launcher inside it
  (`_use_fullscreen_to_position`, `ulauncher_window.py:31`). Untested guess: this is why the live
  window costs about 3x the offscreen one (large RGBA buffers per frame). Measure it.
- After a launch the window stays up to 0.5 s waiting for the app to take focus
  (`_APP_FOCUS_TIMEOUT_SEC`, needed for Mutter activation tokens).
- Not a cost: the Ctrl+, hotkey runs `ulauncher toggle`, a shell script that calls `gapplication`
  without starting Python.

## Plan

Measure before and after every step, one commit per logical change, and keep upstream tests green.

0. **Harness.** End to end and headless, close to the live setup. Preferred: a private
   `dbus-run-session` running `gnome-shell --headless --wayland --no-x11 --virtual-monitor
   1920x1080`, the fork inside it with copied XDG config and state dirs, keys injected through
   `org.gnome.Mutter.RemoteDesktop` (`CreateSession`, `Start`, `NotifyKeyboardKeysym`) on that
   private bus, and `launch_app` stubbed by an env-gated hook that records the activation instead
   of launching. Probes hook GTK signals found from `Gtk.Window.list_toplevels()` (entry
   `changed`, key press, frame clock `after-paint`) so they survive refactors. Scenario: toggle,
   then type "firefox" at 20 to 70 ms per key starting 0 to 100 ms after the toggle, then Enter.
   Report: toggle to focused window, per-key handler time, Enter to activation, which app was
   activated, lost characters. Fallback if Mutter injection fails: Xvfb plus python-xlib XTEST.
1. **Keep the window alive.** Build it once at startup, hide on close, show on open. Keep the
   focus-out, activation-token and positioning behaviour.
2. **Stop reloading triggers on every open.** Refresh apps on `Gio.AppInfoMonitor` "changed".
3. **Coalesce keystrokes.** Run the query from an idle callback (`scheduling.run_when_idle`
   returns a cancellable context), replacing any pending one. Enter, arrows, Tab and Alt+jump
   first flush the pending query. Write the failing test for "Enter before results" first.
4. **Enter fast path.** With a query pending, score and activate the top result without drawing.
5. **Rendering.** Reuse row widgets instead of destroying them, icon cache 512, build the visible
   rows first and the rest when idle.
6. **Scoring.** One `SequenceMatcher` per text with `set_seq1(query)`, precomputed normalized
   fields. Must keep scores identical (check on a large set of pairs, with a planted control).
7. **Full-monitor surface.** Measure its cost in the harness; try opaque and input regions or a
   smaller window if it matters.
8. **Launch.** Measure Enter to window gone and Enter to app focused; shorten what is safe.

Use a Fable agent (`model: fable`) with a self-contained brief when a step stalls, starting with
the harness if Mutter injection or the headless shell fights back.

## Rules

- Never inject input into the live desktop. No uinput (its devices reach the real session) and no
  test windows on the real screen: GUI tests run on Xvfb :97 or in the headless shell, with
  `WAYLAND_DISPLAY` unset where needed.
- Run Python from the Bash tool under `env -i` (the VS Code snap environment breaks python3), and
  heavy runs under `systemd-run --user --scope -p MemoryMax=8G`.
- No push without an explicit grant from the owner. No Claude attribution in commits.
- Switch the live launcher only after the harness shows the fork is correct and faster: add a
  systemd drop-in `~/.config/systemd/user/ulauncher.service.d/fork.conf` with an empty
  `ExecStart=` line and then `ExecStart=<clone>/bin/ulauncher start`, run `systemctl --user
  daemon-reload` and restart the unit. Roll back by deleting the drop-in. Tell the owner first.
