"""Server side of the Applications addon.

This module contains the server side of the Applications addon.
It is responsible for managing settings and initial setup of addon.

## Attributes backward compatibility
Current and previous versions of applications addon did use AYON attributes
to define applications and tools for a project and task.

This system was replaced with a new system using settings. This change is
not 100% backwards compatible, we need to make sure that older versions of
the addon don't break initialization.

Older versions of the addon used settings of other versions, but
the settings structure did change which can cause that combination of old
and new Applications addon on server can cause crashes.

First version introduction settings does support both settings and attributes
so the handling of older versions is part of the addon, but following versions
have to find some clever way how to avoid the issues.

Version stored under 'ATTRIBUTES_VERSION_MILESTONE' should be last released
version that used only old attribute system.
"""
import os
from typing import Any
from typing import TYPE_CHECKING

import semver

from ayon_server.lib.postgres import Postgres
from ayon_server.logging import logger
from ayon_server.events import EventStream, EventModel
from ayon_server.addons import BaseServerAddon, AddonLibrary
from ayon_server.actions.config import set_action_config
from ayon_server.actions.context import ActionContext
from ayon_server.entities.core import attribute_library
from ayon_server.entities.user import UserEntity
from ayon_server.helpers.project_list import get_project_list

try:
    # Added in ayon-backend 1.8.0
    from ayon_server.utils import hash_data
except ImportError:
    import hashlib
    import json
    def hash_data(data):
        if not isinstance(data, str):
            data = json.dumps(data)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

if TYPE_CHECKING:
    from ayon_server.actions import (
        ActionExecutor,
        ExecuteResponseModel,
        SimpleActionManifest,
        DynamicActionManifest,
    )

from .constants import LABELS_BY_GROUP_NAME
from .settings import ApplicationsAddonSettings, DEFAULT_VALUES
from .actions import (
    get_action_manifests,
    get_dynamic_action_manifests,
    IDENTIFIER_PREFIX,
    IDENTIFIER_WORKFILE_PREFIX,
    DEBUG_TERMINAL_ID,
)

ATTRIBUTES_VERSION_MILESTONE = (1, 0, 0)

HOST_TO_EXT_MAPPING = {
    "aftereffects": {".aep"},
    "blender": {".blend"},
    "celaction": {".scn"},
    "cinema4d": {".c4d"},
    "equalizer": {".3de"},
    "flame": {".otoc"},
    "fusion": {".comp"},
    "hiero": {".hrox"},
    "houdini": {".hip", ".hiplc", ".hipnc"},
    "maya": {".ma", ".mb"},
    "max": {".max"},
    "marvelousdesigner": {".zprj"},
    "mochapro": {".mocha"},
    "motionbuilder": {".fbx"},
    "nuke": {".nk"},
    "photoshop": {".psd", ".psb"},
    "premiere": {".prproj"},
    "resolve": {".drp"},
    "silhouette": {".sfx"},
    "substancedesigner": {".sbs", ".sbsar", ".sbsasm"},
    "substancepainter": {".spp", ".toc"},
    "tvpaint": {".tvpp"},
    "zbrush": {".zpr"},
}
EXT_TO_HOST_MAPPING = {}
for host_name, extensions in HOST_TO_EXT_MAPPING.items():
    for extension in extensions:
        EXT_TO_HOST_MAPPING[extension] = host_name


def create_chunks(values: list, chunk_size=100):
    chunks = []
    if not values:
        return chunks
    iterable_size = len(values)
    for idx in range(0, iterable_size, chunk_size):
        chunks.append(values[idx:idx + chunk_size])
    return chunks


def parse_version(version):
    try:
        return semver.VersionInfo.parse(version)
    except ValueError:
        return None


def parse_versions(versions):
    version_objs = []
    invalid_versions = []
    output = (version_objs, invalid_versions)
    for version in versions:
        parsed_version = parse_version(version)
        if parsed_version is None:
            invalid_versions.append(version)
        else:
            version_objs.append((version, parsed_version))
    return output


