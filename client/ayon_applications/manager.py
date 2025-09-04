"""Application manager and application launch context."""
from __future__ import annotations

import contextlib
import copy
import inspect
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, NamedTuple, Optional, Union

from ayon_core import AYON_CORE_ROOT
from ayon_core.addon import AddonsManager
from ayon_core.lib import (
    Logger,
    classes_from_module,
    get_launcher_local_dir,
    get_linux_launcher_args,
    get_local_site_id,
    modules_from_path,
)
from ayon_core.settings import get_studio_settings

from .constants import DEFAULT_ENV_SUBGROUP
from .defs import (
    Application,
    ApplicationExecutable,
    ApplicationGroup,
    EnvironmentTool,
    EnvironmentToolGroup,
    LaunchTypes,
)
from .exceptions import (
    ApplicationExecutableNotFound,
    ApplicationNotFound,
)
from .hooks import PostLaunchHook, PreLaunchHook


@dataclass
class ProcessInfo:
    """Information about a process launched by the addon.

    Attributes:
        name (str): Name of the process.
        args (list[str]): Arguments for the process.
        env (dict[str, str]): Environment variables for the process.
        cwd (str): Current working directory for the process.
        pid (int): Process ID of the launched process.
        active (bool): Whether the process is currently active.
        output (Path): Output of the process.

    """

    name: str
    args: list[str]
    env: dict[str, str]
    cwd: str
    pid: Optional[int] = None
    active: bool = False
    output: Optional[Path] = None
    start_time: Optional[float] = None
    created_at: Optional[str] = None
    site_id: Optional[str] = None


class ProcessIdTriplet(NamedTuple):
    """Triplet of process identification values."""
    pid: int
    executable: Optional[str]  # we might not be able to get it sometimes
    start_time: Optional[float]  # the same goes for start time


