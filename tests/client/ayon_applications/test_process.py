"""Tests for process state checks used by the Process Monitor.

'psutil' is replaced by a fake module so the macOS behaviour can be
tested on any platform. 'process.py' is loaded directly from its file
with a stub 'ayon_core' so AYON does not have to be installed.

Run with: python -m pytest tests/client/ayon_applications
"""
from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

PROCESS_PY = (
    Path(__file__).resolve().parents[3]
    / "client" / "ayon_applications" / "process.py"
)

MAYA_EXE = "/Applications/Autodesk/maya2025/Maya.app/Contents/MacOS/Maya"
START_TIME = 1_700_000_000.25


# --- Fake psutil ---------------------------------------------------------
class FakePsutilError(Exception):
    pass


class FakeNoSuchProcess(FakePsutilError):
    pass


class FakeZombieProcess(FakeNoSuchProcess):
    pass


class FakeAccessDenied(FakePsutilError):
    pass


@dataclass
class FakeProc:
    """Values returned by the fake process.

    Any value that is an exception instance is raised instead.
    """
    exe: Any = ""
    name: Any = ""
    cmdline: Any = field(default_factory=list)
    create_time: Any = START_TIME
    status: Any = "running"


def make_fake_psutil(procs: dict[int, FakeProc]) -> types.ModuleType:
    module = types.ModuleType("psutil")
    module.Error = FakePsutilError
    module.NoSuchProcess = FakeNoSuchProcess
    module.ZombieProcess = FakeZombieProcess
    module.AccessDenied = FakeAccessDenied
    module.STATUS_RUNNING = "running"
    module.STATUS_ZOMBIE = "zombie"

    def _value(value):
        if isinstance(value, BaseException):
            raise value
        return value

    class Process:
        def __init__(self, pid: int) -> None:
            if pid not in procs:
                raise FakeNoSuchProcess(pid)
            self.pid = pid
            self._data = procs[pid]

        def exe(self):
            return _value(self._data.exe)

        def name(self):
            return _value(self._data.name)

        def cmdline(self):
            return _value(self._data.cmdline)

        def create_time(self):
            return _value(self._data.create_time)

        def status(self):
            return _value(self._data.status)

    module.Process = Process
    return module


# --- Fixtures ------------------------------------------------------------
@pytest.fixture
def process_module(monkeypatch, tmp_path):
    """Load 'process.py' with a stub 'ayon_core.lib'."""
    ayon_core = types.ModuleType("ayon_core")
    ayon_core_lib = types.ModuleType("ayon_core.lib")
    ayon_core_lib.get_launcher_local_dir = lambda *args: str(tmp_path)
    ayon_core.lib = ayon_core_lib
    monkeypatch.setitem(sys.modules, "ayon_core", ayon_core)
    monkeypatch.setitem(sys.modules, "ayon_core.lib", ayon_core_lib)

    name = "_ayon_applications_process_under_test"
    spec = importlib.util.spec_from_file_location(name, PROCESS_PY)
    module = importlib.util.module_from_spec(spec)
    # dataclasses need the module in 'sys.modules' while loading
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_system(monkeypatch, process_module):
    """Set the fake process table and platform name."""
    def _set(procs: dict[int, FakeProc], system: str = "Darwin") -> None:
        monkeypatch.setitem(sys.modules, "psutil", make_fake_psutil(procs))
        monkeypatch.setattr(
            process_module.platform, "system", lambda: system
        )
    return _set


def is_running(process_module, pid, executable, start_time):
    return process_module.ProcessManager._is_process_running(
        pid, executable, start_time
    )


# --- Tests: process identified by PID and start time ---------------------
def test_macos_open_launch_services_wrapper_is_running(
    process_module, fake_system
):
    """App launched with 'open -n -W' stores the PID of 'open'.

    'open -W' stays alive until the application quits, but its executable
    is '/usr/bin/open', not the application executable.
    """
    fake_system({
        100: FakeProc(
            exe="/usr/bin/open",
            name="open",
            cmdline=[
                "open", "-n", "-W", "-a",
                "/Applications/Autodesk/maya2025/Maya.app", "--args",
            ],
        ),
    })
    assert is_running(process_module, 100, MAYA_EXE, START_TIME)


def test_launcher_wrapper_is_running(process_module, fake_system):
    """Hosts like Photoshop are started through the AYON launcher."""
    fake_system({
        100: FakeProc(
            exe="/Applications/AYON 1.4.0.app/Contents/MacOS/ayon",
            name="ayon",
            cmdline=[
                "/Applications/AYON 1.4.0.app/Contents/MacOS/ayon",
                "run", "launch_script.py",
            ],
        ),
    })
    photoshop_exe = (
        "/Applications/Adobe Photoshop 2025/Adobe Photoshop 2025.app"
        "/Contents/MacOS/Adobe Photoshop 2025"
    )
    assert is_running(process_module, 100, photoshop_exe, START_TIME)


