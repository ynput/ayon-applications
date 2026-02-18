"""Loader action to open source workfiles in Tray Browser."""
from __future__ import annotations
import os
from typing import Optional, Any, TYPE_CHECKING

import ayon_api
from ayon_core.addon import IHostAddon, AddonsManager
from ayon_core.pipeline.actions import (
    LoaderSimpleActionPlugin,
    LoaderActionSelection,
    LoaderActionResult,
)
from ayon_core.style import load_stylesheet
from qtpy import QtWidgets, QtCore

if TYPE_CHECKING:
    from ayon_applications.manager import Application


class OpenSourceWorkfileAction(LoaderSimpleActionPlugin):
    """Open source workfile in its host DCC application."""

    label = "Open Source Workfile"
    order = 5
    group_label = None
    icon = {
        "type": "material-symbols",
        "name": "rocket_launch",
        "color": "#d8d8d8",
    }

    # TODO: Allow to customize in settings whether this action is enabled
    # TODO: Allow to customize for which extensions or product types this
    #  action is available.

    def is_compatible(self, selection: LoaderActionSelection) -> bool:
        """Check if any selected version has source workfile."""
        # Only allow if no registered host, like in standalone browser
        if self.host_name:
            return False

        if not selection.versions_selected():
            return False

        for version in selection.get_selected_version_entities():
            if version.get("attrib", {}).get("source"):
                return True
        return False

    def execute_simple_action(
            self,
            selection: LoaderActionSelection,
            form_values: dict[str, Any],
    ) -> Optional[LoaderActionResult]:
        """Open source workfile in DCC application."""
        versions = selection.get_selected_version_entities()
        version = versions[0] if versions else None

        if not version:
            return LoaderActionResult(
                "No version selected",
                success=False,
            )

        source_path = version.get("attrib", {}).get("source")
        if not source_path:
            return LoaderActionResult(
                "This version doesn't have source workfile information.",
                success=False,
            )

        workfile_name = os.path.basename(source_path)
        file_ext = os.path.splitext(workfile_name)[1].lower()
        if not file_ext:
            return LoaderActionResult(
                f"Version source '{workfile_name}' has no extension.",
                success=False,
            )

        # Get compatible applications
        addons_manager = self._context.get_addons_manager()
        compatible_apps = self._get_compatible_apps(
            addons_manager, file_ext
        )

        if not compatible_apps:
            return LoaderActionResult(
                f"No compatible applications found for {file_ext}",
                success=False,
            )

        # Check the source app that was used to create the publish
        source_app_full_name = version.get("data", {}).get("ayon_app_name")

        anatomy = selection.get_project_anatomy()
        workfile_path: str = anatomy.fill_root(source_path)
        if not os.path.exists(workfile_path):
            return LoaderActionResult(
                f"Source workfile does not exist at '{workfile_path}'",
                success=False,
            )

        # Show selection dialog
        selected_app = self._show_app_dialog(
            compatible_apps, workfile_name, selection.project_name,
            source_app_full_name=source_app_full_name
        )

        if not selected_app:
            return LoaderActionResult("Cancelled", success=False)

        # Launch application
        try:
            self._launch_app(
                selected_app,
                version,
                source_path,
                selection.project_name,
                addons_manager,
                anatomy,
            )
            return LoaderActionResult(
                f"<b>{selected_app['label']}</b> launched with "
                f"<b>{workfile_name}</b>",
                success=True,
            )
        except Exception as e:
            self.log.error(f"Failed to launch: {e}", exc_info=True)
            return LoaderActionResult(
                f"Failed to launch application:\n{str(e)}",
                success=False,
            )

    def _get_compatible_apps(self, addons_manager, file_ext):
        """Get compatible applications for file extension."""

        # For each host addon find the relevant supported extensions
        host_names: set[str] = set()
        for addon in addons_manager.addons:
            if not isinstance(addon, IHostAddon):
                continue

            try:
                # Ignore issues if an addon happens to have a
                # broken implementation
                extensions = addon.get_workfile_extensions()
            except Exception:
                self.log.error(
                    f"Failed to get workfile extensions for addon: {addon}",
                    exc_info=True
                )
                continue

            host_name: str = addon.host_name
            if file_ext in extensions:
                host_names.add(host_name)

        if not host_names:
            return []

        # Find the applications matching the host names
        apps_addon = addons_manager.get("applications")
        if not apps_addon:
            return []

        app_manager = apps_addon.get_applications_manager()
        compatible = []
        for app_name, app in app_manager.applications.items():
            if app.host_name not in host_names:
                continue

            # TODO: Filter to applications that are available in the given
            #  source context instead of listing all Studio Settings
            #  applications.

            try:
                exe = app.find_executable()
            except Exception:
                continue

            if exe and os.path.exists(str(exe)):
                compatible.append(app)

        return compatible

    def _show_app_dialog(
            self,
            apps: list["Application"],
            workfile_name: str,
            project_name: str,
            source_app_full_name: Optional[str] = None):
        """Show application selection dialog."""
        dialog = QtWidgets.QDialog()
        dialog.setWindowTitle("Open Source Workfile")
        dialog.setMinimumWidth(400)
        dialog.setStyleSheet(load_stylesheet())

        layout = QtWidgets.QVBoxLayout(dialog)

        info = QtWidgets.QLabel(
            f"<h3>Open Source Workfile</h3>"
            f"<p><b>Workfile:</b> {workfile_name}</p>"
            f"<p><b>Project:</b> {project_name}</p>"
        )
        info.setTextFormat(QtCore.Qt.RichText)
        layout.addWidget(info)

        app_list = QtWidgets.QListWidget()
        preferred_index = 0
        for i, app in enumerate(apps):
            label = app.full_label or app.name

            # Highlight the app that was used to create the publish so that
            # the user knows it's the recommended one to open with.
            if source_app_full_name and app.full_name == source_app_full_name:
                preferred_index = i
                label += " (used to create publish)"

            # TODO: Show application icon if available
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.UserRole, app)
            app_list.addItem(item)

        # Preselect the first entry or the one matching the source app
        app_list.setCurrentRow(preferred_index)

        layout.addWidget(app_list)

        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QtWidgets.QPushButton("Cancel")
        open_btn = QtWidgets.QPushButton("Open")
        open_btn.setDefault(True)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(open_btn)
        layout.addLayout(btn_layout)

        open_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        app_list.itemDoubleClicked.connect(dialog.accept)

        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            item = app_list.currentItem()
            if not item:
                return None

            return item.data(QtCore.Qt.UserRole)
        return None

    def _launch_app(
        self,
        app: "Application",
        version: dict,
        workfile_path: str,
        project_name: str,
        addons_manager: AddonsManager,
    ):
        """Launch application with workfile."""

        # This incorrectly assumes that the workfile lives in the same context
        # as the published product. This may not always be the case, because
        # e.g. a product may be published from a different task or folder.
        # TODO: Correctly resolve the context of the source workfile.
        product_id = version["productId"]
        task_id = version.get("taskId")

        product = ayon_api.get_product_by_id(project_name, product_id)
        folder = ayon_api.get_folder_by_id(project_name, product["folderId"])
        task = ayon_api.get_task_by_id(project_name,
                                       task_id) if task_id else None

        # TODO: Launch should perhaps not go through addon directly
        #  but go through AYON subprocess to ensure correct bundle
        #  is also initialized for e.g. per-project bundles, etc.
        apps_addon = addons_manager["applications"]
        apps_addon.launch_application(
            app_name=app.full_name,
            project_name=project_name,
            folder_path=folder["path"],
            task_name=task["name"] if task else None,
            workfile_path=workfile_path,
            use_last_workfile=False,
        )