class ApplicationManager:
    """Load applications and tools and store them by their full name.

    Args:
        studio_settings (dict): Preloaded studio settings. When passed manager
            will always use these values. Gives ability to create manager
            using different settings.
    """
    # holds connection to the process info storage
    # - this is used to store process information about launched applications
    _process_storage: Optional[sqlite3.Connection] = None

    def __init__(self, studio_settings: Optional[dict[str, Any]] = None):
        self.log = Logger.get_logger(self.__class__.__name__)

        self.app_groups: dict[str, ApplicationGroup] = {}
        self.applications: dict[str, Application] = {}
        self.tool_groups: dict[str, EnvironmentToolGroup] = {}
        self.tools: dict[str, EnvironmentTool] = {}

        self._studio_settings = studio_settings

        self.refresh()

    @staticmethod
    def get_process_info_storage_location() -> Path:
        """Get the path to process info storage.

        Returns:
            Path: Path to the process handlers storage.

        """
        return Path(get_launcher_local_dir()) / "process_handlers.db"

    def _get_process_storage_connection(self) -> sqlite3.Connection:
        """Store process handlers in the addon.

        Returns:
            sqlite3.Connection: Connection to the process handlers storage.

        """
        cnx = sqlite3.connect(self.get_process_info_storage_location())
        cursor = cnx.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS process_info ("
            "hash TEXT PRIMARY KEY, "
            "name TEXT, "
            "args TEXT DEFAULT NULL, "
            "env TEXT DEFAULT NULL, "
            "cwd TEXT DEFAULT NULL, "
            "pid INTEGER DEFAULT NULL, "
            "output_file TEXT DEFAULT NULL, "
            "start_time REAL DEFAULT NULL, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "site_id TEXT DEFAULT NULL"
            ")"
        )

        return cnx

    @staticmethod
    def get_process_info_hash(process_info: ProcessInfo) -> str:
        """Get hash of the process information.

        Returns:
            str: Hash of the process information.
        """
        # include executable name (if available) to reduce collisions when
        # PIDs are reused
        exe = ApplicationManager._extract_executable_name_from_args(
            process_info.args)
        # include start_time (if available) to make hash much harder to collide
        start = (
            f"{process_info.start_time}"
            if process_info.start_time is not None else ""
        )
        key = f"{process_info.name}{process_info.pid}{exe or ''}{start}"
        return sha256(key.encode()).hexdigest()

    @staticmethod
    def _extract_executable_name_from_args(
            args: Optional[Union[str, list, tuple]]) -> Optional[str]:
        """Try to extract executable (image) name from stored args.

        Returns basename of first argument if available, otherwise None.

        Args:
            args (Optional[Union[str, list, tuple]]): Arguments to extract
                executable name from.

        Returns:
            Optional[str]: Executable name or None if not found.

        """
        if not args:
            return None

        first = None
        # args might be a string, list, or nested list
        if isinstance(args, str):
            first = args
        elif isinstance(args, (list, tuple)) and len(args) > 0:
            first = args[0]
            if isinstance(first, (list, tuple)) and len(first) > 0:
                first = first[0]

        if first is None:
            return None

        try:
            return os.path.basename(str(first))
        except Exception:
            return None

    def store_process_info(self, process_info: ProcessInfo) -> None:
        """Store process information.

        Args:
            process_info (ProcessInfo): Process handler to store.

        """
        if process_info.pid is None:
            self.log.warning((
                "Cannot store process info for process without PID. "
                "Process name: %s"
            ), process_info.name)
            return
        if self._process_storage is None:
            self._process_storage = self._get_process_storage_connection()

        cursor = self._process_storage.cursor()
        process_hash = self.get_process_info_hash(process_info)
        cursor.execute(
            "INSERT OR REPLACE INTO process_info "
            "(hash, name, args, env, cwd, "
            "pid, output_file, start_time, site_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                process_hash,
                process_info.name,
                json.dumps(process_info.args),
                json.dumps(process_info.env),
                process_info.cwd,
                process_info.pid,
                (
                    process_info.output.as_posix()
                    if process_info.output else None
                ),
                process_info.start_time,
                process_info.site_id
            )
        )
        self._process_storage.commit()

    def get_process_info(self, process_hash: str) -> Optional[ProcessInfo]:
        """Get process information by hash.

        Args:
            process_hash (str): Hash of the process.

        Returns:
            Optional[ProcessInfo]: Process information or None if not found.
        """
        if self._process_storage is None:
            self._process_storage = self._get_process_storage_connection()

        cursor = self._process_storage.cursor()
        cursor.execute(
            "SELECT * FROM process_info WHERE hash = ?",
            (process_hash,)
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return ProcessInfo(
            name=row[1],
            args=json.loads(row[2]),
            env=json.loads(row[3]),
            cwd=row[4],
            pid=row[5],
            output=Path(row[6]) if row[6] else None,
            start_time=row[7],
            created_at=row[8],
            site_id=row[9]
        )

    def get_process_info_by_name(
        self, name: str, site_id: Optional[str] = None
    ) -> Optional[ProcessInfo]:
        """Get process information by name.

        Args:
            name (str): Name of the process.
            site_id (Optional[str]): Site ID to filter processes.

        Returns:
            Optional[ProcessInfo]: Process information or None if not found.
        """
        if self._process_storage is None:
            self._process_storage = self._get_process_storage_connection()

        cursor = self._process_storage.cursor()
        query = "SELECT * FROM process_info WHERE name = ?"
        params = [name]
        if site_id:
            query += " AND site_id = ?"
            params.append(site_id)

        cursor.execute(query, params)
        row = cursor.fetchone()
        if row is None:
            return None

        return ProcessInfo(
            name=row[1],
            args=json.loads(row[2]),
            env=json.loads(row[3]),
            cwd=row[4],
            pid=row[5],
            output=Path(row[6]) if row[6] else None,
            start_time=row[7],
            created_at=row[8],
            site_id=row[9]
        )

    def set_studio_settings(self, studio_settings: dict[str, Any]) -> None:
        """Ability to change init system settings.

        This will trigger refresh of manager.
        """
        self._studio_settings = studio_settings

        self.refresh()

    def refresh(self) -> None:
        """Refresh applications from settings."""
        self.app_groups.clear()
        self.applications.clear()
        self.tool_groups.clear()
        self.tools.clear()

        if self._studio_settings is not None:
            settings = copy.deepcopy(self._studio_settings)
        else:
            settings = get_studio_settings(
                clear_metadata=False, exclude_locals=False
            )

        applications_addon_settings = settings["applications"]

        # Prepare known applications
        app_defs = applications_addon_settings["applications"]
        additional_apps = app_defs.pop("additional_apps")
        for additional_app in additional_apps:
            app_name = additional_app.pop("name")
            if app_name in app_defs:
                self.log.warning(
                    f"Additional application '{app_name}' is already"
                    " in built-in applications."
                )
            app_defs[app_name] = additional_app

        for group_name, variant_defs in app_defs.items():
            group = ApplicationGroup(group_name, variant_defs, self)
            self.app_groups[group_name] = group
            for app in group:
                self.applications[app.full_name] = app

        tools_definitions = applications_addon_settings["tool_groups"]
        for tool_group_data in tools_definitions:
            group = EnvironmentToolGroup(tool_group_data, self)
            self.tool_groups[group.name] = group
            for tool in group:
                self.tools[tool.full_name] = tool

    def find_latest_available_variant_for_group(
        self, group_name: str
    ) -> Optional[ApplicationGroup]:
        group = self.app_groups.get(group_name)
        if group is None or not group.enabled:
            return None

        output = None
        for _, variant in reversed(sorted(group.variants.items())):
            executable = variant.find_executable()
            if executable:
                output = variant
                break
        return output

    def create_launch_context(
        self, app_name: str, **data
    ) -> "ApplicationLaunchContext":
        """Prepare launch context for application.

        Args:
            app_name (str): Name of application that should be launched.
            **data (Any): Any additional data. Data may be used during

        Returns:
            ApplicationLaunchContext: Launch context for application.

        Raises:
            ApplicationNotFound: Application was not found by entered name.
        """

        app = self.applications.get(app_name)
        if not app:
            raise ApplicationNotFound(app_name)

        executable = app.find_executable()

        return ApplicationLaunchContext(
            app, executable, **data
        )

    def launch_with_context(
        self, launch_context: "ApplicationLaunchContext"
    ) -> Optional[subprocess.Popen]:
        """Launch application using existing launch context.

        Args:
            launch_context (ApplicationLaunchContext): Prepared launch
                context.
        """

        if not launch_context.executable:
            raise ApplicationExecutableNotFound(launch_context.application)
        return launch_context.launch()

    def launch(self, app_name, **data) -> Optional[subprocess.Popen]:
        """Launch procedure.

        For host application it's expected to contain "project_name",
        "folder_path" and "task_name".

        Args:
            app_name (str): Name of application that should be launched.
            **data (Any): Any additional data. Data may be used during
                preparation to store objects usable in multiple places.

        Raises:
            ApplicationNotFound: Application was not found by entered
                argument `app_name`.
            ApplicationExecutableNotFound: Executables in application
                definition were not found on this machine.
            ApplicationLaunchFailed: Something important for application launch
                failed. Exception should contain an explanation message,
                traceback should not be needed.

        """
        context = self.create_launch_context(app_name, **data)
        return self.launch_with_context(context)

    def get_all_process_info(self) -> list[ProcessInfo]:
        """Get all process information from the database.

        Returns:
            list[ProcessInfo]: List of all process information.
        """
        if self._process_storage is None:
            self._process_storage = self._get_process_storage_connection()

        cursor = self._process_storage.cursor()
        cursor.execute("SELECT * FROM process_info ORDER BY created_at DESC")
        rows = cursor.fetchall()

        processes: list[ProcessInfo] = [
            ProcessInfo(
                name=row[1],
                args=json.loads(row[2]) if row[2] else [],
                env=json.loads(row[3]) if row[3] else {},
                cwd=row[4],
                pid=row[5],
                output=Path(row[6]) if row[6] else None,
                start_time=row[7],
                created_at=row[8],
                site_id=row[9],
            )
            for row in rows
        ]
        # Check if processes are still running
        # This is done by checking the pid of the process.
        # It is using `_are_processes_running` method which
        # checks for processes in batch, mostly because of the fallback
        # on systems without `psutil` module. See `_are_processes_running`
        # documentation for more details.
        # Build list of (pid, executable_name, start_time) triplets so the
        # check can verify PID + image and, when possible, process start time
        # (stronger protection against PID reuse).
        pid_triplets: list[ProcessIdTriplet] = []
        processes_with_pid = []
        for proc in processes:
            if proc.pid is None:
                continue
            exe = self._extract_executable_name_from_args(proc.args)
            pid_triplets.append(
                ProcessIdTriplet(proc.pid, exe, proc.start_time))
            processes_with_pid.append(proc)

        if pid_triplets:
            running_status = self._are_processes_running(pid_triplets)
            for proc, (_, is_running) in zip(
                    processes_with_pid, running_status):
                proc.active = is_running

        return processes

    def delete_process_info(self, process_hash: str) -> bool:
        """Delete process information by hash.

        Args:
            process_hash (str): Hash of the process to delete.

        Returns:
            bool: True if deleted, False if not found.
        """
        if self._process_storage is None:
            self._process_storage = self._get_process_storage_connection()

        cursor = self._process_storage.cursor()
        cursor.execute(
            "DELETE FROM process_info WHERE hash = ?",
            (process_hash,))
        self._process_storage.commit()
        return cursor.rowcount > 0

    def delete_inactive_processes(self) -> int:
        """Delete all inactive process information.

        Returns:
            int: Number of deleted processes.
        """
        if self._process_storage is None:
            self._process_storage = self._get_process_storage_connection()

        # Get all processes and check which ones are inactive
        all_processes = self.get_all_process_info()
        inactive_hashes = []

        for process in all_processes:
            if not process.active:
                process_hash = self.get_process_info_hash(process)
                inactive_hashes.append(process_hash)

        if not inactive_hashes:
            return 0

        cursor = self._process_storage.cursor()
        placeholders = ",".join("?" * len(inactive_hashes))
        cursor.execute(
            ("DELETE FROM process_info WHERE "  # noqa: S608
            f"hash IN ({placeholders})"),
            inactive_hashes
        )
        self._process_storage.commit()
        return cursor.rowcount

    @staticmethod
    def _is_process_running_psutils(
            pid: int,
            executable: str,
            start_time: Optional[float] = None) -> bool:
        """Check if a process is running using psutil.

        Args:
            pid (int): Process ID to check.
            executable (str): Executable name to verify.
            start_time (Optional[float]): Start time to verify.

        Returns:
            bool: True if the process is running, False otherwise.

        """
        import psutil
        # Use psutil to check process existence and inspect its image/cmdline
        try:
            proc = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return False

        # If start_time provided, verify it matches process creation time
        if start_time is not None:
            try:
                proc_ct = proc.create_time()
                # allow small tolerance for float differences
                if abs(proc_ct - float(start_time)) > 1.0:
                    return False
            except Exception:  # noqa: BLE001
                # cannot verify start time -> conservative False
                return False

        if not executable:
            # No executable provided, process exists
            # (and start_time matched if provided)
            return True

        # Try to get executable path/name and command line first
        candidates = set()
        with contextlib.suppress(Exception):
            exe_path = proc.exe() if hasattr(proc, "exe") else None
            if exe_path:
                candidates.add(os.path.basename(exe_path).lower())

            name = proc.name()
            if name:
                candidates.add(name.lower())

            cmd = proc.cmdline()
            if cmd:
                first = cmd[0]
                candidates.add(os.path.basename(first).lower())

        return executable.lower() in candidates

    @staticmethod
    def _are_processes_running(
            pid_triplets: list[ProcessIdTriplet]) -> list[tuple[int, bool]]:
        """Check if the processes are still running.

        This checks for presence of `psutil` module and uses it if available.
        If `psutil` is not available, it falls back to using `os.kill` on Unix
        systems or `tasklist` command on Windows to check if the processes
        are running. `psutil` is preferred because it is more reliable and
        provides a consistent interface across platforms. But since it is a
        not pure Python module, it may not be available on all systems.

        The batch check is done to avoid multiple calls to the system
        to check for each process individually, which can be inefficient -
        especially on Windows where `tasklist` can be slow for many processes.
        `tasklist` supports querying multiple processes at once using
        the `/FI` filter option.

        We should refactor this method once we find out that the fallback
        method is not needed anymore.

        Args:
            pid_triplets (list[ProcessIdTriplet]): Processes ID to check.

        Returns:
            list[tuple[int, bool]]: List of tuples with process ID and
                boolean indicating if the process is running.

        """
        if not pid_triplets:
            result: list[tuple[int, bool]] = []

            return result

        try:
            return ApplicationManager._check_processes_running_psutil(
                pid_triplets)

        except ImportError:
            # Fallback for systems without psutil
            if platform.system().lower() == "windows":
                return ApplicationManager._check_processes_running_win(
                    pid_triplets)

            return ApplicationManager._check_processes_running_unix(
                pid_triplets)

    @staticmethod
    def _check_processes_running_psutil(
            pid_triplets: list[ProcessIdTriplet]) -> list[tuple[int, bool]]:
        """Check if processes are running using psutil.

        Args:
            pid_triplets (list[ProcessIdTriplet]): List of triplets

        Returns:
            list[tuple[int, bool]]: List of tuples with process ID and
                boolean indicating if the process is running.

        """
        result: list[tuple[int, bool]] = []
        import psutil
        for pid, exe, start_time in pid_triplets:
            try:
                is_running = ApplicationManager._is_process_running_psutils(
                    pid, exe, start_time
                )
            except Exception:  # noqa: BLE001
                # if something goes wrong, fall back to pid_exists
                try:
                    is_running = psutil.pid_exists(pid)
                except Exception:   # noqa: BLE001
                    is_running = False
            result.append((pid, is_running))
        return result

    @staticmethod
    def _check_processes_running_win(
        pid_triplets: list[ProcessIdTriplet],
    ) -> list[tuple[int, bool]]:
        """Check if processes are running on Windows using tasklist.

        Args:
            pid_triplets (list[ProcessIdTriplet]): List of triplets

        Returns:
            list[tuple[int, bool]]: List of tuples with process ID and
                boolean indicating if the process is running.

        """
        result: list[tuple[int, bool]] = []
        # Use tasklist CSV output for more robust parsing (handles spaces)
        filters: list[str] = []
        filters.extend(f"/FI PID eq {pid}" for pid, _, _ in pid_triplets)
        try:
            tasklist_result = subprocess.run(
                ["tasklist", "/FO", "CSV", *filters],  # noqa: S607
                capture_output=True,
                text=True,
                check=True
            )
        except (subprocess.SubprocessError, subprocess.CalledProcessError):
            return []

        # Parse CSV lines: "Image Name","PID",...
        for raw_line in tasklist_result.stdout.splitlines():
            line = raw_line.strip()
            if not line or line.startswith('"Image Name"'):
                continue
            # simple CSV parse: split by comma and strip quotes
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) < 2:  # noqa: PLR2004
                continue
            image = parts[0]
            pid_str = parts[1]
            if not pid_str.isdigit():
                continue
            pid = int(pid_str)
            for expected_pid, expected_exe, _ in pid_triplets:
                if pid != expected_pid:
                    continue
                # cannot verify start time here (tasklist doesn't provide it)
                if expected_exe is None:
                    result.append((pid, True))
                else:
                    result.append((pid, image.lower() == expected_exe.lower()))

        return result

    @staticmethod
    def _check_processes_running_unix(
        pid_triplets: list[ProcessIdTriplet],
    ) -> list[tuple[int, bool]]:
        """Check if processes are running on Unix using /proc, ps, or os.kill.

        Args:
            pid_triplets (list[ProcessIdTriplet]): List of triplets

        Returns:
            list[tuple[int, bool]]: List of tuples with process ID and
                boolean indicating if the process is running.
        """
        result: list[tuple[int, bool]] = []
        # POSIX fallback - try /proc, ps, or os.kill
        for pid, expected_exe, _ in pid_triplets:
            with contextlib.suppress(Exception):
                # Prefer /proc if available
                proc_exe_path = f"/proc/{pid}/exe"
                if os.path.islink(proc_exe_path):
                    target = os.readlink(proc_exe_path)
                    image = os.path.basename(target)
                    if (
                        expected_exe is None
                        or image.lower() == expected_exe.lower()
                    ):
                        result.append((pid, True))
                    else:
                        result.append((pid, False))
                    continue

            # Try ps -p <pid> -o comm=
            with contextlib.suppress(Exception):
                ps_res = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "comm="],  # noqa: S607
                    capture_output=True,
                    text=True,
                    check=True
                )
                name = ps_res.stdout.strip()
                if name:
                    image = os.path.basename(name)
                    result.append(
                        (
                            pid,
                            expected_exe is None or image.lower() == expected_exe.lower())  # noqa: E501
                        )
                    continue

            # Last resort: check process existence with signal 0
            try:
                os.kill(pid, 0)
            except OSError:
                result.append((pid, False))
            else:
                # We know process exists but cannot verify
                # image name or start time
                result.append((pid, expected_exe is None))

        return result


