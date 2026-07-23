# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright (C) 2020 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from craft_parts import Part, PartInfo, ProjectInfo
from pydantic import ValidationError

import snapcraft.parts.plugins as colcon
from snapcraft.parts.plugins import _ros


def test_rosdep_error():
    e = _ros.RosdepUnexpectedResultError(dependency="foo", output="foo doesn't exists")
    assert (
        str(e) == "Received unexpected result from rosdep when "
        "trying to resolve 'foo':\nfoo doesn't exists"
    )


def test_parse_rosdep_resolve_dependencies():
    # @todo
    assert True


def test_get_debian_package_names(monkeypatch):
    def _run(cmd, check, capture_output, env):
        assert cmd == ["rosdep", "resolve", "foo_bar", "--rosdistro", "humble"]
        assert check is True
        assert capture_output is True
        return SimpleNamespace(stdout=b"#apt\nfoo-bar\nfoo\n")

    monkeypatch.setattr(_ros.subprocess, "run", _run)

    assert _ros._get_debian_package_names("foo_bar", "humble") == {
        "foo-bar",
        "foo",
    }


def test_find_installed_debian_dependencies(monkeypatch):
    def _resolve(package, ros_distro):
        assert ros_distro == "humble"
        if package == "ros_pkg_a":
            return {"ros-pkg-a", "libfoo"}
        if package == "ros_pkg_b":
            return {"ros-pkg-b"}
        return set()

    apt_output = {
        "libfoo": b"libfoo-dep\nshared-dep\n",
        "ros-pkg-a": b"libbar\nshared-dep\n",
        "ros-pkg-b": b"",
    }

    def _run(cmd, check, stdout, stderr, env):
        assert cmd[:2] == ["apt", "depends"]
        assert check is True
        assert stdout == subprocess.PIPE
        assert stderr == subprocess.STDOUT
        return SimpleNamespace(stdout=apt_output[cmd[-1]])

    monkeypatch.setattr(_ros, "_get_debian_package_names", _resolve)
    monkeypatch.setattr(_ros.subprocess, "run", _run)

    assert _ros._find_installed_debian_dependencies(
        {"ros_pkg_a", "ros_pkg_b"}, "humble"
    ) == {
        "libfoo",
        "libbar",
        "ros-pkg-a",
        "ros-pkg-b",
        "libfoo-dep",
        "shared-dep",
    }


def test_stage_runtime_dependencies_skips_build_snap_packages(monkeypatch, tmp_path):
    part_src = tmp_path / "src"
    part_src.mkdir()
    part_install = tmp_path / "install"
    part_install.mkdir()

    dep = SimpleNamespace(name="needs-resolve", evaluated_condition=True)
    source_pkg = SimpleNamespace(
        exec_depends=[dep], evaluate_conditions=lambda _conditions: None
    )
    fetch_calls = {}

    def _run(cmd, check, capture_output, env):
        return SimpleNamespace(stdout=b"#apt\nresolved-apt-package\n")

    def _find_installed(build_snap_packages, ros_distro):
        return {"already-provided-debian"}

    def _fetch_stage_packages(**kwargs):
        fetch_calls.update(kwargs)
        return ["resolved-apt-package"]

    monkeypatch.setattr(
        _ros,
        "_get_installed_dependencies",
        lambda _: {"provided-by-build-snap"},
    )
    find_packages_mock = Mock(side_effect=[{}, {"pkg": source_pkg}])
    monkeypatch.setattr(_ros.catkin_packages, "find_packages", find_packages_mock)
    monkeypatch.setattr(_ros.subprocess, "run", _run)
    monkeypatch.setattr(_ros, "_find_installed_debian_dependencies", _find_installed)
    monkeypatch.setattr(_ros.Repo, "configure", lambda _name: None)
    monkeypatch.setattr(_ros.Repo, "fetch_stage_packages", _fetch_stage_packages)
    monkeypatch.setattr(_ros.Repo, "unpack_stage_packages", lambda **_kwargs: None)

    callback = _ros.stage_runtime_dependencies.callback
    assert callback is not None
    callback(
        part_src=str(part_src),
        part_install=str(part_install),
        ros_version="2",
        ros_distro="humble",
        target_arch="amd64",
        stage_cache_dir=str(tmp_path),
        base="core22",
    )

    find_packages_mock.assert_any_call(str(part_install))
    find_packages_mock.assert_any_call(str(part_src))
    assert fetch_calls["packages_filters"] == {"already-provided-debian"}


