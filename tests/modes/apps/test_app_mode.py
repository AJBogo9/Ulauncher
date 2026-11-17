from __future__ import annotations

from types import SimpleNamespace

from pytest_mock import MockerFixture

from ulauncher.gi import Gio
from ulauncher.modes.apps.app_mode import AppMode


def test_has_trigger_changes__after_desktop_entries_or_filter_settings_change(mocker: MockerFixture) -> None:
    mocker.patch("ulauncher.modes.apps.app_mode.GioUnix.DesktopAppInfo.get_all", return_value=[])
    settings = SimpleNamespace(enable_application_mode=True, disable_desktop_filters=False)
    mocker.patch("ulauncher.modes.apps.app_mode.Settings.load", return_value=settings)
    mode = AppMode()
    assert mode.has_trigger_changes()

    list(mode.get_triggers())
    assert not mode.has_trigger_changes()

    Gio.AppInfoMonitor.get().emit("changed")
    assert mode.has_trigger_changes()

    list(mode.get_triggers())
    settings.disable_desktop_filters = True
    assert mode.has_trigger_changes()
