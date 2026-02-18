import collections
import os
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

from .constants import LABELS_BY_GROUP_NAME, ICONS_BY_GROUP_NAME

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


def _sort_getter(item):
    return item["group_label"], item["variant_label"]


def get_items_for_app_groups(groups):
    items = []
    for group in groups:
        group_name = group["name"]
        group_label = group.get(
            "label", LABELS_BY_GROUP_NAME.get(group_name)
        ) or group_name
        icon_name = ICONS_BY_GROUP_NAME.get(group_name)
        if not icon_name:
            icon_name = group.get("icon")

        if icon_name:
            icon_name = os.path.basename(icon_name)

        icon = None
        if icon_name:
            icon = {
                "type": "url",
                "url": f"{{addon_url}}/icons/{icon_name}",
            }

        for variant in group["variants"]:
            variant_name = variant["name"]
            if not variant_name:
                continue
            variant_label = variant["label"] or variant_name
            full_name = f"{group_name}/{variant_name}"
            items.append({
                "host_name": group["host_name"],
                "value": full_name,
                "group_label": group_label,
                "variant_label": variant_label,
                "icon": icon,
            })

    items.sort(key=_sort_getter)
    return items


def _prepare_label_kwargs(item):
    group_label = item["group_label"]
    variant_label = item["variant_label"]
    if _GROUP_LABEL_AVAILABLE:
        return {
            "label": variant_label,
            "group_label": group_label,
        }

    return {
        "label": f"{group_label} {variant_label}",
    }


def _get_app_items_by_name(
    addon_settings: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    app_settings = addon_settings["applications"]
    app_groups = app_settings.pop("additional_apps")
    for group_name, value in app_settings.items():
        if not value["enabled"]:
            continue
        value["name"] = group_name
        app_groups.append(value)

    # This is very simplified profiles logic
    app_items = get_items_for_app_groups(app_groups)
    return {
        item["value"]: item
        for item in app_items
    }


def _get_task_types_by_app_name(
    app_items_by_name: dict[str, dict[str, Any]],
    addon_settings: dict[str, Any],
    project_entity: ProjectEntity
) -> dict[str, set[str]]:
    project_task_types = {
        task_type["name"]
        for task_type in project_entity.task_types
    }
    task_types_by_app_name = collections.defaultdict(set)
    if not addon_settings["project_applications"]["enabled"]:
        project_apps = project_entity.original_attributes.get(
            "applications", []
        )
        for app_full_name, item in app_items_by_name.items():
            if app_full_name in project_apps:
                task_types_by_app_name[app_full_name] |= (
                    project_task_types.copy()
                )
        return task_types_by_app_name

    profiles = copy.deepcopy(
        addon_settings["project_applications"]["profiles"]
    )

    generic_apps = None
    used_task_types = set()
    for profile in profiles:
        allowed_apps = set(app_items_by_name.keys())
        if profile["allow_type"] != "all_applications":
            allowed_apps &= set(profile["applications"])

        if not profile["task_types"]:
            if generic_apps is None:
                generic_apps = allowed_apps
            continue

        task_types = set(profile["task_types"]) - used_task_types
        if not task_types:
            continue

        for app_name in allowed_apps:
            task_types_by_app_name[app_name] |= task_types

        used_task_types |= task_types

    generic_task_types = project_task_types - used_task_types
    if generic_task_types and generic_apps:
        for app_name in generic_apps:
            task_types_by_app_name[app_name] |= generic_task_types
    return task_types_by_app_name


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

    task_types_by_app_name = _get_task_types_by_app_name(
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

    for app_name, task_types in task_types_by_app_name.items():
        if not task_types:
            continue
        app_item = app_items_by_name[app_name]
        output.append(
            SimpleActionManifest(
                identifier=f"{IDENTIFIER_PREFIX}{app_name}",
                **_prepare_label_kwargs(app_item),
                category="Applications",
                icon=app_item["icon"],
                order=0,
                entity_type="task",
                entity_subtypes=list(task_types),
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

    task_types_by_app_name = _get_task_types_by_app_name(
        app_items_by_name,
        addon_settings,
        project_entity,
    )
    app_names_by_task_type = collections.defaultdict(set)
    for app_name, task_types in task_types_by_app_name.items():
        for task_type in task_types:
            app_names_by_task_type[task_type].add(app_name)

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
            if app_item["host_name"] not in host_names:
                continue
            output.append(DynamicActionManifest(
                identifier=f"{IDENTIFIER_WORKFILE_PREFIX}{app_name}",
                **_prepare_label_kwargs(app_item),
                category="Applications",
                icon=app_item["icon"],
                order=0,
                addon_name=addon.name,
                addon_version=addon.version,
            ))

    return output
