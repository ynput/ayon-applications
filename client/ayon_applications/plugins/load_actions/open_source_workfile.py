"""Loader action to open source workfiles in Tray Browser."""
from __future__ import annotations
import os
from typing import Optional, Any

import ayon_api
from ayon_core.addon import IHostAddon, AddonsManager
from ayon_core.pipeline.actions import (
    LoaderSimpleActionPlugin,
    LoaderActionSelection,
    LoaderActionResult,
)
from ayon_core.lib import run_ayon_launcher_process
from ayon_applications.ui.debug_terminal_launch import choose_app


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
            source = version.get("attrib", {}).get("source")

            if not source:
                return False

            if source.startswith("{root"):
                return True

            elif os.path.exists(source):
                # Assume it's a valid source workfile
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
        task_id = version.get("taskId")
        project_name = selection.project_name
        addons_manager = self.get_addons_manager()
        compatible_apps = self._get_compatible_apps(
            addons_manager,
            file_ext=file_ext,
            project_name=project_name,
            task_id=task_id
        )
        if not compatible_apps:
            return LoaderActionResult(
                f"No compatible applications found for {file_ext}",
                success=False,
            )
        apps_addon = addons_manager["applications"]
        selected_app_name = choose_app(apps_addon, compatible_apps)

        anatomy = selection.get_project_anatomy()
        workfile_path: str = anatomy.fill_root(source_path)
        if not os.path.exists(workfile_path):
            return LoaderActionResult(
                f"Source workfile does not exist at '{workfile_path}'",
                success=False,
            )

        if not selected_app_name:
            return LoaderActionResult("Cancelled", success=False)

        selected_app = next(
            app for app in compatible_apps
            if app.full_name == selected_app_name
        )

        # Launch application
        try:
            product_id = version["productId"]
            product = ayon_api.get_product_by_id(project_name, product_id)
            folder = ayon_api.get_folder_by_id(
                project_name, product["folderId"]
            )
            task = (
                ayon_api.get_task_by_id(project_name, task_id)
                if task_id else None
            )
            run_ayon_launcher_process(
                "addon", "applications", "launch",
                "--project", project_name,
                "--folder", folder["path"],
                "--task", task["name"] if task else None,
                "--app", selected_app.full_name,
                "--workfile-path", workfile_path,
                "--use-last-workfile", "0",
            )
            return LoaderActionResult(
                f"<b>{selected_app.full_label or selected_app.label}</b> "
                f"launched with <b>{workfile_name}</b>",
                success=True,
            )
        except Exception as e:
            self.log.error(f"Failed to launch: {e}", exc_info=True)
            return LoaderActionResult(
                f"Failed to launch application:\n{str(e)}",
                success=False,
            )

    def _get_compatible_apps(
        self,
        addons_manager,
        file_ext,
        project_name,
        task_id,
    ) -> list[Any]:
        """Get compatible applications for file extension."""

        # 1) host names that can open this extension
        host_names: set[str] = set()
        for addon in addons_manager.addons:
            if not isinstance(addon, IHostAddon):
                continue

            try:

                extensions = addon.get_workfile_extensions()
            except Exception:
                self.log.error(
                    f"Failed to get workfile extensions for addon: {addon}",
                    exc_info=True,
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

        app_items = apps_addon.get_application_items(
            project_name,
            task_id=task_id,
            version=apps_addon.version
        )

        allowed_names = {
            item["full_name"]
            for item in app_items
            if item.get("host_name") in host_names
        }
        if not allowed_names:
            return []

        # 3) map back to Application objects for choose_app and launch flow
        app_manager = apps_addon.get_applications_manager()
        compatible = []
        for app in app_manager.applications.values():
            if app.full_name not in allowed_names:
                continue

            try:
                exe = app.find_executable()
            except Exception:
                continue

            if exe and os.path.exists(str(exe)):
                compatible.append(app)

        return compatible