@pytest.fixture
def setup_method_fixture():
    def _setup_method_fixture(base, new_dir, properties=None):
        if properties is None:
            properties = {}
        properties["source"] = "."
        plugin_properties = colcon.ColconPlugin.properties_class.unmarshal(properties)
        part = Part("foo", {})

        project_info = ProjectInfo(
            application_name="test", base=base, cache_dir=new_dir
        )
        project_info._parallel_build_count = 42

        part_info = PartInfo(project_info=project_info, part=part)
        part_info._part_install_dir = Path("install/dir")

        return colcon.ColconPlugin(properties=plugin_properties, part_info=part_info)

    yield _setup_method_fixture


def _source_ros_ws(path: str) -> list[str]:
    """Build the commands sourcing a ROS 2 workspace at ``path``."""
    return [
        f'if [ -n "${{ROS_DISTRO:-}}" ] && [ -f "{path}/opt/ros/${{ROS_DISTRO:-}}/local_setup.sh" ]; then',
        f'AMENT_CURRENT_PREFIX="{path}/opt/ros/${{ROS_DISTRO:-}}" . "{path}/opt/ros/${{ROS_DISTRO:-}}/local_setup.sh"',
        "fi",
        f'if [ -f "{path}/opt/ros/snap/local_setup.sh" ]; then',
        f'COLCON_CURRENT_PREFIX="{path}/opt/ros/snap" . "{path}/opt/ros/snap/local_setup.sh"',
        "fi",
    ]


def _activation_commands(build_snaps: tuple[str, ...] = ()) -> list[str]:
    commands = ["## Sourcing ROS ws in build snaps"]
    for snap in build_snaps:
        commands.extend(_source_ros_ws(f"/snap/{snap}/current"))
    if build_snaps:
        commands.append("")

    commands.append("##[craft-parts.colcon] Sourcing colcon ws in stage snaps")
    commands.extend(_source_ros_ws("${CRAFT_PART_INSTALL}"))
    commands.append("")

    commands.append("##[craft-parts.colcon] Sourcing the colcon workspace")
    commands.extend(_source_ros_ws(""))
    commands.append("")

    return commands


def _list_packages_commands(build_snaps: tuple[str, ...] = ()) -> list[str]:
    commands = [
        'rm -f "${CRAFT_PART_INSTALL}/.installed_packages.txt"',
        'rm -f "${CRAFT_PART_INSTALL}/.build_snaps.txt"',
    ]
    for snap in build_snaps:
        base = f"/snap/{snap}/current/opt/ros"
        sys_path = f"{base}/${{ROS_DISTRO}}/"
        app_path = f"{base}/snap/"
        commands.extend(
            [
                f"if [ -d {base} ]; then",
                f"AMENT_PREFIX_PATH={sys_path}:{app_path} "
                'ros2 pkg list >> "${CRAFT_PART_INSTALL}/.installed_packages.txt"',
                "fi",
                f'if [ -d "{sys_path}" ]; then',
                f'rosdep keys --rosdistro "${{ROS_DISTRO}}" --from-paths "{sys_path}" --ignore-packages-from-source '
                '>> "${CRAFT_PART_INSTALL}/.installed_packages.txt"',
                "fi",
                f'if [ -d "{app_path}" ]; then',
                f'rosdep keys --rosdistro "${{ROS_DISTRO}}" --from-paths "{app_path}" --ignore-packages-from-source '
                '>> "${CRAFT_PART_INSTALL}/.installed_packages.txt"',
                "fi",
            ]
        )
    if build_snaps:
        commands.append("")
    return commands