def test_wrapper_script_is_running(process_module, fake_system):
    """The kernel runs a '#!' script with the interpreter as executable."""
    fake_system({
        100: FakeProc(
            exe="/bin/bash",
            name="bash",
            cmdline=["/bin/bash", "/opt/tools/run_maya.sh"],
        ),
    })
    assert is_running(
        process_module, 100, "/opt/tools/run_maya.sh", START_TIME
    )


def test_symlinked_executable_is_running(process_module, fake_system):
    """'exe()' returns the resolved path, argv[0] the symlink path."""
    fake_system({
        100: FakeProc(
            exe="/Applications/Blender.app/Contents/MacOS/Blender",
            name="Blender",
            cmdline=["/usr/local/bin/blender"],
        ),
    })
    assert is_running(
        process_module, 100, "/opt/blender/current/blender", START_TIME
    )


def test_start_time_within_tolerance_is_running(
    process_module, fake_system
):
    fake_system({100: FakeProc(exe=MAYA_EXE, create_time=START_TIME + 0.5)})
    assert is_running(process_module, 100, MAYA_EXE, START_TIME)


def test_reused_pid_is_stopped(process_module, fake_system):
    """Same PID with a different start time is another process."""
    fake_system({100: FakeProc(exe=MAYA_EXE, create_time=START_TIME + 60)})
    assert not is_running(process_module, 100, MAYA_EXE, START_TIME)


def test_missing_process_is_stopped(process_module, fake_system):
    fake_system({})
    assert not is_running(process_module, 100, MAYA_EXE, START_TIME)


def test_zombie_process_is_stopped(process_module, fake_system):
    """Process that ended but was not reaped yet by its parent.

    On macOS the start time of a zombie can still be read.
    """
    fake_system({
        100: FakeProc(
            exe=FakeZombieProcess(100),
            name="open",
            cmdline=FakeZombieProcess(100),
            status="zombie",
        ),
    })
    assert not is_running(process_module, 100, MAYA_EXE, START_TIME)


def test_status_access_denied_uses_start_time(
    process_module, fake_system
):
    fake_system({
        100: FakeProc(exe="/usr/bin/open", status=FakeAccessDenied(100)),
    })
    assert is_running(process_module, 100, MAYA_EXE, START_TIME)


def test_start_time_error_is_stopped(process_module, fake_system):
    fake_system({
        100: FakeProc(exe=MAYA_EXE, create_time=FakeAccessDenied(100)),
    })
    assert not is_running(process_module, 100, MAYA_EXE, START_TIME)


# --- Tests: no start time, executable is compared ------------------------
def test_no_start_time_exe_error_still_checks_cmdline(
    process_module, fake_system
):
    """'exe()' failing must not skip 'name()' and 'cmdline()'."""
    fake_system({
        100: FakeProc(
            exe=FakeAccessDenied(100),
            name="Maya",
            cmdline=[MAYA_EXE],
        ),
    })
    assert is_running(process_module, 100, MAYA_EXE, None)


def test_no_start_time_matching_exe_is_running(
    process_module, fake_system
):
    fake_system({100: FakeProc(exe=MAYA_EXE, name="Maya")})
    assert is_running(process_module, 100, MAYA_EXE, None)


def test_no_start_time_other_exe_is_stopped(process_module, fake_system):
    fake_system({
        100: FakeProc(exe="/usr/bin/python3", name="python3"),
    })
    assert not is_running(process_module, 100, MAYA_EXE, None)


def test_no_start_time_windows_matches_image_name(
    process_module, fake_system
):
    fake_system(
        {100: FakeProc(exe="C:/Program Files/Autodesk/Maya2025/bin/maya.exe",
                       name="maya.exe")},
        system="Windows",
    )
    assert is_running(
        process_module,
        100,
        "C:/Program Files/Autodesk/Maya2025/bin/Maya.exe",
        None,
    )


# --- Tests: stored state -------------------------------------------------
def test_open_launch_not_stored_as_stopped(process_module, fake_system):
    """Running app launched with 'open' must not be stored as stopped."""
    fake_system({
        100: FakeProc(
            exe="/usr/bin/open",
            name="open",
            cmdline=["open", "-n", "-W", "-a", "/Applications/Maya.app"],
        ),
    })
    manager = process_module.ProcessManager()
    process_info = process_module.ProcessInfo(
        name="maya/2025",
        executable=Path(MAYA_EXE),
        args=[MAYA_EXE],
        env={},
        cwd="/tmp",
        pid=100,
        start_time=START_TIME,
    )
    manager.store_process_info(process_info)

    processes = manager.get_all_process_info()
    assert [proc.stopped for proc in processes] == [False]
    stored = manager.get_process_info(process_info.hash)
    assert stored is not None
    assert not stored.stopped
    assert manager.invalidate_process(stored)
