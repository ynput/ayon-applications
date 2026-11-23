"""Test workdir creation without requiring an AYON installation."""
from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest


CLIENT_ROOT = (
    Path(__file__).resolve().parents[3] / "client" / "ayon_applications"
)


@pytest.fixture
def hook_modules(monkeypatch):
    """Load the real hook, launch filters, and exception classes."""
    core = types.ModuleType("ayon_core")
    core_lib = types.ModuleType("ayon_core.lib")
    core_lib.Logger = types.SimpleNamespace(get_logger=logging.getLogger)
    core_lib.find_executable = lambda path: path
    core_addon = types.ModuleType("ayon_core.addon")
    core_addon.AddonsManager = object
    core.lib = core_lib
    core.addon = core_addon
    package = types.ModuleType("ayon_applications")
    package.__path__ = [str(CLIENT_ROOT)]
    for name, module in (
        ("ayon_core", core),
        ("ayon_core.lib", core_lib),
        ("ayon_core.addon", core_addon),
        ("ayon_applications", package),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    defs = load("ayon_applications.defs", CLIENT_ROOT / "defs.py")
    hooks = load("ayon_applications.hooks", CLIENT_ROOT / "hooks.py")
    exceptions = load(
        "ayon_applications.exceptions", CLIENT_ROOT / "exceptions.py"
    )
    package.LaunchTypes = defs.LaunchTypes
    package.PreLaunchHook = hooks.PreLaunchHook
    package.ApplicationLaunchFailed = exceptions.ApplicationLaunchFailed
    hook = load(
        "_ayon_create_workdir_under_test",
        CLIENT_ROOT / "hooks" / "prelaunch_create_workdir.py",
    )
    return hook, package


def make_hook(hook_modules, workdir, launch_type="local", is_host=True):
    """Create a launch context using the production hook validation."""
    hook_module, _ = hook_modules
    context = types.SimpleNamespace(
        application=types.SimpleNamespace(is_host=is_host),
        launch_type=launch_type,
        env={} if workdir is None else {"AYON_WORKDIR": str(workdir)},
    )
    return hook_module.CreateWorkdirHook(context)


@pytest.mark.parametrize(
    ("launch_type", "should_create"),
    [
        ("local", True),
        ("remote", True),
        ("automated", True),
        ("farm-publish", False),
        ("farm-render", False),
    ],
)
def test_workdir_creation_by_launch_type(
    hook_modules, tmp_path, launch_type, should_create
):
    """Create workdirs for host launches while farm extraction skips them."""
    workdir = tmp_path / "task" / "work"
    hook = make_hook(hook_modules, workdir, launch_type)
    if hook.is_valid:
        hook.execute()
    assert workdir.is_dir() is should_create
    assert workdir.parent.exists() is should_create


def test_existing_workdir_preserves_contents(hook_modules, tmp_path):
    """Preserve existing files when the workdir already exists."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    workfile = workdir / "scene.hip"
    workfile.write_text("existing workfile")
    make_hook(hook_modules, workdir).execute()
    assert workfile.read_text() == "existing workfile"


def test_non_host_does_not_create_workdir(hook_modules, tmp_path):
    """Ignore inherited workdir variables for non-host applications."""
    workdir = tmp_path / "work"
    make_hook(hook_modules, workdir, is_host=False).execute()
    assert not workdir.exists()


@pytest.mark.parametrize("workdir", [None, ""])
def test_missing_workdir_is_skipped(hook_modules, monkeypatch, workdir):
    """Allow launch contexts without a computed workdir."""
    hook_module, _ = hook_modules

    def unexpected_makedirs(*args, **kwargs):
        pytest.fail("No directory should be created without AYON_WORKDIR")

    monkeypatch.setattr(hook_module.os, "makedirs", unexpected_makedirs)
    make_hook(hook_modules, workdir).execute()


def test_creation_failure_aborts_launch(hook_modules, tmp_path):
    """Keep directory creation errors in the application-launch contract."""
    _, package = hook_modules
    blocker = tmp_path / "blocked"
    blocker.write_text("A file obstructing the workdir's parent")
    workdir = blocker / "work"
    with pytest.raises(
        package.ApplicationLaunchFailed, match="Couldn't create workdir"
    ) as raised:
        make_hook(hook_modules, workdir).execute()
    assert isinstance(raised.value.__cause__, OSError)
    assert not workdir.exists()