class ApplicationLaunchContext:
    """Context of launching application.

    Main purpose of context is to prepare launch arguments and keyword
    arguments for new process. Most important part of keyword arguments
    preparations are environment variables.

    During the whole process is possible to use `data` attribute to store
    object usable in multiple places.

    Launch arguments are strings in list. It is possible to "chain" argument
    when order of them matters. That is possible to do with adding list where
    order is right and should not change.
    NOTE: This is recommendation, not requirement.
    e.g.: `["nuke.exe", "--NukeX"]` -> In this case any part of process may
    insert argument between `nuke.exe` and `--NukeX`. To keep them together
    it is better to wrap them in another list: `[["nuke.exe", "--NukeX"]]`.

    Notes:
        It is possible to use launch context only to prepare environment
            variables. In that case `executable` may be None and can be used
            'run_prelaunch_hooks' method to run prelaunch hooks which prepare
            them.

    Args:
        application (Application): Application definition.
        executable (ApplicationExecutable): Object with path to executable.
        env_group (Optional[str]): Environment variable group. If not set
            'DEFAULT_ENV_SUBGROUP' is used.
        launch_type (Optional[str]): Launch type. If not set 'local' is used.
        **data (dict): Any additional data. Data may be used during
            preparation to store objects usable in multiple places.
    """

    def __init__(
        self,
        application: Application,
        executable: ApplicationExecutable,
        env_group: Optional[str] = None,
        launch_type: Optional[str] = None,
        **data
    ):
        # Application object
        self.application: Application = application

        self.addons_manager: AddonsManager = AddonsManager()

        # Logger
        self.log: logging.Logger = Logger.get_logger(
            f"{self.__class__.__name__}-{application.full_name}"
        )

        self.executable: ApplicationExecutable = executable

        if launch_type is None:
            launch_type = LaunchTypes.local
        self.launch_type: str = launch_type

        if env_group is None:
            env_group = DEFAULT_ENV_SUBGROUP

        self.env_group: str = env_group

        self.data: dict[str, Any] = dict(data)

        launch_args = []
        if executable is not None:
            launch_args = executable.as_args()
        # subprocess.Popen launch arguments (first argument in constructor)
        self.launch_args: list[str] = launch_args
        self.launch_args.extend(application.arguments)
        if self.data.get("app_args"):
            self.launch_args.extend(self.data.pop("app_args"))

        # Handle launch environemtns
        src_env = self.data.pop("env", None)
        if src_env is not None and not isinstance(src_env, dict):
            self.log.warning(
                f"Passed `env` kwarg has invalid type: {type(src_env)}."
                " Expected: `dict`. Using `os.environ` instead."
            )
            src_env = None

        if src_env is None:
            src_env = os.environ

        ignored_env = {"QT_API", }
        env = {
            key: str(value)
            for key, value in src_env.items()
            if key not in ignored_env
        }
        # subprocess.Popen keyword arguments
        self.kwargs = {"env": env}

        if platform.system().lower() == "windows":
            # Detach new process from currently running process on Windows
            flags = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
            )
            self.kwargs["creationflags"] = flags

        if not sys.stdout:
            self.kwargs["stdout"] = subprocess.DEVNULL
            self.kwargs["stderr"] = subprocess.DEVNULL

        self.prelaunch_hooks = None
        self.postlaunch_hooks = None

        self.process = None
        self._prelaunch_hooks_executed = False

    @property
    def env(self) -> dict[str, str]:
        if (
            "env" not in self.kwargs
            or self.kwargs["env"] is None
        ):
            self.kwargs["env"] = {}
        return self.kwargs["env"]

    @env.setter
    def env(self, value: dict[str, str]) -> None:
        if not isinstance(value, dict):
            raise TypeError(
                f"'env' attribute expect 'dict' object. Got: {type(value)}"
            )
        self.kwargs["env"] = value

    @property
    def modules_manager(self) -> AddonsManager:
        """
        Deprecated:
            Use 'addons_manager' instead.

        """
        return self.addons_manager

    def _collect_addons_launch_hook_paths(self) -> list[str]:
        """Helper to collect application launch hooks from addons.

        Module have to have implemented 'get_launch_hook_paths' method which
        can expect application as argument or nothing.

        Returns:
            list[str]: Paths to launch hook directories.

        """
        expected_types = (list, tuple, set)

        output = []
        for module in self.addons_manager.get_enabled_addons():
            # Skip module if does not have implemented 'get_launch_hook_paths'
            func = getattr(module, "get_launch_hook_paths", None)
            if func is None:
                continue

            func = module.get_launch_hook_paths
            if hasattr(inspect, "signature"):
                sig = inspect.signature(func)
                expect_args = len(sig.parameters) > 0
            else:
                expect_args = len(inspect.getargspec(func)[0]) > 0

            # Pass application argument if method expect it.
            try:
                if expect_args:
                    hook_paths = func(self.application)
                else:
                    hook_paths = func()
            except Exception:
                self.log.warning(
                    "Failed to call 'get_launch_hook_paths'",
                    exc_info=True
                )
                continue

            if not hook_paths:
                continue

            # Convert string to list
            if isinstance(hook_paths, str):
                hook_paths = [hook_paths]

            # Skip invalid types
            if not isinstance(hook_paths, expected_types):
                self.log.warning(
                    "Result of `get_launch_hook_paths` has invalid"
                    f" type {type(hook_paths)}. Expected {expected_types}"
                )
                continue

            output.extend(hook_paths)
        return output

    def paths_to_launch_hooks(self) -> list[str]:
        """Directory paths where to look for launch hooks."""
        # This method has potential to be part of application manager (maybe).
        paths = []

        # TODO load additional studio paths from settings
        global_hooks_dir = os.path.join(AYON_CORE_ROOT, "hooks")

        hooks_dirs = [
            global_hooks_dir
        ]
        if self.host_name:
            # If host requires launch hooks and is module then launch hooks
            #   should be collected using 'collect_launch_hook_paths'
            #   - module have to implement 'get_launch_hook_paths'
            host_module = self.addons_manager.get_host_addon(self.host_name)
            if not host_module:
                hooks_dirs.append(os.path.join(
                    AYON_CORE_ROOT, "hosts", self.host_name, "hooks"
                ))

        for path in hooks_dirs:
            if (
                os.path.exists(path)
                and os.path.isdir(path)
                and path not in paths
            ):
                paths.append(path)

        # Load modules paths
        paths.extend(self._collect_addons_launch_hook_paths())

        return paths

    def discover_launch_hooks(self, force: bool = False) -> None:
        """Load and prepare launch hooks."""
        if (
            self.prelaunch_hooks is not None
            or self.postlaunch_hooks is not None
        ):
            if not force:
                self.log.info("Launch hooks were already discovered.")
                return

            self.prelaunch_hooks.clear()
            self.postlaunch_hooks.clear()

        self.log.debug("Discovery of launch hooks started.")

        paths = self.paths_to_launch_hooks()
        self.log.debug("Paths searched for launch hooks:\n{}".format(
            "\n".join(f"- {path}" for path in paths)
        ))

        all_classes = {
            "pre": [],
            "post": []
        }
        for path in paths:
            if not os.path.exists(path):
                self.log.info(
                    f"Path to launch hooks does not exist: \"{path}\""
                )
                continue

            modules, _crashed = modules_from_path(path)
            for _filepath, module in modules:
                all_classes["pre"].extend(
                    classes_from_module(PreLaunchHook, module)
                )
                all_classes["post"].extend(
                    classes_from_module(PostLaunchHook, module)
                )

        for launch_type, classes in all_classes.items():
            hooks_with_order = []
            hooks_without_order = []
            for klass in classes:
                try:
                    hook = klass(self)
                    if not hook.is_valid:
                        self.log.debug(
                            "Skipped hook invalid for current launch context:"
                            f" {klass.__name__}"
                        )
                        continue

                    if inspect.isabstract(hook):
                        self.log.debug(
                            f"Skipped abstract hook: {klass.__name__}"
                        )
                        continue

                    # Separate hooks by pre/post class
                    if hook.order is None:
                        hooks_without_order.append(hook)
                    else:
                        hooks_with_order.append(hook)

                except Exception:
                    self.log.warning(
                        f"Initialization of hook failed: {klass.__name__}",
                        exc_info=True
                    )

            # Sort hooks with order by order
            ordered_hooks = list(sorted(
                hooks_with_order, key=lambda obj: obj.order
            ))
            # Extend ordered hooks with hooks without defined order
            ordered_hooks.extend(hooks_without_order)

            if launch_type == "pre":
                self.prelaunch_hooks = ordered_hooks
            else:
                self.postlaunch_hooks = ordered_hooks

        self.log.debug(
            f"Found {len(self.prelaunch_hooks)} prelaunch"
            f" and {len(self.postlaunch_hooks)} postlaunch hooks."
        )

    @property
    def app_name(self) -> str:
        return self.application.name

    @property
    def host_name(self) -> str:
        return self.application.host_name

    @property
    def app_group(self) -> ApplicationGroup:
        return self.application.group

    @property
    def manager(self) -> ApplicationManager:
        return self.application.manager

    def _run_process(self) -> subprocess.Popen:
        """Run the process with the given launch arguments and keyword args.

        This method will handle the process differently based on the platform
        it is running on. It will create a temporary file for output on
        Windows and MacOS, while on Linux it will use a mid-process to launch
        the application with the provided arguments and environment variables.

        Todo (antirotor): store process info to the database on linux.

        Returns:
            subprocess.Popen: The process object created by Popen.

        """
        # Windows and MacOS have easier process start
        low_platform = platform.system().lower()
        if low_platform in ("windows", "darwin"):
            return self._execute_with_stdout()
        # Linux uses mid process
        # - it is possible that the mid process executable is not
        #   available for this version of AYON in that case use standard
        #   launch
        launch_args = get_linux_launcher_args()
        if launch_args is None:
            return subprocess.Popen(self.launch_args, **self.kwargs)

        # Prepare data that will be passed to midprocess
        # - store arguments to a json and pass path to json as last argument
        # - pass environments to set
        app_env = self.kwargs.pop("env", {})
        # create temporary file path passed to midprocess
        temp_file = tempfile.NamedTemporaryFile(
            mode="w",
            prefix=f"ayon_{self.application.host_name}_output_",
            suffix=".txt",
            delete=False
        )

        json_data = {
            "name": self.application.full_name,
            "site_id": get_local_site_id(),
            "cwd": os.getcwd(),
            "args": self.launch_args,
            "env": app_env,
            "output": temp_file.name
        }
        if app_env:
            # Filter environments of subprocess
            self.kwargs["env"] = {
                key: value
                for key, value in os.environ.items()
                if key in app_env
            }

        # Create the temp file
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="ay_app_args", suffix=".json", delete=False
        ) as json_temp:
            json_temp_filepath = json_temp.name
            json.dump(json_data, json_temp)

        launch_args.append(json_temp_filepath)

        # Create mid-process which will launch application
        process = subprocess.Popen(launch_args, **self.kwargs)
        # Wait until the process finishes
        #   - This is important! The process would stay in "open" state.
        process.wait()

        # read back pid from the json file
        try:
            with open(json_temp_filepath, encoding="utf-8") as stream:
                json_data = json.load(stream)

                try:
                    import psutil
                except ImportError:
                    psutil = None

                pid_from_mid = json_data.get("pid")
                start_time = None
                if pid_from_mid and psutil:
                    start_time = self._get_process_start_time(process)

                process_info = ProcessInfo(
                    name=self.application.full_name,
                    args=self.launch_args,
                    env=self.kwargs.get("env", {}),
                    cwd=os.getcwd(),
                    pid=pid_from_mid,
                    output=Path(temp_file.name),
                    start_time=start_time,
                    site_id=get_local_site_id(),
                )
                # Store process info to the database
                self.manager.store_process_info(process_info)
        except OSError:
            self.log.exception(
                "Failed to read process info from JSON file: %s"
            )

        # Remove the temp file
        os.remove(json_temp_filepath)
        # Return process which is already terminated
        return process

    def run_prelaunch_hooks(self) -> None:
        """Run prelaunch hooks.

        This method will be executed only once, any future calls will skip
        the processing.

        """
        if self._prelaunch_hooks_executed:
            self.log.warning("Prelaunch hooks were already executed.")
            return
        # Discover launch hooks
        self.discover_launch_hooks()

        # Execute prelaunch hooks
        for hook in self.prelaunch_hooks:
            self.log.debug(
                f"Executing prelaunch hook: {hook.__class__.__name__}"
            )
            hook.execute()
        self._prelaunch_hooks_executed = True

    def launch(self) -> Optional[subprocess.Popen]:
        """Collect data for new process and then create it.

        This method must not be executed more than once.

        Returns:
            subprocess.Popen: Created process as Popen object.

        """
        if self.process is not None:
            self.log.warning("Application was already launched.")
            return None

        if not self._prelaunch_hooks_executed:
            self.run_prelaunch_hooks()

        self.log.debug("All prelaunch hook executed. Starting new process.")

        # Prepare subprocess args
        args_len_str = ""
        if isinstance(self.launch_args, str):
            args = self.launch_args
        else:
            args = self.clear_launch_args(self.launch_args)
            args_len_str = f" ({len(args)})"
        self.log.info(
            f'Launching "{self.application.full_name}"'
            f" with args{args_len_str}: {args}"
        )
        self.launch_args = args

        # Run process
        self.process = self._run_process()

        # Process post launch hooks
        for hook in self.postlaunch_hooks:
            self.log.debug(
                f"Executing postlaunch hook: {hook.__class__.__name__}"
            )

            # TODO how to handle errors?
            # - store to variable to let them accessible?
            try:
                hook.execute()

            except Exception:
                self.log.warning(
                    "After launch procedures were not successful.",
                    exc_info=True,
                )

        self.log.debug(f"Launch of {self.application.full_name} finished.")

        return self.process

    @staticmethod
    def clear_launch_args(args: list) -> list[str]:
        """Collect launch arguments to final order.

        Launch argument should be a list that may contain another lists this
        function will upack inner lists and keep ordering.

        ```
        # source
        [ [ arg1, [ arg2, arg3 ] ], arg4, [arg5, arg6]]
        # result
        [ arg1, arg2, arg3, arg4, arg5, arg6]

        Args:
            args (list): Source arguments in list may contain inner lists.

        Returns:
            list: Unpacked arguments.

        """
        all_cleared = False
        while not all_cleared:
            all_cleared = True
            new_args = []
            for arg in args:
                if isinstance(arg, (list, tuple, set)):
                    all_cleared = False
                    for _arg in arg:
                        new_args.append(_arg)
                else:
                    new_args.append(arg)
            args = new_args

        return args

    def _execute_with_stdout(self) -> subprocess.Popen:
        """Run the process with stdout and stderr redirected to a file.

        Stores process information to the database.

        Returns:
            subprocess.Popen: The process object created by Popen.
        """
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix=f"ayon_{self.application.host_name}_output_",
            suffix=".txt",
            delete=False, encoding="utf-8"
        ) as temp_file:
            temp_file_path = temp_file.name

        with open(temp_file_path, "wb") as tmp_file:
            self.kwargs["stdout"] = tmp_file
            self.kwargs["stderr"] = tmp_file
            process = subprocess.Popen(self.launch_args, **self.kwargs)

            start_time = self._get_process_start_time(process)

            process_info = ProcessInfo(
                name=self.application.full_name,
                args=self.launch_args,
                env=self.kwargs.get("env", {}),
                cwd=os.getcwd(),
                pid=process.pid,
                output=Path(temp_file_path),
                start_time=start_time,
                site_id=get_local_site_id()
            )
            # Store process info to the database
            self.manager.store_process_info(process_info)

        return process

    @staticmethod
    def _get_process_start_time(
            process: subprocess.Popen) -> Optional[float]:
        """Get the start time of a process using psutil.

        Returns:
            Optional[float]: The start time of the process in seconds since
                the epoch, or None if it cannot be determined.

        """
        # Try to fetch process start time when psutil is available
        try:
            import psutil
        except ImportError:
            return None

        start_time = None
        if process.pid:
            try:
                start_time = psutil.Process(process.pid).create_time()
            except (
                    psutil.NoSuchProcess,
                    psutil.ZombieProcess,
                    psutil.AccessDenied):
                start_time = None
        return start_time