def _stage_runtime_command(env_prefix: str, cache_dir, base: str) -> str:
    return (
        f"env -i LANG=C.UTF-8 LC_ALL=C.UTF-8 {env_prefix}/test/python3 -I "
        "/test/_ros.py "
        'stage-runtime-dependencies --part-src "${CRAFT_PART_SRC_WORK}" '
        '--part-install "${CRAFT_PART_INSTALL}" '
        '--ros-version "${ROS_VERSION}" --ros-distro "${ROS_DISTRO}" '
        '--target-arch "${CRAFT_TARGET_ARCH}" '
        f"--stage-cache-dir {cache_dir} --base {base}"
    )


def _expected_build_commands(
    *,
    base: str,
    cache_dir,
    build_line: str,
    build_snaps: tuple[str, ...] = (),
    env_prefix: str = "",
) -> list[str]:
    return (
        [
            "if [ ! -f /etc/ros/rosdep/sources.list.d/20-default.list ]; then",
            "sudo --preserve-env=http_proxy,https_proxy rosdep init; fi",
            'rosdep update --include-eol-distros --rosdistro "${ROS_DISTRO}"',
            'state="$(set +o); set -$-"',
            "set +u",
            "",
        ]
        + _activation_commands(build_snaps)
        + ['eval "${state}"']
        + _list_packages_commands(build_snaps)
        + [
            'rosdep install --default-yes --ignore-packages-from-source --from-paths "${CRAFT_PART_SRC_WORK}"',
            'state="$(set +o); set -$-"',
            "set +u",
            "",
        ]
        + _activation_commands(build_snaps)
        + ['eval "${state}"']
        + [
            "## Build command",
            build_line,
            "## Post build command",
            'if [ -f "${CRAFT_PART_INSTALL}"/opt/ros/snap/COLCON_IGNORE ]; then',
            'rm "${CRAFT_PART_INSTALL}"/opt/ros/snap/COLCON_IGNORE',
            "fi",
        ]
        + [_stage_runtime_command(env_prefix, cache_dir, base)]
    )


_FULL_ENV_PREFIX = (
    "PATH=/bin:/test SNAP=TESTSNAP SNAP_ARCH=TESTARCH SNAP_NAME=TESTSNAPNAME "
    "SNAP_VERSION=TESTV1 http_proxy=http://foo https_proxy=https://bar "
)

_FULL_ENV = {
    "FOO": "baR",
    "PATH": "/bin:/test",
    "SNAP": "TESTSNAP",
    "SNAP_ARCH": "TESTARCH",
    "SNAP_NAME": "TESTSNAPNAME",
    "SNAP_VERSION": "TESTV1",
    "http_proxy": "http://foo",
    "https_proxy": "https://bar",
}


