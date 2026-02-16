from __future__ import annotations

import os
import sys
import json
import traceback
import tempfile
import warnings
import typing
from typing import Optional, Any

import ayon_api

from ayon_core.lib import (
    run_ayon_launcher_process,
    is_headless_mode_enabled,
    env_value_to_bool,
)
from ayon_core.addon import (
    AYONAddon,
    IPluginPaths,
    ITrayAction,
    click_wrap,
    ensure_addons_are_process_ready,
)

from .version import __version__
from .constants import APPLICATIONS_ADDON_ROOT
from .defs import LaunchTypes, GroupAppInfo
from .manager import ApplicationManager
from .exceptions import (
    ApplicationLaunchFailed,
    ApplicationExecutableNotFound,
    ApplicationNotFound,
)
from .utils import get_app_icon_path

if typing.TYPE_CHECKING:
    from typing import Literal

    BoolArg = Literal["1", "0"]
    from ayon_applications.manager import Application
    from ayon_core.tools.tray.webserver import WebServerManager
    from ayon_applications.ui.process_monitor import ProcessMonitorWindow


class ApplicationsAddon(AYONAddon, IPluginPaths, ITrayAction):

    name = "applications"
    version = __version__
    admin_action = True

    label = "Process Monitor"

    _icons_cache: dict[str, GroupAppInfo] = {}
    _app_groups_info_cache = None

    @classmethod
    def get_app_group_info(cls, group_name: str) -> Optional[GroupAppInfo]:
        """Get info about application group.

        Output contains only constant group information from server. Does not
            respect settings.

        Args:
            group_name (str): Application name.

        Returns:
            Optional[GroupAppInfo]: Application group info.

        """
        app_groups_info = cls._get_app_groups_info()
        return app_groups_info.get(group_name)

    @classmethod
    def get_app_label(cls, group_name: str) -> str:
        """Get label for application group by name.

        Args:
            group_name (str): Application name.

        Returns:
            str: Application label.

        """
        app_group_info = cls.get_app_group_info(group_name)
        if app_group_info is None:
            return group_name
        return app_group_info.label

    @classmethod
    def get_app_icon(cls, group_name: str) -> Optional[str]:
        """Get icon for application group by name.

        Args:
            group_name (str): Application name.

        Returns:
            Optional[str]: Application icon filename.

        """
        app_group_info = cls.get_app_group_info(group_name)
        if app_group_info is None:
            return None
        return app_group_info.icon

    def tray_init(self) -> None:
        """Initialize the tray action."""
        self._process_monitor_window: Optional[ProcessMonitorWindow] = None

    def on_action_trigger(self) -> None:
        """Action triggered when the tray icon is clicked."""
        from ayon_applications.ui.process_monitor import (
            ProcessMonitorWindow,
        )
        if self._process_monitor_window is None:
            self._process_monitor_window = ProcessMonitorWindow()

        self._process_monitor_window.show()
        self._process_monitor_window.raise_()
        self._process_monitor_window.activateWindow()

    def get_app_environments_for_context(
        self,
        project_name: str,
        folder_path: str,
        task_name: str,
        full_app_name: str,
        env_group: Optional[str] = None,
        launch_type: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> dict[str, str]:
        """Calculate environment variables for launch context.

        Args:
            project_name (str): Project name.
            folder_path (str): Folder path.
            task_name (str): Task name.
            full_app_name (str): Full application name.
            env_group (Optional[str]): Environment group.
            launch_type (Optional[str]): Launch type.
            env (Optional[dict[str, str]]): Environment variables to update.

        Returns:
            dict[str, str]: Environment variables for context.

        """
        from ayon_applications.utils import get_app_environments_for_context

        if not full_app_name:
            return {}

        return get_app_environments_for_context(
            project_name,
            folder_path,
            task_name,
            full_app_name,
            env_group=env_group,
            launch_type=launch_type,
            env=env,
            addons_manager=self.manager
        )

    def get_farm_publish_environment_variables(
        self,
        project_name: str,
        folder_path: str,
        task_name: str,
        full_app_name: Optional[str] = None,
        env_group: Optional[str] = None,
    ) -> dict[str, str]:
        """Calculate environment variables for farm publish.

        Args:
            project_name (str): Project name.
            folder_path (str): Folder path.
            task_name (str): Task name.
            env_group (Optional[str]): Environment group.
            full_app_name (Optional[str]): Full application name. Value from
                environment variable 'AYON_APP_NAME' is used if 'None' is
                passed.

        Returns:
            dict[str, str]: Environment variables for farm publish.

        """
        if full_app_name is None:
            full_app_name = os.getenv("AYON_APP_NAME")

        return self.get_app_environments_for_context(
            project_name,
            folder_path,
            task_name,
            full_app_name,
            env_group=env_group,
            launch_type=LaunchTypes.farm_publish
        )

    def get_applications_manager(
        self, settings: Optional[dict[str, Any]] = None
    ) -> "ApplicationManager":
        """Get applications manager.

        Args:
            settings (Optional[dict]): Studio/project settings.

        Returns:
            ApplicationManager: Applications manager.

        """
        return ApplicationManager(settings)

    def get_plugin_paths(self) -> dict[str, list[str]]:
        return {}

    def get_publish_plugin_paths(self, host_name: str) -> list[str]:
        return [
            os.path.join(APPLICATIONS_ADDON_ROOT, "plugins", "publish")
        ]

    def get_launch_hook_paths(self, app: "Application") -> list[str]:
        return [
            os.path.join(APPLICATIONS_ADDON_ROOT, "hooks")
        ]

    def get_app_icon_path(self, icon_filename: str) -> str:
        """DEPRECATED Get icon path.

        Args:
            icon_filename (str): Icon filename.

        Returns:
            Optional[str]: Icon path or None if not found.

        """
        return get_app_icon_path(icon_filename)

    def get_app_icon_url(
        self, icon_filename: str, server: bool = False
    ) -> Optional[str]:
        """Get icon path.

        Method does not validate if icon filename exist on server.

        Args:
            icon_filename (str): Icon name.
            server (Optional[bool]): Return url to AYON server.

        Returns:
            Union[str, None]: Icon path or None is server url is not
                available.

        """
        if not icon_filename:
            return None
        icon_name = os.path.basename(icon_filename)
        if server:
            base_url = ayon_api.get_base_url()
            return (
                f"{base_url}/api/addons/{self.name}/{self.version}"
                f"/icons/{icon_name}"
            )
        server_url = os.getenv("AYON_WEBSERVER_URL")
        if not server_url:
            return None
        return "/".join([
            server_url, "addons", self.name, "icons", icon_name
        ])

    def launch_application(
        self,
        app_name: str,
        project_name: str,
        folder_path: str,
        task_name: str,
        workfile_path: Optional[str] = None,
        use_last_workfile: Optional[bool] = None,
    ):
        """Launch application.

        Args:
            app_name (str): Full application name e.g. 'maya/2024'.
            project_name (str): Project name.
            folder_path (str): Folder path.
            task_name (str): Task name.
            workfile_path (Optional[str]): Workfile path to use.
            use_last_workfile (Optional[bool]): Explicitly tell to use or
                not use last workfile. Ignored if 'workfile_path' is passed.

        """
        ensure_addons_are_process_ready(
            addon_name=self.name,
            addon_version=self.version,
            project_name=project_name,
        )
        headless = is_headless_mode_enabled()

        data = {
            "project_name": project_name,
            "folder_path": folder_path,
            "task_name": task_name,
        }
        # Backwards compatibility 'workfile_path' was added
        #   before 'use_last_workfile'
        if isinstance(workfile_path, bool):
            use_last_workfile = workfile_path
            workfile_path = None
            warnings.warn(
                "Passed 'use_last_workfile' as positional argument."
                " Use explicit 'use_last_workfile' keyword argument instead.",
                DeprecationWarning,
                stacklevel=2,
            )

        if workfile_path:
            data["workfile_path"] = workfile_path
            # Backwards compatibility to be able to use 'workfile_path'
            #   argument with older ayon-core
            # use_last_workfile = False
            data["last_workfile_path"] = workfile_path
            data["start_last_workfile"] = True

        elif use_last_workfile is not None:
            data["start_last_workfile"] = use_last_workfile

        # TODO handle raise errors
        failed = True
        message = None
        detail = None
        try:
            app_manager = self.get_applications_manager()
            app_manager.launch(app_name, **data)
            failed = False

        except (
            ApplicationLaunchFailed,
            ApplicationExecutableNotFound,
            ApplicationNotFound,
        ) as exc:
            message = str(exc)
            self.log.warning(f"Application launch failed: {message}")

        except Exception as exc:
            message = "An unexpected error happened"
            detail = "".join(traceback.format_exception(*sys.exc_info()))
            self.log.warning(
                f"Application launch failed: {str(exc)}",
                exc_info=True
            )

        if not failed:
            return

        if not headless:
            self._show_launch_error_dialog(message, detail)
        sys.exit(1)

    def webserver_initialization(self, manager: "WebServerManager") -> None:
        """Initialize webserver.

        Add localhost handler for icons requests.

        This was added for ftrack which is showing icons

        Args:
            manager (WebServerManager): Webserver manager.

        """

        async def _get_web_icon(request):
            from aiohttp import web, ClientSession

            filename = request.match_info["filename"]
            # TODO find better way how to cache
            if filename not in self.__class__._icons_cache:
                url = self.get_app_icon_url(filename, server=True)
                async with ClientSession() as session:
                    async with session.get(url) as resp:
                        assert resp.status == 200
                        data = await resp.read()

                self.__class__._icons_cache[filename] = data
            return web.Response(body=self.__class__._icons_cache[filename])

        manager.add_addon_route(
            self.name,
            "/icons/{filename}",
            "GET",
            _get_web_icon,
        )

    # --- CLI ---
    def cli(self, addon_click_group) -> None:
        main_group = click_wrap.group(
            self._cli_main, name=self.name, help="Applications addon"
        )
        (
            main_group.command(
                self._cli_extract_environments,
                name="extractenvironments",
                help=(
                    "Extract environment variables for context into json file"
                )
            )
            .argument("output_json_path")
            .option("--project", help="Project name", default=None)
            .option("--folder", help="Folder path", default=None)
            .option("--task", help="Task name", default=None)
            .option("--app", help="Full application name", default=None)
            .option(
                "--envgroup",
                help="Environment group (e.g. \"farm\")",
                default=None
            )
        )
        (
            main_group.command(
                self._cli_launch_context_names,
                name="launch",
                help="Launch application"
            )
            .option("--app", required=True, help="Full application name")
            .option("--project", required=True, help="Project name")
            .option("--folder", required=True, help="Folder path")
            .option("--task", required=True, help="Task name")
            .option(
                "--use-last-workfile",
                help="Use last workfile",
                default=None,
            )
        )
        (
            main_group.command(
                self._cli_launch_with_task_id,
                name="launch-by-id",
                help="Launch application"
            )
            .option("--app", required=True, help="Full application name")
            .option("--project", required=True, help="Project name")
            .option("--task-id", required=True, help="Task id")
            .option(
                "--use-last-workfile",
                help="Use last workfile",
                default=None,
            )
        )
        (
            main_group.command(
                self._cli_launch_with_workfile_id,
                name="launch-by-workfile-id",
                help="Launch application using workfile id"
            )
            .option("--app", required=True, help="Full application name")
            .option("--project", required=True, help="Project name")
            .option("--workfile-id", required=True, help="Workfile id")
        )
        (
            main_group.command(
                self._cli_launch_with_debug_terminal,
                name="launch-debug-terminal",
                help="Launch with debug terminal"
            )
            .option("--project", required=True, help="Project name")
            .option("--task-id", required=True, help="Task id")
            .option(
                "--app",
                required=False,
                help="Full application name",
                default=None,
            )
        )
        # Convert main command to click object and add it to parent group
        addon_click_group.add_command(
            main_group.to_click_obj()
        )

    def _cli_main(self) -> None:
        pass

    def _cli_extract_environments(
        self,
        output_json_path: str,
        project: str,
        folder: str,
        task: str,
        app: str,
        envgroup: str,
    ) -> None:
        """Produces json file with environment based on project and app.

        Called by farm integration to propagate environment into farm jobs.

        Args:
            output_json_path (str): Output json file path.
            project (str): Project name.
            folder (str): Folder path.
            task (str): Task name.
            app (str): Full application name e.g. 'maya/2024'.
            envgroup (str): Environment group.

        """
        if all((project, folder, task, app)):
            env = self.get_farm_publish_environment_variables(
                project, folder, task, app, env_group=envgroup,
            )
        else:
            env = os.environ.copy()

        output_dir = os.path.dirname(output_json_path)
        os.makedirs(output_dir, exist_ok=True)

        with open(output_json_path, "w") as file_stream:
            json.dump(env, file_stream, indent=4)

    def _cli_launch_context_names(
        self,
        project: str,
        folder: str,
        task: str,
        app: str,
        use_last_workfile: Optional["BoolArg"],
    ) -> None:
        """Launch application.

        Args:
            project (str): Project name.
            folder (str): Folder path.
            task (str): Task name.
            app (str): Full application name e.g. 'maya/2024'.
            use_last_workfile (Optional[Literal["1", "0"]): Explicitly tell
                to use last workfile.

        """
        if use_last_workfile is not None:
            use_last_workfile = env_value_to_bool(
                use_last_workfile, default=None
            )
        self.launch_application(
            app, project, folder, task, use_last_workfile=use_last_workfile,
        )

    def _cli_launch_with_task_id(
        self,
        project: str,
        task_id: str,
        app: str,
        use_last_workfile: Optional["BoolArg"],
    ) -> None:
        """Launch application using project name, task id and full app name.

        Args:
            project (str): Project name.
            task_id (str): Task id.
            app (str): Full application name e.g. 'maya/2024'.
            use_last_workfile (Optional[Literal["1", "0"]): Explicitly tell
                to use last workfile.

        """
        if use_last_workfile is not None:
            use_last_workfile = env_value_to_bool(
                value=use_last_workfile, default=None
            )

        task_entity = ayon_api.get_task_by_id(
            project, task_id, fields={"name", "folderId"}
        )
        folder_entity = ayon_api.get_folder_by_id(
            project, task_entity["folderId"], fields={"path"}
        )
        self.launch_application(
            app,
            project,
            folder_entity["path"],
            task_entity["name"],
            use_last_workfile=use_last_workfile,
        )

    def _cli_launch_with_workfile_id(
        self,
        project: str,
        workfile_id: str,
        app: str,
    ) -> None:
        from ayon_core.pipeline import Anatomy

        workfile_entity = ayon_api.get_workfile_info_by_id(
            project, workfile_id
        )
        task_id = workfile_entity["taskId"]
        task_entity = ayon_api.get_task_by_id(
            project, task_id, fields={"name", "folderId"}
        )
        folder_entity = ayon_api.get_folder_by_id(
            project, task_entity["folderId"], fields={"path"}
        )
        anatomy = Anatomy(project)
        workfile_path = anatomy.fill_root(workfile_entity["path"])
        self.launch_application(
            app,
            project,
            folder_entity["path"],
            task_entity["name"],
            workfile_path=workfile_path,
        )

    def _cli_launch_with_debug_terminal(
        self,
        project: str,
        task_id: str,
        app: Optional[str],
    ) -> None:
        from .ui.debug_terminal_launch import run_with_debug_terminal

        run_with_debug_terminal(self, project, task_id, app)

    def _show_launch_error_dialog(self, message: str, detail: str) -> None:
        script_path = os.path.join(
            APPLICATIONS_ADDON_ROOT, "ui", "launch_failed_dialog.py"
        )
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            tmp_path = tmp.name
            json.dump(
                {"message": message, "detail": detail},
                tmp.file
            )

        try:
            run_ayon_launcher_process(
                "--skip-bootstrap",
                script_path,
                tmp_path,
                add_sys_paths=True,
                creationflags=0,
            )

        finally:
            os.remove(tmp_path)

    @classmethod
    def _get_app_groups_info(cls) -> dict[str, GroupAppInfo]:
        if cls._app_groups_info_cache is None:
            response = ayon_api.get(
                f"addons/{cls.name}/{cls.version}/appGroupsInfo"
            )
            response.raise_for_status()
            cls._app_groups_info_cache = {
                key: GroupAppInfo(
                    name=key,
                    label=value["label"],
                    icon=value["icon"],
                )
                for key, value in response.data.items()
            }
        return cls._app_groups_info_cache