class ApplicationsAddon(BaseServerAddon):
    settings_model = ApplicationsAddonSettings
    # TODO remove this attribute when attributes support is removed
    has_attributes = True

    def initialize(self):
        EventStream.subscribe(
            "bundle.updated",
            self._on_bundle_updated,
            all_nodes=True,
        )

    async def get_simple_actions(
        self,
        project_name: str | None = None,
        variant: str = "production",
    ) -> list["SimpleActionManifest"]:
        return await get_action_manifests(
            self,
            project_name=project_name,
            variant=variant,
        )

    async def get_dynamic_actions(
        self,
        context: ActionContext,
        variant: str = "production",
    ) -> list["DynamicActionManifest"]:
        return await get_dynamic_action_manifests(
            self,
            context=context,
            variant=variant,
        )

    async def execute_action(
        self,
        executor: "ActionExecutor",
    ) -> "ExecuteResponseModel":
        """Execute an action provided by the addon"""
        context = executor.context
        project_name = context.project_name
        entity_id = context.entity_ids[0]

        bundle_args = []
        if executor.variant not in ("production", "staging"):
            bundle_args = ["--bundle", executor.variant]

        if executor.identifier == DEBUG_TERMINAL_ID:
            args = [
                "addon", "applications", "launch-debug-terminal",
                "--project", project_name,
                "--task-id", entity_id,
            ]
            args.extend(bundle_args)
            return await executor.get_launcher_action_response(
                args=args
            )

        app_name = entity_id_arg = command = None
        skip_last_workfile = None
        if executor.identifier.startswith(IDENTIFIER_PREFIX):
            app_name = executor.identifier.removeprefix(IDENTIFIER_PREFIX)
            command = "launch-by-id"
            entity_id_arg = "--task-id"
            config = await self.get_action_config(
                executor.identifier,
                executor.context,
                executor.user,
                executor.variant,
            )
            skip_last_workfile = config.get("skip_last_workfile")

        elif executor.identifier.startswith(IDENTIFIER_WORKFILE_PREFIX):
            app_name = executor.identifier.removeprefix(
                IDENTIFIER_WORKFILE_PREFIX
            )
            command = "launch-by-workfile-id"
            entity_id_arg = "--workfile-id"

        if not app_name:
            return await executor.get_simple_response(
                message="Failed to launch application."
                " Unknown action identifier.",
                success=False,
            )

        args = [
            "addon", "applications", command,
            "--app", app_name,
            "--project", project_name,
            entity_id_arg, entity_id,
        ]
        args.extend(bundle_args)
        if skip_last_workfile is not None:
            args.extend([
                "--use-last-workfile", str(int(not skip_last_workfile))
            ])
        # 'get_launcher_response' is available since AYON 1.8.3
        if hasattr(executor, "get_launcher_response"):
            return await executor.get_launcher_response(args=args)
        # Backwards compatibility
        return await executor.get_launcher_action_response(args=args)

    async def get_default_settings(self):
        return self.get_settings_model()(**DEFAULT_VALUES)

    async def pre_setup(self):
        """Make sure older version of addon use the new way of attributes."""

        instance = AddonLibrary.getinstance()
        app_defs = instance.data.get(self.name)
        old_addon = app_defs.versions.get("0.1.0")
        if old_addon is not None:
            # Override 'create_applications_attribute' for older versions
            #   - avoid infinite server restart loop
            old_addon.create_applications_attribute = (
                self.create_applications_attribute
            )

        # Update older versions of applications addon to use new
        #   '_update_enums'
        # - new function skips newer addon versions without 'has_attributes'
        version_objs, _invalid_versions = parse_versions(app_defs.versions)
        for addon_version, version_obj in version_objs:
            # Last release with only old attribute system
            if version_obj < ATTRIBUTES_VERSION_MILESTONE:
                addon = app_defs.versions[addon_version]
                addon._update_enums = self._update_enums

    async def create_action_config_hash(
        self,
        identifier: str,
        context: ActionContext,
        user: UserEntity,
        variant: str,
    ) -> str:
        """Create a hash for action config store"""
        if not identifier.startswith(IDENTIFIER_PREFIX):
            return await super().create_action_config_hash(
                identifier, context, user, variant
            )

        # Change identifier to only app name and one task id
        identifier = identifier.removeprefix(IDENTIFIER_PREFIX)
        hash_content = [
            user.name,
            identifier,
            context.project_name,
            context.entity_ids[0],
        ]
        logger.trace(f"Creating config hash from {hash_content}")
        return hash_data(hash_content)

    async def set_action_config(
        self,
        identifier: str,
        context: ActionContext,
        user: UserEntity,
        variant: str,
        config: dict[str, Any],
    ) -> None:
        if not identifier.startswith(IDENTIFIER_PREFIX):
            await super().set_action_config(
                identifier, context, user, variant, config
            )
            return

        if not context.entity_ids:
            return

        # Unset 'skip_last_workfile' if it is set to 'False'
        if config.get("skip_last_workfile") is False:
            config.pop("skip_last_workfile")

        identifier = identifier.removeprefix(IDENTIFIER_PREFIX)
        for entity_id in context.entity_ids:
            config_hash = hash_data([
                user.name,
                identifier,
                context.project_name,
                entity_id,
            ])
            await set_action_config(
                config_hash,
                config,
                addon_name=self.name,
                addon_version=self.version,
                identifier=identifier,
                project_name=context.project_name,
                user_name=user.name,
            )

    async def convert_settings_overrides(
        self,
        source_version: str,
        overrides: dict[str, Any],
    ) -> dict[str, Any]:
        overrides = await super().convert_settings_overrides(
            source_version, overrides
        )
        # Since 1.0.0 the project applications and tools are
        #   using settings instead of attributes.
        # Disable automatically project applications and tools
        #   when converting settings of version < 1.0.0 so we don't break
        #   productions on update
        if parse_version(source_version) < (1, 0, 0):
            prj_apps = overrides.setdefault("project_applications", {})
            prj_apps["enabled"] = False
            prj_tools = overrides.setdefault("project_tools", {})
            prj_tools["enabled"] = False
        return overrides

    # --------------------------------------
    # Backwards compatibility for attributes
    # --------------------------------------
    def _sort_versions(self, addon_versions, reverse=False):
        version_objs, invalid_versions = parse_versions(addon_versions)

        valid_versions = [
            addon_version
            for addon_version, version_obj in (
                sorted(version_objs, key=lambda x: x[1])
            )
        ]
        sorted_versions = list(sorted(invalid_versions)) + valid_versions
        if reverse:
            sorted_versions = reversed(sorted_versions)
        for addon_version in sorted_versions:
            yield addon_version

    def _merge_groups(self, output, new_groups):
        groups_by_name = {
            o_group["name"]: o_group
            for o_group in output
        }
        extend_groups = []
        for new_group in new_groups:
            group_name = new_group["name"]
            if group_name not in groups_by_name:
                extend_groups.append(new_group)
                continue
            existing_group = groups_by_name[group_name]
            existing_variants = existing_group["variants"]
            existing_variants_by_name = {
                variant["name"]: variant
                for variant in existing_variants
            }
            for new_variant in new_group["variants"]:
                if new_variant["name"] not in existing_variants_by_name:
                    existing_variants.append(new_variant)

        output.extend(extend_groups)

    def _get_enum_items_from_groups(self, groups):
        label_by_name = {}
        for group in groups:
            group_name = group["name"]
            group_label = group.get(
                "label", LABELS_BY_GROUP_NAME.get(group_name)
            ) or group_name
            for variant in group["variants"]:
                variant_name = variant["name"]
                if not variant_name:
                    continue
                variant_label = variant["label"] or variant_name
                full_name = f"{group_name}/{variant_name}"
                full_label = f"{group_label} {variant_label}"
                label_by_name[full_name] = full_label

        return [
            {"value": full_name, "label": label_by_name[full_name]}
            for full_name in sorted(label_by_name)
        ]

    def _addon_has_attributes(self, addon, addon_version):
        version_obj = parse_version(addon_version)
        if version_obj is None or version_obj < ATTRIBUTES_VERSION_MILESTONE:
            return True

        return getattr(addon, "has_attributes", False)

    async def _update_enums(self):
        """Updates applications and tools enums based on the addon settings.
        This method is called when the addon is started (after we are sure
        that the 'applications' and 'tools' attributes exist) and when
        the addon settings are updated (using on_settings_updated method).
        """

        instance = AddonLibrary.getinstance()
        app_defs = instance.data.get(self.name)
        all_applications = []
        all_tools = []
        for addon_version in self._sort_versions(
            app_defs.versions.keys(), reverse=True
        ):
            addon = app_defs.versions[addon_version]
            if not self._addon_has_attributes(addon, addon_version):
                continue

            for variant in ("production", "staging"):
                settings_model = await addon.get_studio_settings(variant)
                studio_settings = settings_model.dict()
                application_settings = studio_settings["applications"]
                app_groups = application_settings.pop("additional_apps")
                for group_name, value in application_settings.items():
                    value["name"] = group_name
                    app_groups.append(value)
                self._merge_groups(all_applications, app_groups)
                self._merge_groups(all_tools, studio_settings["tool_groups"])

        apps_attrib_name = "applications"
        tools_attrib_name = "tools"

        apps_enum = self._get_enum_items_from_groups(all_applications)
        tools_enum = self._get_enum_items_from_groups(all_tools)

        apps_attribute_data = {
            "type": "list_of_strings",
            "title": "Applications",
            "enum": apps_enum,
        }
        tools_attribute_data = {
            "type": "list_of_strings",
            "title": "Tools",
            "enum": tools_enum,
        }

        apps_scope = ["project"]
        tools_scope = ["project", "folder", "task"]

        apps_matches = False
        tools_matches = False

        async for row in Postgres.iterate(
            "SELECT name, position, scope, data from public.attributes"
        ):
            if row["name"] == apps_attrib_name:
                # Check if scope is matching ftrack addon requirements
                if (
                    set(row["scope"]) == set(apps_scope)
                    and row["data"].get("enum") == apps_enum
                ):
                    apps_matches = True

            elif row["name"] == tools_attrib_name:
                if (
                    set(row["scope"]) == set(tools_scope)
                    and row["data"].get("enum") == tools_enum
                ):
                    tools_matches = True

        if apps_matches and tools_matches:
            return

        if not apps_matches:
            await Postgres.execute(
                """
                UPDATE attributes SET
                    scope = $1,
                    data = $2
                WHERE
                    name = $3
                """,
                apps_scope,
                apps_attribute_data,
                apps_attrib_name,
            )

        if not tools_matches:
            await Postgres.execute(
                """
                UPDATE attributes SET
                    scope = $1,
                    data = $2
                WHERE
                    name = $3
                """,
                tools_scope,
                tools_attribute_data,
                tools_attrib_name,
            )

        # Reset attributes cache on server
        await attribute_library.load()

    # --------------------------------------------
    # Auto-fill of host_name in workfiles entities
    # --------------------------------------------
    async def _workfile_entities_auto_filled(self) -> bool:
        async for _ in Postgres.iterate(
            "SELECT * FROM public.addon_data"
            " WHERE addon_name = $1 AND key = $2",
            self.name,
            "workfile_entities_host_name_filled",
        ):
            return True
        return False

    async def _on_bundle_updated(
        self, event: EventModel, *args, **kwargs
    ) -> None:
        if await self._workfile_entities_auto_filled():
            return

        if not event.summary.get("isProduction"):
            return

        addons = event.payload.get("addons", {})
        addon_version = addons.get(self.name)
        if addon_version != self.version:
            return

        await self._autofill_workfile_entities()

    async def _autofill_workfile_entities(self):
        project_names = [
            project.name
            for project in await get_project_list()
        ]
        for project_name in project_names:
            query = f"""
                SELECT id, attrib, path FROM project_{project_name}.workfiles
                WHERE data->'host_name' IS NULL;
            """
            workfile_entities = [
                row
                async for row in Postgres.iterate(query)
            ]
            changes = []
            for workfile_entity in workfile_entities:
                ext = workfile_entity["attrib"].get("extension")
                if not ext:
                    ext = os.path.splitext(workfile_entity["path"])[-1]
                if not ext:
                    continue
                mapped_host_name = EXT_TO_HOST_MAPPING.get(ext.lower())
                if mapped_host_name:
                    changes.append((workfile_entity["id"], mapped_host_name))

            for chunk in create_chunks(changes):
                async with Postgres.transaction():
                    for (workfile_id, host_name) in chunk:
                        await Postgres.execute(
                            f"UPDATE project_{project_name}.workfiles"
                            " SET data = jsonb_set(data, '{host_name}', $1)"
                            " WHERE id = $2;",
                            host_name,
                            workfile_id
                        )

        await Postgres.execute(
            "INSERT INTO public.addon_data"
            " (addon_name, addon_version, key, data)"
            " VALUES ($1, $2, $3, $4)",
            self.name,
            self.version,
            "workfile_entities_host_name_filled",
            {
                "project_names": project_names,
            }
        )
