from __future__ import annotations
from dataclasses import dataclass
import os
import sys
import copy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import platform
import inspect
import sqlite3
import subprocess
from typing import Optional

from ayon_core import AYON_CORE_ROOT
from ayon_core.settings import get_studio_settings
from ayon_core.lib import (
    Logger,
    modules_from_path,
    classes_from_module,
    get_linux_launcher_args,
    get_local_site_id,
)
from ayon_core.addon import AddonsManager

from .constants import DEFAULT_ENV_SUBGROUP
from .exceptions import (
    ApplicationNotFound,
    ApplicationExecutableNotFound,
)
from .hooks import PostLaunchHook, PreLaunchHook
from .defs import EnvironmentToolGroup, ApplicationGroup, LaunchTypes

# Check if psutil is available
try:
    import psutil
except ImportError:
    psutil = None


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
    created_at: Optional[str] = None
    site_id: Optional[str] = None


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

    def __init__(self, studio_settings=None):
        self.log = Logger.get_logger(self.__class__.__name__)

        self.app_groups = {}
        self.applications = {}
        self.tool_groups = {}
        self.tools = {}

        self._studio_settings = studio_settings

        self.refresh()

    @staticmethod
    def get_process_info_storage_location() -> Path:
        """Get the path to process info storage.

        Returns:
            Path: Path to the process handlers storage.

        """
        storage_root = os.getenv("AYON_LAUNCHER_LOCAL_DIR")
        if not storage_root:
            msg = (
                "Cannot determine process handlers storage location. "
                "Environment variable 'AYON_LAUNCHER_LOCAL_DIR' is not set. "
            )
            raise RuntimeError(msg)
        return Path(storage_root) / "process_handlers.db"

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
        return sha256(
            f"{process_info.name}{process_info.pid}".encode()).hexdigest()


    def store_process_info(self, process_info: ProcessInfo) -> None:
        """Store process information.

        Args:
            process_info (ProcessInfo): Process handler to store.
        """
        if self._process_storage is None:
            self._process_storage = self._get_process_storage_connection()

        cursor = self._process_storage.cursor()
        process_hash = self.get_process_info_hash(process_info)
        cursor.execute(
            "INSERT OR REPLACE INTO process_info "
            "(hash, name, args, env, cwd, pid, output_file, site_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
            created_at=row[7],
            site_id=row[8]
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
            created_at=row[7],
            site_id=row[8]
        )

    def set_studio_settings(self, studio_settings):
        """Ability to change init system settings.

        This will trigger refresh of manager.
        """
        self._studio_settings = studio_settings

        self.refresh()

    def refresh(self):
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
                self.log.warning((
                    "Additional application '{}' is already"
                    " in built-in applications."
                ).format(app_name))
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

    def find_latest_available_variant_for_group(self, group_name):
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

    def create_launch_context(self, app_name, **data):
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

    def launch_with_context(self, launch_context):
        """Launch application using existing launch context.

        Args:
            launch_context (ApplicationLaunchContext): Prepared launch
                context.
        """

        if not launch_context.executable:
            raise ApplicationExecutableNotFound(launch_context.application)
        return launch_context.launch()

    def launch(self, app_name, **data):
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
            ApplicationExecutableNotFound: Executables in application definition
                were not found on this machine.
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

        processes: list[ProcessInfo] = []
        processes.extend(
            ProcessInfo(
                name=row[1],
                args=json.loads(row[2]) if row[2] else [],
                env=json.loads(row[3]) if row[3] else {},
                cwd=row[4],
                pid=row[5],
                output=Path(row[6]) if row[6] else None,
                created_at=row[7],
                site_id=row[8],
            )
            for row in rows
        )
        # Check if processes are still running
        pids = [proc.pid for proc in processes if proc.pid is not None]
        if pids:
            running_status = self._are_processes_running(pids)
            for proc, is_running in zip(processes, running_status):
                proc.active = is_running[1]

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
        cursor.execute("DELETE FROM process_info WHERE hash = ?", (process_hash,))
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
        placeholders = ','.join('?' * len(inactive_hashes))
        cursor.execute(
            f"DELETE FROM process_info WHERE hash IN ({placeholders})",
            inactive_hashes
        )
        self._process_storage.commit()
        return cursor.rowcount

    @staticmethod
    def _is_process_running_psutils(pid: int) -> bool:
        """Check if a process is running using psutil.

        Args:
            pid (int): Process ID to check.

        Returns:
            bool: True if the process is running, False otherwise.

        """
        # import check should be done before invoking this method
        if not psutil:
            msg = "psutil module is not available."
            raise RuntimeError(msg)

        return psutil.pid_exists(pid)

    @staticmethod
    def _are_processes_running(pids: list[int]) -> list[tuple[int, bool]]:
        """Check if the processes are still running.

        Args:
            pids (list[int]): Processes ID to check.

        Returns:
            list[tuple[int, bool]]: List of tuples with process ID and
                boolean indicating if the process is running.
        """
        result: list[tuple[int, bool]] = []

        if not pids:
            return result

        try:
            import psutil
            for pid in pids:
                is_running = psutil.pid_exists(pid)
                result.append((pid, is_running))

            return result

        except ImportError:
            # Fallback for systems without psutil
            if platform.system().lower() == "windows":
                filters: list[str] = []
                filters.extend(f"/FI PID eq {pid}" for pid in pids)
                import subprocess

                try:
                    tasklist_result = subprocess.run(
                        ["tasklist", *filters], capture_output=True, text=True
                    )
                    for line in tasklist_result.stdout.splitlines():
                        for pid in pids:
                            if f"{pid}" in line:
                                result.append((pid, True))
                                break
                except subprocess.SubprocessError:
                    return []
            else:
                for pid in pids:
                    # Check if the process is running by sending signal 0
                    # - this does not send any signal, just checks if the
                    # process exists
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        result.append((pid, False))
                    else:
                        result.append((pid, True))
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
        application,
        executable,
        env_group=None,
        launch_type=None,
        **data
    ):
        # Application object
        self.application = application

        self.addons_manager = AddonsManager()

        # Logger
        logger_name = "{}-{}".format(self.__class__.__name__,
                                     self.application.full_name)
        self.log = Logger.get_logger(logger_name)

        self.executable = executable

        if launch_type is None:
            launch_type = LaunchTypes.local
        self.launch_type = launch_type

        if env_group is None:
            env_group = DEFAULT_ENV_SUBGROUP

        self.env_group = env_group

        self.data = dict(data)

        launch_args = []
        if executable is not None:
            launch_args = executable.as_args()
        # subprocess.Popen launch arguments (first argument in constructor)
        self.launch_args = launch_args
        self.launch_args.extend(application.arguments)
        if self.data.get("app_args"):
            self.launch_args.extend(self.data.pop("app_args"))

        # Handle launch environemtns
        src_env = self.data.pop("env", None)
        if src_env is not None and not isinstance(src_env, dict):
            self.log.warning((
                "Passed `env` kwarg has invalid type: {}. Expected: `dict`."
                " Using `os.environ` instead."
            ).format(str(type(src_env))))
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
    def env(self):
        if (
            "env" not in self.kwargs
            or self.kwargs["env"] is None
        ):
            self.kwargs["env"] = {}
        return self.kwargs["env"]

    @env.setter
    def env(self, value):
        if not isinstance(value, dict):
            raise ValueError(
                "'env' attribute expect 'dict' object. Got: {}".format(
                    str(type(value))
                )
            )
        self.kwargs["env"] = value

    @property
    def modules_manager(self):
        """
        Deprecated:
            Use 'addons_manager' instead.

        """
        return self.addons_manager

    def _collect_addons_launch_hook_paths(self):
        """Helper to collect application launch hooks from addons.

        Module have to have implemented 'get_launch_hook_paths' method which
        can expect application as argument or nothing.

        Returns:
            List[str]: Paths to launch hook directories.
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
                self.log.warning((
                    "Result of `get_launch_hook_paths`"
                    " has invalid type {}. Expected {}"
                ).format(type(hook_paths), expected_types))
                continue

            output.extend(hook_paths)
        return output

    def paths_to_launch_hooks(self):
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

    def discover_launch_hooks(self, force=False):
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
            "\n".join("- {}".format(path) for path in paths)
        ))

        all_classes = {
            "pre": [],
            "post": []
        }
        for path in paths:
            if not os.path.exists(path):
                self.log.info(
                    "Path to launch hooks does not exist: \"{}\"".format(path)
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
                            "Skipped hook invalid for current launch context: "
                            "{}".format(klass.__name__)
                        )
                        continue

                    if inspect.isabstract(hook):
                        self.log.debug("Skipped abstract hook: {}".format(
                            klass.__name__
                        ))
                        continue

                    # Separate hooks by pre/post class
                    if hook.order is None:
                        hooks_without_order.append(hook)
                    else:
                        hooks_with_order.append(hook)

                except Exception:
                    self.log.warning(
                        "Initialization of hook failed: "
                        "{}".format(klass.__name__),
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

        self.log.debug("Found {} prelaunch and {} postlaunch hooks.".format(
            len(self.prelaunch_hooks), len(self.postlaunch_hooks)
        ))

    @property
    def app_name(self):
        return self.application.name

    @property
    def host_name(self):
        return self.application.host_name

    @property
    def app_group(self):
        return self.application.group

    @property
    def manager(self):
        return self.application.manager

    def _run_process(self):
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
        # create temporaty file path passed to midprocess
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
        json_temp = tempfile.NamedTemporaryFile(
            mode="w", prefix="ay_app_args", suffix=".json", delete=False
        )
        json_temp.close()
        json_temp_filepath = json_temp.name
        with open(json_temp_filepath, "w") as stream:
            json.dump(json_data, stream)

        launch_args.append(json_temp_filepath)

        # Create mid-process which will launch application
        process = subprocess.Popen(launch_args, **self.kwargs)
        # Wait until the process finishes
        #   - This is important! The process would stay in "open" state.
        process.wait()

        # read back pid from the json file
        try:
            with open(json_temp_filepath, "r") as stream:
                json_data = json.load(stream)

                process_info = ProcessInfo(
                    name=self.application.full_name,
                    args=self.launch_args,
                    env=self.kwargs.get("env", {}),
                    cwd=os.getcwd(),
                    pid=json_data.get("pid"),
                    output=Path(temp_file.name),
                    site_id=get_local_site_id()
                )
                # Store process info to the database
                self.manager.store_process_info(process_info)
        except OSError as e:
            self.log.error(
                "Failed to read process info from JSON file: %s", e,
                exc_info=True
            )

        # Remove the temp file
        os.remove(json_temp_filepath)
        # Return process which is already terminated
        return process

    def _execute_with_stdout(self):
        """Run the process with stdout and stderr redirected to a file.

        Stores process information to the database.

        Returns:
            subprocess.Popen: The process object created by Popen.
        """
        temp_file = tempfile.NamedTemporaryFile(
            mode="w",
            prefix=f"ayon_{self.application.host_name}_output_",
            suffix=".txt",
            delete=False
        )
        temp_file_path = temp_file.name
        temp_file.close()
        with open(temp_file_path, "w") as tmp_file:
            self.kwargs["stdout"] = tmp_file
            self.kwargs["stderr"] = tmp_file
            process = subprocess.Popen(self.launch_args, **self.kwargs)

            process_info = ProcessInfo(
                name=self.application.full_name,
                args=self.launch_args,
                env=self.kwargs.get("env", {}),
                cwd=os.getcwd(),
                pid=process.pid,
                output=Path(temp_file_path),
                site_id=get_local_site_id()
            )
            # Store process info to the database
            self.manager.store_process_info(process_info)

        return process

    def run_prelaunch_hooks(self):
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
        for prelaunch_hook in self.prelaunch_hooks:
            self.log.debug("Executing prelaunch hook: {}".format(
                str(prelaunch_hook.__class__.__name__)
            ))
            prelaunch_hook.execute()
        self._prelaunch_hooks_executed = True

    def launch(self):
        """Collect data for new process and then create it.

        This method must not be executed more than once.

        Returns:
            subprocess.Popen: Created process as Popen object.
        """
        if self.process is not None:
            self.log.warning("Application was already launched.")
            return

        if not self._prelaunch_hooks_executed:
            self.run_prelaunch_hooks()

        self.log.debug("All prelaunch hook executed. Starting new process.")

        # Prepare subprocess args
        args_len_str = ""
        if isinstance(self.launch_args, str):
            args = self.launch_args
        else:
            args = self.clear_launch_args(self.launch_args)
            args_len_str = " ({})".format(len(args))
        self.log.info(
            "Launching \"{}\" with args{}: {}".format(
                self.application.full_name, args_len_str, args
            )
        )
        self.launch_args = args

        # Run process
        self.process = self._run_process()

        # Process post launch hooks
        for postlaunch_hook in self.postlaunch_hooks:
            self.log.debug("Executing postlaunch hook: {}".format(
                str(postlaunch_hook.__class__.__name__)
            ))

            # TODO how to handle errors?
            # - store to variable to let them accessible?
            try:
                postlaunch_hook.execute()

            except Exception:
                self.log.warning(
                    "After launch procedures were not successful.",
                    exc_info=True
                )

        self.log.debug("Launch of {} finished.".format(
            self.application.full_name
        ))

        return self.process

    @staticmethod
    def clear_launch_args(args):
        """Collect launch arguments to final order.

        Launch argument should be list that may contain another lists this
        function will upack inner lists and keep ordering.

        ```
        # source
        [ [ arg1, [ arg2, arg3 ] ], arg4, [arg5, arg6]]
        # result
        [ arg1, arg2, arg3, arg4, arg5, arg6]

        Args:
            args (list): Source arguments in list may contain inner lists.

        Return:
            list: Unpacked arguments.
        """
        if isinstance(args, str):
            return args
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

