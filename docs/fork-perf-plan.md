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

## Results on 2026-11-17

Steps 0 to 8 are done except step 4. Each change is one commit on main with its harness numbers in
the message, and the progress ledger in `.superpowers/sdd/fork-perf-plan/progress.md` (git-ignored)
has every ruling. Nothing is pushed and the live launcher is still the PPA build.

### Harness

`scripts/perf/harness.py` runs Ulauncher from source trees or git revisions in a private headless
gnome-shell and types into it through Mutter RemoteDesktop. Snapshot the user's dirs once, then
compare trees:

```sh
env -i HOME=$HOME PATH=/usr/bin:/bin LANG=C.UTF-8 python3 scripts/perf/harness.py snapshot
env -i HOME=$HOME PATH=/usr/bin:/bin LANG=C.UTF-8 python3 scripts/perf/harness.py run \
    --tree base=git:38cacd1b --tree current=.
```

`--words a,b,c` types other words, `label=path+nofullscreen` sizes the window to its content and
`label=path+profile` writes cProfile data per query. Results land in `.perf/results/`, and
`harness.py analyze <file>` recomputes a summary. Medians move by a few ms between runs, so only
compare trees within one run. `scripts/perf/check_scoring.py` checks that fuzzy scores match a git
revision.

### Base against current

48 trials per tree, median / p90 / max in ms, base 38cacd1b against c0607e0e (before the garbage
collection change, which only moves full collections out of typing).

| Measure | base, "firefox" | current, "firefox" | base, eight words | current, eight words |
| --- | --- | --- | --- | --- |
| Toggle to focus | 46.1 / 55.8 / 71 | 30.1 / 39.9 / 43.2 | 46.6 / 57.5 / 62.3 | 23.6 / 39.9 / 49.9 |
| Key handler | 15.5 / 33.5 / 50.6 | 0.3 / 0.5 / 1 | 14.3 / 31.2 / 50.6 | 0.3 / 0.8 / 2 |
| Key to screen | 41.3 / 108.7 / 189.4 | 13.1 / 23.9 / 53.7 | 35.5 / 69.3 / 165 | 13.2 / 17.6 / 41.8 |
| Query | 15 / 33.2 / 50.4 | 6 / 11.5 / 27.5 | 13.9 / 30.8 / 50 | 5.8 / 9.3 / 16.3 |
| Enter to window hidden | 220.9 / 238.8 / 287.6 | 149.2 / 163.5 / 170.2 | 113.1 / 235.8 / 248 | 76.9 / 162.9 / 171 |

No wrong launches. Lost keys are the same in both and all were typed before the window had focus.

### What each step found

- 1, 2: rebuilding the window cost about 6 ms; the larger cost was the forced trigger reload after
  the first frame, which the focus-in event waited behind.
- 3: the query moved out of the key handler into an idle callback; keys that act on results flush it.
- 4: skipped. No Enter arrived with a query pending at 10 to 70 ms per key.
- 5: rendering, not scoring, was most of a query. Icon cache 512, visible rows first, reused rows.
- 6: one SequenceMatcher per text. Identical on 1,994,792 pairs, cold search 8.33 -> 6.99 ms in
  process, no change end to end.
- 7: kept the full-monitor surface. It costs nothing while typing and about 5 ms per open.
- 8: launches read the cgroup instead of running systemctl (7 ms each), and the window closes when
  the launched app takes focus instead of 70 ms later.
- After review: focus goes back to the entry on every open, close clears results and collects
  garbage, keywords survive conflicts, and settings, compositing and scaling changes rebuild.

### Left open

- The settings and theme check costs about 1.25 ms per open before the window shows.
- Not measured: the live GNOME session with its GPU compositor and the real hotkey, X11 and
  XWayland, layer shell, and the non-persistent mode in daily use.

### Upstream PRs

Decided on 2026-11-17: the owner daily drives the fork first, then sends the work upstream as a
series of small PRs, one change each, stacked in commit order, each with its own harness numbers.
The harness stays in the fork; the PR text describes the method and shows the numbers. Fork-only
commits stay out: this plan, the harness and its lint config, and the `.perf` ignore. Upstream
asks that the author owns and has verified every line (friday on #1549), and UI behaviour changes
also need gornostal (#1656). Upstream main had not moved past the fork's base on 2026-11-17.
