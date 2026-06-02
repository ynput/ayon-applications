"""Server side of the Applications addon.

This module contains the server side of the Applications addon.
It is responsible for managing settings and initial setup of addon.
"""
import os
from typing import Any
from typing import TYPE_CHECKING

import semver
from fastapi import Query

from ayon_server.lib.postgres import Postgres
from ayon_server.logging import logger
from ayon_server.events import EventStream, EventModel
from ayon_server.addons import BaseServerAddon, AddonLibrary
from ayon_server.actions.config import set_action_config
from ayon_server.actions.context import ActionContext
from ayon_server.entities import TaskEntity
from ayon_server.entities.user import UserEntity
from ayon_server.helpers.project_list import get_project_list
from ayon_server.bundles.project_bundles import (
    has_project_bundle,
    get_project_bundle_addons,
)

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

from .settings import (
    ApplicationsAddonSettings,
    DEFAULT_VALUES,
    applications_enum,
)
from .utils import (
    ApplicationItem,
    ToolItem,
    get_application_items,
    get_tool_items,
    get_app_names_by_task_type,
)
from .actions import (
    get_action_manifests,
    get_dynamic_action_manifests,
    IDENTIFIER_PREFIX,
    IDENTIFIER_WORKFILE_PREFIX,
    DEBUG_TERMINAL_ID,
)

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
            all_nodes=False,
        )

        self.add_endpoint(
            "apps/{project_name}/task/{task_id}",
            self._get_task_applications_endpoint,
            method="GET",
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

    async def get_application_items(
        self, project_name: str | None, variant: str
    ) -> list[ApplicationItem]:
        if project_name is None:
            settings = await self.get_studio_settings(variant=variant)
        else:
            settings = await self.get_project_settings(
                project_name, variant=variant
            )
        return get_application_items(settings.dict())

    async def get_tool_items(
        self, project_name: str | None, variant: str
    ) -> list[ToolItem]:
        if project_name is None:
            settings = await self.get_studio_settings(variant=variant)
        else:
            settings = await self.get_project_settings(
                project_name, variant=variant
            )
        return get_tool_items(settings.dict())

    async def get_applications_items_for_task(
        self,
        project_name: str,
        task_id: str,
        variant: str,
    ) -> list[ApplicationItem]:
        settings = await self.get_project_settings(
            project_name, variant=variant
        )
        settings_value = settings.dict()

        app_items = get_application_items(settings_value)
        app_items_by_name = {
            app_item.full_name: app_item
            for app_item in app_items
        }

        task_entity = await TaskEntity.load(project_name, task_id)
        app_names_by_task_type = get_app_names_by_task_type(
            settings_value,
            {task_entity.task_type},
            app_items=app_items,
        )
        output = []
        for app_name in app_names_by_task_type[task_entity.task_type]:
            app_item = app_items_by_name[app_name]
            output.append(app_item)
        return output

    async def get_addon_for_context(
        self, project_name: str | None, variant: str
    ) -> BaseServerAddon | None:
        """Find applications addon version for a given context."""
        if (
            project_name is None
            or variant not in ("production", "staging")
            or not await has_project_bundle(project_name, variant=variant)
        ):
            return await self._get_studio_bundle_addon(variant)

        addons = await get_project_bundle_addons(
            project_name, variant=variant
        )
        version = addons.get(self.name)
        if not version or version == "__disable__":
            return None

        if version == "__inherit__":
            return await self._get_studio_bundle_addon(variant)

        addon_library = AddonLibrary.getinstance()
        if (addon_def := addon_library.data.get(self.name)) is None:
            return None
        return addon_def.get(version)

    async def get_applications_settings_enum(
        self,
        *,
        project_name: str | None = None,
        settings_variant: str = None,
    ):
        """Helper that can be used to get applications enum for settings.

        Example:
            from ayon_server.addons import AddonLibrary

            async def apps_enum(project_name, addon, settings_variant):
                addon_library = AddonLibrary.getinstance()
                app_addons = addon_library.data.get("applications") or {}
                addon = app_addons.latest
                if hasattr(addon, "get_applications_settings_enum"):
                    return await addon.get_applications_settings_enum(
                        project_name=project_name,
                        settings_variant=settings_variant,
                    )
                return []

            class SomeSettingsModel(BaseModel):
                application: str = SettingsField(
                    default_factory=list,
                    title="Applications",
                    enum_resolver=apps_enum,
                )
        """
        if settings_variant is None:
            settings_variant = "production"
        addon = await self.get_addon_for_context(
            project_name, settings_variant
        )
        if addon is self:
            return await applications_enum(
                project_name=project_name,
                addon=addon,
                settings_variant=settings_variant,
            )

        if hasattr(addon, "get_applications_settings_enum"):
            v_enum_func = addon.get_applications_settings_enum()
            return await v_enum_func(
                project_name=project_name,
                addon=addon,
                settings_variant=settings_variant,
            )
        return []

    async def get_applications_for_context(
        self, project_name: str | None, variant: str
    ) -> list[ApplicationItem]:
        """Get applications available for a given context.

        This method can be used by other addons to get applciations available
            for a given project and variant. It will return applciations based
            on variant and project bundle if project has any.

        Will work only if the addon version is new enough to have
            'get_tool_items' method, otherwise it will return empty list.

        """
        addon = await self.get_addon_for_context(project_name, variant)
        if hasattr(addon, "get_application_items"):
            return await addon.get_application_items(project_name, variant)
        return []

    async def get_tools_for_context(
        self, project_name: str | None, variant: str
    ) -> list[ToolItem]:
        """Get tools available for a given context.

        This method can be used by other addons to get tools available for
            a given project and variant. It will return tools based on variant
            and project bundle if project has any.

        Will work only if the addon version is new enough to have
            'get_tool_items' method, otherwise it will return empty list.

        """
        addon = await self.get_addon_for_context(project_name, variant)
        if hasattr(addon, "get_tool_items"):
            return await addon.get_tool_items(project_name, variant)
        return []

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

    async def _get_studio_bundle_addon(self, variant: str):
        addon_library = AddonLibrary.getinstance()
        if (addon_def := addon_library.data.get(self.name)) is None:
            return None
        addon_versions_by_name = (
            await addon_library.get_addon_versions_by_variant(variant)
        )
        version = addon_versions_by_name.get(self.name)
        return addon_def.get(version)

    async def _get_task_applications_endpoint(
        self,
        project_name: str,
        task_id: str,
        variant: str | None = Query(None, title="Settings Variant"),
    ):
        if variant is None:
            variant = "production"
        app_items = []
        addon = await self.get_addon_for_context(project_name, variant)
        if hasattr(addon, "get_applications_items_for_task"):
            app_items = await self.get_applications_items_for_task(
                project_name, task_id, variant
            )

        return {
            "applications": [app_item for app_item in app_items]
        }
