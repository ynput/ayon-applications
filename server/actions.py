import copy
import typing
from typing import Any

from ayon_server.actions import (
    SimpleActionManifest,
    DynamicActionManifest,
    ActionContext,
)

from ayon_server.entities import ProjectEntity, TaskEntity, WorkfileEntity
try:
    # Added in ayon-backend 1.8.0
    from ayon_server.forms import SimpleForm
except ImportError:
    SimpleForm = None

from .utils import get_application_items, ApplicationItem

IDENTIFIER_PREFIX = "application.launch."
IDENTIFIER_WORKFILE_PREFIX = "application.launch-workfile."
DEBUG_TERMINAL_ID = "application.debug_terminal"

_manifest_fields = getattr(SimpleActionManifest, "__fields__", None)
if _manifest_fields is None:
    _manifest_fields = getattr(SimpleActionManifest, "model_fields", set)()
# Backwards compatibility for AYON server older than 1.8.0
_GROUP_LABEL_AVAILABLE = "group_label" in _manifest_fields

if typing.TYPE_CHECKING:
    from .addon import ApplicationsAddon


def _prepare_label_kwargs(item: ApplicationItem) -> dict[str, str]:
    if _GROUP_LABEL_AVAILABLE and item.show_grouped:
        return {
            "label": item.variant_label,
            "group_label": item.group_label,
        }

    return {
        "label": item.full_label,
    }


def _get_app_items_by_name(
    addon_settings: dict[str, Any]
) -> dict[str, ApplicationItem]:
    return {
        item.full_name: item
        for item in get_application_items(addon_settings)
    }


def _get_app_names_by_task_type(
    app_items_by_name: dict[str, ApplicationItem],
    addon_settings: dict[str, Any],
    project_entity: ProjectEntity
) -> dict[str, list[str]]:
    profiles = copy.deepcopy(
        addon_settings["project_applications"]["profiles"]
    )

    default_profile = None
    profiles_by_task_type = {}
    for profile in profiles:
        if not profile["task_types"]:
            if default_profile is None:
                default_profile = profile
            continue

        for task_type in profile["task_types"]:
            profiles_by_task_type.setdefault(task_type, profile)

    app_names_by_task_type = {
        task_type["name"]: []
        for task_type in project_entity.task_types
    }
    for task_type in project_entity.task_types:
        task_type_name = task_type["name"]
        task_type_profile = profiles_by_task_type.get(task_type_name)
        if task_type_profile is None:
            task_type_profile = default_profile
            if task_type_profile is None:
                continue

        if task_type_profile["allow_type"] == "all_applications":
            profile_apps = list(app_items_by_name.keys())
            profile_apps.sort()
        else:
            profile_apps = [
                app_name
                for app_name in task_type_profile["applications"]
                if app_name in app_items_by_name
            ]

        if profile_apps:
            app_names_by_task_type[task_type_name] = profile_apps

    return app_names_by_task_type


async def get_action_manifests(
    addon: "ApplicationsAddon",
    project_name: str,
    variant: str,
):
    if not project_name:
        return []

    project_entity = await ProjectEntity.load(project_name)

    settings_model = await addon.get_project_settings(
        project_name, variant=variant
    )
    addon_settings = settings_model.dict()

    app_items_by_name = _get_app_items_by_name(addon_settings)

    app_names_by_task_type = _get_app_names_by_task_type(
        app_items_by_name,
        addon_settings,
        project_entity,
    )

    output = [
        SimpleActionManifest(
            order=100,
            identifier=DEBUG_TERMINAL_ID,
            label="Terminal",
            category="Applications",
            entity_type="task",
            allow_multiselection=False,
            icon={
                "type": "material-symbols",
                "name": "terminal",
                "color": "#e8770e",
            },
        ),
    ]

    kwargs = {}
    if SimpleForm is not None:
        kwargs["config_fields"] = SimpleForm().boolean(
            "skip_last_workfile",
            label="Skip last workfile",
            value=False,
        )

    for task_type, app_names in app_names_by_task_type.items():
        for app_name in app_names:
            app_item = app_items_by_name[app_name]
            output.append(
                SimpleActionManifest(
                    identifier=f"{IDENTIFIER_PREFIX}{app_name}",
                    **_prepare_label_kwargs(app_item),
                    category="Applications",
                    icon=app_item.icon,
                    order=0,
                    entity_type="task",
                    entity_subtypes=[task_type],
                    allow_multiselection=False,
                    **kwargs
                )
            )
    return output


async def get_dynamic_action_manifests(
    addon: "ApplicationsAddon",
    context: ActionContext,
    variant: str,
) -> list[DynamicActionManifest]:
    project_name = context.project_name
    if not project_name or context.entity_type != "workfile":
        return []

    workfile_entities = [
        await WorkfileEntity.load(
            project_name=project_name,
            entity_id=entity_id,
        )
        for entity_id in context.entity_ids
    ]
    host_names = set()
    for workfile_entity in workfile_entities:
        host_name = workfile_entity.data.get("host_name")
        if host_name:
            host_names.add(host_name)

    if not host_names:
        return []

    settings_model = await addon.get_project_settings(
        project_name, variant=variant
    )
    addon_settings = settings_model.dict()

    project_entity = await ProjectEntity.load(project_name)

    app_items_by_name = _get_app_items_by_name(addon_settings)

    app_names_by_task_type = _get_app_names_by_task_type(
        app_items_by_name,
        addon_settings,
        project_entity,
    )

    task_ids = {
        workfile_entity.task_id
        for workfile_entity in workfile_entities
    }
    task_entities = [
        await TaskEntity.load(project_name, task_id)
        for task_id in task_ids
    ]
    task_types = {
        task_entity.task_type
        for task_entity in task_entities
    }

    collected_apps = set()
    output = []
    for task_type in task_types:
        for app_name in app_names_by_task_type[task_type]:
            if app_name in collected_apps:
                continue
            collected_apps.add(app_name)

            app_item = app_items_by_name[app_name]
            if app_item.host_name not in host_names:
                continue
            output.append(DynamicActionManifest(
                identifier=f"{IDENTIFIER_WORKFILE_PREFIX}{app_name}",
                **_prepare_label_kwargs(app_item),
                category="Applications",
                icon=app_item.icon,
                order=0,
                addon_name=addon.name,
                addon_version=addon.version,
            ))

    return output