class TestPluginColconPlugin:
    """Colcon plugin tests."""

    def test_property_empty_invalid(self):
        with pytest.raises(ValidationError):
            colcon.ColconPlugin.properties_class.unmarshal({})

    def test_property_default(self):
        try:
            colcon.ColconPlugin.properties_class.unmarshal({"source": "."})
        except ValidationError as e:
            raise AssertionError(f"{e}") from e

    def test_property_all(self):
        try:
            properties = colcon.ColconPlugin.properties_class.unmarshal(
                {
                    "source": ".",
                    "colcon-ament-cmake-args": ["ament", "args..."],
                    "colcon-catkin-cmake-args": ["catkin", "args..."],
                    "colcon-cmake-args": ["cmake", "args..."],
                    "colcon-packages": ["package1", "package2..."],
                    "colcon-packages-ignore": ["ipackage1", "ipackage2..."],
                    "colcon-ros-build-snaps": ["ros-core"],
                }
            )
        except ValidationError as e:
            raise AssertionError(f"{e}") from e

        assert properties.source == "."
        assert properties.colcon_ament_cmake_args == ["ament", "args..."]
        assert properties.colcon_catkin_cmake_args == ["catkin", "args..."]
        assert properties.colcon_cmake_args == ["cmake", "args..."]
        assert properties.colcon_packages == ["package1", "package2..."]
        assert properties.colcon_packages_ignore == ["ipackage1", "ipackage2..."]
        assert properties.colcon_ros_build_snaps == ["ros-core"]

    def test_get_build_packages_core22(self, setup_method_fixture, new_dir):
        plugin = setup_method_fixture("core22", new_dir)

        assert plugin.get_build_packages() == {
            "cmake",
            "colcon",
            "g++",
            "gcc",
            "python3-colcon-cmake",
            "python3-colcon-common-extensions",
            "python3-colcon-core",
            "python3-colcon-package-selection",
            "python3-colcon-parallel-executor",
            "python3-colcon-python-setup-py",
            "python3-rosinstall",
            "python3-wstool",
            "python3-rosdep",
            "ros-humble-ros2pkg",
        }

    def test_get_build_packages_core24(self, setup_method_fixture, new_dir):
        plugin = setup_method_fixture("core24", new_dir)

        assert plugin.get_build_packages() == {
            "cmake",
            "colcon",
            "g++",
            "gcc",
            "python3-colcon-cmake",
            "python3-colcon-common-extensions",
            "python3-colcon-core",
            "python3-colcon-package-selection",
            "python3-colcon-parallel-executor",
            "python3-colcon-python-setup-py",
            "python3-rosdep",
            "ros-jazzy-ros2pkg",
        }

    def test_get_build_packages_core26(self, setup_method_fixture, new_dir):
        plugin = setup_method_fixture("core26", new_dir)

        assert plugin.get_build_packages() == {
            "cmake",
            "colcon",
            "g++",
            "gcc",
            "python3-colcon-cmake",
            "python3-colcon-common-extensions",
            "python3-colcon-core",
            "python3-colcon-package-selection",
            "python3-colcon-parallel-executor",
            "python3-colcon-python-setup-py",
            "python3-rosdep",
            "ros-lyrical-ros2pkg",
        }

    def test_get_build_snaps(self, setup_method_fixture, new_dir):
        plugin = setup_method_fixture("core22", new_dir)

        assert plugin.get_build_snaps() == set()

        plugin = setup_method_fixture(
            "core22",
            new_dir,
            properties={
                "source": ".",
                "colcon-ament-cmake-args": [],
                "colcon-catkin-cmake-args": [],
                "colcon-cmake-args": [],
                "colcon-packages": [],
                "colcon-packages-ignore": [],
                "colcon-ros-build-snaps": ["Foo"],
            },
        )

        assert plugin.get_build_snaps() == {"Foo"}

    def test_get_build_environment(self, setup_method_fixture, new_dir):
        plugin = setup_method_fixture("core22", new_dir)

        assert plugin.get_build_environment() == {
            "AMENT_PYTHON_EXECUTABLE": "/usr/bin/python3",
            "COLCON_PYTHON_EXECUTABLE": "/usr/bin/python3",
            "ROS_PYTHON_VERSION": "3",
        }

    def test_out_of_source_build_property(self):
        assert colcon.ColconPlugin.get_out_of_source_build()

    def _base_build_line(self, plugin) -> str:
        return (
            "colcon build "
            '--base-paths "${CRAFT_PART_SRC_WORK}" '
            '--build-base "${CRAFT_PART_BUILD}" '
            '--merge-install --install-base "${CRAFT_PART_INSTALL}/opt/ros/snap" '
            "--cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF "
            '--parallel-workers "${CRAFT_PARALLEL_BUILD_COUNT}"'
        )

    def _full_build_line(self, plugin, *, build_type: str = "Release") -> str:
        return (
            "colcon build "
            '--base-paths "${CRAFT_PART_SRC_WORK}" '
            '--build-base "${CRAFT_PART_BUILD}" '
            '--merge-install --install-base "${CRAFT_PART_INSTALL}/opt/ros/snap" '
            "--packages-ignore ipackage1 ipackage2... --packages-select package1 "
            f"package2... --cmake-args -DCMAKE_BUILD_TYPE={build_type} cmake args... "
            "-DBUILD_TESTING=OFF "
            "--ament-cmake-args ament args... --catkin-cmake-args catkin args... "
            '--parallel-workers "${CRAFT_PARALLEL_BUILD_COUNT}"'
        )

    @pytest.mark.parametrize("base", ["core22", "core24", "core26"])
    def test_get_build_commands(self, setup_method_fixture, new_dir, monkeypatch, base):
        plugin = setup_method_fixture(base, new_dir)

        monkeypatch.setattr(sys, "path", ["", "/test"])
        monkeypatch.setattr(sys, "executable", "/test/python3")
        monkeypatch.setattr(_ros, "__file__", "/test/_ros.py")
        monkeypatch.setattr(os, "environ", {})

        assert plugin.get_build_commands() == _expected_build_commands(
            base=base,
            cache_dir=new_dir,
            build_line=self._base_build_line(plugin),
        )

    @pytest.mark.parametrize("base", ["core22", "core24", "core26"])
    def test_get_build_commands_with_all_properties(
        self, setup_method_fixture, new_dir, monkeypatch, base
    ):
        plugin = setup_method_fixture(
            base,
            new_dir,
            properties={
                "source": ".",
                "colcon-ament-cmake-args": ["ament", "args..."],
                "colcon-catkin-cmake-args": ["catkin", "args..."],
                "colcon-cmake-args": ["cmake", "args..."],
                "colcon-packages": ["package1", "package2..."],
                "colcon-packages-ignore": ["ipackage1", "ipackage2..."],
                "colcon-ros-build-snaps": ["foo"],
            },
        )

        monkeypatch.setattr(sys, "path", ["", "/test"])
        monkeypatch.setattr(sys, "executable", "/test/python3")
        monkeypatch.setattr(_ros, "__file__", "/test/_ros.py")
        monkeypatch.setattr(os, "environ", _FULL_ENV)

        assert plugin.get_build_commands() == _expected_build_commands(
            base=base,
            cache_dir=new_dir,
            build_line=self._full_build_line(plugin),
            build_snaps=("foo",),
            env_prefix=_FULL_ENV_PREFIX,
        )

    def test_get_build_commands_with_cmake_debug(
        self, setup_method_fixture, new_dir, monkeypatch
    ):
        plugin = setup_method_fixture(
            "core22",
            new_dir,
            properties={
                "source": ".",
                "colcon-ament-cmake-args": ["ament", "args..."],
                "colcon-catkin-cmake-args": ["catkin", "args..."],
                "colcon-cmake-args": ["-DCMAKE_BUILD_TYPE=Debug", "args..."],
                "colcon-packages": ["package1", "package2..."],
                "colcon-packages-ignore": ["ipackage1", "ipackage2..."],
                "colcon-ros-build-snaps": ["foo"],
            },
        )

        monkeypatch.setattr(sys, "path", ["", "/test"])
        monkeypatch.setattr(sys, "executable", "/test/python3")
        monkeypatch.setattr(_ros, "__file__", "/test/_ros.py")
        monkeypatch.setattr(os, "environ", _FULL_ENV)

        build_line = (
            "colcon build "
            '--base-paths "${CRAFT_PART_SRC_WORK}" '
            '--build-base "${CRAFT_PART_BUILD}" '
            '--merge-install --install-base "${CRAFT_PART_INSTALL}/opt/ros/snap" '
            "--packages-ignore ipackage1 ipackage2... --packages-select package1 "
            "package2... --cmake-args -DCMAKE_BUILD_TYPE=Debug args... "
            "-DBUILD_TESTING=OFF "
            "--ament-cmake-args ament args... --catkin-cmake-args catkin args... "
            '--parallel-workers "${CRAFT_PARALLEL_BUILD_COUNT}"'
        )

        assert plugin.get_build_commands() == _expected_build_commands(
            base="core22",
            cache_dir=new_dir,
            build_line=build_line,
            build_snaps=("foo",),
            env_prefix=_FULL_ENV_PREFIX,
        )
