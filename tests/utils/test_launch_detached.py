from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from ulauncher.utils import launch_detached as launch_detached_module
from ulauncher.utils.launch_detached import cgroup_in_unit, launch_detached


@pytest.mark.parametrize(
    ("cgroups", "expected"),
    [
        ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/ulauncher.service\n", True),
        ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-ulauncher-7.scope\n", False),
        ("0::/user.slice/user-1000.slice/session-2.scope\n", False),
        ("12:pids:/user.slice\n1:name=systemd:/user.slice/user@1000.service/ulauncher.service\n", True),
        ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/ulauncher.service.d\n", False),
        ("", False),
    ],
)
def test_cgroup_in_unit(cgroups: str, expected: bool) -> None:
    assert cgroup_in_unit(cgroups, "ulauncher.service") is expected


@pytest.fixture
def spawn_async(mocker: MockerFixture) -> MagicMock:
    return mocker.patch("ulauncher.utils.launch_detached.GLib.spawn_async")


@pytest.mark.parametrize(("in_unit", "argv"), [(True, ["systemd-run", "--user", "--scope", "app"]), (False, ["app"])])
def test_launch_detached__leaves_the_unit_without_asking_systemctl(
    mocker: MockerFixture, spawn_async: MagicMock, in_unit: bool, argv: list[str]
) -> None:
    mocker.patch.object(launch_detached_module, "_runs_in_ulauncher_unit", return_value=in_unit)
    systemctl = mocker.patch("ulauncher.utils.systemd_controller.subprocess.run")

    launch_detached(["app"], startup_notify_id=None)

    assert spawn_async.call_args.kwargs["argv"] == argv
    systemctl.assert_not_called()
