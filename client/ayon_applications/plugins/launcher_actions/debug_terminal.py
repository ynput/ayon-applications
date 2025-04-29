import os
from typing import Optional

from qtpy import QtWidgets, QtGui, QtCore

from ayon_applications import (
    Application,
    ApplicationManager
)

from ayon_applications.utils import (
    get_app_environments_for_context,
    get_applications_for_context,
    get_app_icon_path
)
from ayon_core.pipeline.actions import LauncherActionSelection
from ayon_core.pipeline import LauncherAction
from ayon_core.style import load_stylesheet
from ayon_core.tools.utils.lib import get_qt_icon


def get_application_qt_icon(application: Application) -> Optional[QtGui.QIcon]:
    """Return QtGui.QIcon for an Application"""
    icon = application.icon
    if not icon:
        return QtGui.QIcon()
    icon_filepath = get_app_icon_path(icon)
    if icon_filepath and os.path.exists(icon_filepath):
        return get_qt_icon({"type": "path", "path": icon_filepath})
    return QtGui.QIcon()


class DebugTerminal(LauncherAction):
    """Run any host environment in command line terminal."""
    name = "debugterminal"
    label = "Terminal"
    icon = {
        "type": "awesome-font",
        "name": "fa.terminal",
        "color": "#e8770e"
    }
    order = 10

    def is_compatible(self, selection) -> bool:
        return selection.is_task_selected

    def process(self, selection, **kwargs):
        # Get cursor position directly so the menu shows closer to where user
        # clicked because the get applications logic might take a brief moment
        pos = QtGui.QCursor.pos()
        application_manager = ApplicationManager()

        # Choose terminal
        terminal_applications = self.get_terminal_applications(
            application_manager)
        if len(terminal_applications) == 0:
            raise ValueError(
                "Missing application variants for terminal application. "
                "Please configure "
                "'ayon+settings://applications/applications/terminal'"
            )
        elif len(terminal_applications) == 1:
            # If only one configured shell application, always use that one
            terminal_app = terminal_applications[0]
            print("Only one terminal application variant is configured. "
                  f"Defaulting to {terminal_app.full_label}")
        else:
            terminal_app = self.choose_app(
                terminal_applications, pos, show_variant_name_only=True)
        if not terminal_app:
            return

        # Get applications
        applications = self.get_project_applications(
            application_manager, selection)
        app = self.choose_app(applications, pos)
        if not app:
            return

        print(f"Retrieving environment for: {app.full_label}..")
        env = get_app_environments_for_context(selection.project_name,
                                               selection.folder_path,
                                               selection.task_name,
                                               app.full_name)

        # If an executable is found. Then add the parent folder to PATH
        # just so we can run the application easily from the command line.
        exe = app.find_executable()
        if exe:
            exe_path = exe._realpath()
            folder = os.path.dirname(exe_path)
            print(f"Appending to PATH: {folder}")
            env["PATH"] += os.pathsep + folder

        cwd = env.get("AYON_WORKDIR")
        if cwd:
            print(f"Setting Work Directory: {cwd}")

        print(f"Launching terminal in environment of {app.full_label}..")
        self.launch_terminal_with_app_context(
            application_manager,
            terminal_app,
            project_name=selection.project_name,
            folder_path=selection.folder_path,
            task_name=selection.task_name,
            env=env,
            cwd=cwd)

    @staticmethod
    def choose_app(
        applications: list[Application],
        pos: QtCore.QPoint,
        show_variant_name_only: bool = False
    ) -> Optional[Application]:
        """Show menu to choose from list of applications"""
        menu = QtWidgets.QMenu()
        menu.setAttribute(QtCore.Qt.WA_DeleteOnClose)  # force garbage collect
        menu.setStyleSheet(load_stylesheet())

        # Sort applications
        applications.sort(key=lambda item: item.full_label)

        for app in applications:
            label = app.label if show_variant_name_only else app.full_label
            menu_action = QtWidgets.QAction(label, parent=menu)
            icon = get_application_qt_icon(app)
            if icon:
                menu_action.setIcon(icon)
            menu_action.setData(app)
            menu.addAction(menu_action)

        result = menu.exec_(pos)
        if result:
            return result.data()

    @staticmethod
    def get_project_applications(
            application_manager: ApplicationManager,
            selection: LauncherActionSelection) -> list[Application]:
        """Return the enabled applications for the project"""

        application_names = get_applications_for_context(
            project_name=selection.project_name,
            folder_entity=selection.folder_entity,
            task_entity=selection.task_entity,
            project_settings=selection.get_project_settings(),
            project_entity=selection.project_entity
        )

        # Filter to apps valid for this current project, with logic from:
        # `ayon_core.tools.launcher.models.actions.ApplicationAction.is_compatible`  # noqa
        applications = []
        for app_name in application_names:
            app = application_manager.applications.get(app_name)
            if not app or not app.enabled:
                continue
            applications.append(app)

        return applications

    @staticmethod
    def get_terminal_applications(application_manager) -> list[Application]:
        """Return all configured terminal applications"""
        # TODO: Maybe filter out terminal applications not configured for your
        #  current platform
        return list(
            application_manager.app_groups["terminal"].variants.values())

    def launch_terminal_with_app_context(
        self,
        application_manager: ApplicationManager,
        application: Application,
        project_name: str,
        folder_path: str,
        task_name: str,
        cwd: str,
        env: dict[str, str]
    ) -> list[str]:
        """Return the terminal executable to launch."""
        # TODO: Allow customization per user for this via AYON settings
        launch_context = application_manager.create_launch_context(
            application.full_name,
            project_name=project_name,
            folder_path=folder_path,
            task_name=task_name,
            env=env
        )
        launch_context.kwargs["cwd"] = cwd
        return application_manager.launch_with_context(launch_context)
