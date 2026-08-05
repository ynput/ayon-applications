"""Loader action to open source workfiles in Tray Browser."""
from __future__ import annotations
import os
import collections
from typing import Optional, Any, TYPE_CHECKING

from ayon_core.addon import IHostAddon
from ayon_core.pipeline.actions import (
    LoaderSimpleActionPlugin,
    LoaderActionSelection,
    LoaderActionResult,
)
from ayon_core.lib import run_detached_ayon_launcher_process
from ayon_applications.ui.debug_terminal_launch import choose_app
from ayon_applications import ApplicationGroup

if TYPE_CHECKING:
    from ayon_applications import Application
    from ayon_applications.manager import ApplicationManager


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
            if not version["taskId"]:
                continue

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
        task_id = version["taskId"]
        project_name = selection.project_name
        addons_manager = self._context.get_addons_manager()
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
            (app for app in compatible_apps
             if app.full_name == selected_app_name),
            None
        )

        if not selected_app:
            return LoaderActionResult(
                f"Selected application '{selected_app_name}' was not found.",
                success=False,
            )
        # Launch application
        run_detached_ayon_launcher_process(
            "addon", "applications", "launch-by-id",
            "--project", project_name,
            "--task-id", version["taskId"],
            "--app", selected_app.full_name,
            "--workfile-path", workfile_path,
            "--use-last-workfile", "0",
        )

    def _get_compatible_apps(
        self,
        addons_manager,
        file_ext,
        project_name,
        task_id,
    ) -> list[Any]:
        """Get compatible applications for file extension."""

        # Find the applications matching the host names
        apps_addon = addons_manager.get("applications")
        if not apps_addon:
            return []
        # host names that can open this extension
        # NOTE: Does not respect project bundle addons.
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

        app_items = apps_addon.get_application_items(
            project_name,
            task_id=task_id,
        )

        app_manager = apps_addon.get_applications_manager()

        return self._create_fake_applications(
            app_manager,
            app_items,
            host_names
        )

    def _create_fake_applications(
            self,
            app_manager: ApplicationManager,
            app_items: list[dict[str, Any]],
            host_names: set[str]) -> list[Application]:
        """Fake application objects representing compatible applications.

        Args:
            app_manager (ApplicationManager): Applications manager
                instance from applications addon
            app_items (list[dict[str, Any]]): Application items from
            applications addon
            host_names (set[str]): host names that can open the source workfile

        Returns:
            list[Application]: Fake application objects representing
                compatible applications.
        """
        app_items_by_group = collections.defaultdict(list)
        for app_item in app_items:
            if app_item["host_name"] not in host_names:
                continue
            full_name = app_item["full_name"]
            group_name = full_name.split("/")[0]
            app_items_by_group[group_name].append(app_item)

        output = []
        for group_name, group_app_items in app_items_by_group.items():
            host_name = None
            variants = []
            for app_item in group_app_items:
                variants.append({
                    "name": app_item["full_name"].split("/")[1],
                    "label": app_item["variant_label"],
                    "environment": "{}",
                    "arguments": [],
                    "executables": {},
                })
            group = ApplicationGroup(
                group_name,
                {
                    "enabled": True,
                    "environment": "{}",
                    "host_name": host_name,
                    "variants": variants,
                },
                app_manager
            )
            for variant in group.variants.values():
                output.append(variant)
        return output
