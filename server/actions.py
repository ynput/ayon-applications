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


async def get_simple_action_manifests(
    addon: "ApplicationsAddon",
    project_name: str | None,
    variant: str,
) -> list[DynamicActionManifest]:
    return [
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
                "url": f"{{addon_url}}/public/icons/{icon_name}",
            }

        for variant in group["variants"]:
            variant_name = variant["name"]
            if not variant_name:
                continue
            variant_group_label = variant["group_label"]
            if not variant_group_label:
                variant_group_label = group_label
            variant_label = variant["label"] or variant_name
            full_name = f"{group_name}/{variant_name}"
            items.append({
                "host_name": group["host_name"],
                "value": full_name,
                "group_label": variant_group_label,
                "variant_label": variant_label,
                "icon": icon,
            })

    return items


def _get_app_items(
    addon_settings: dict[str, Any]
) -> list[dict[str, Any]]:
    app_settings = addon_settings["applications"]
    app_groups = app_settings.pop("additional_apps")
    for group_name, value in app_settings.items():
        if not value["enabled"]:
            continue
        value["name"] = group_name
        app_groups.append(value)

    # This is very simplified profiles logic
    return get_items_for_app_groups(app_groups)


def _get_applications_for_task_type(
    addon_settings: dict[str, Any],
    task_type: str,
) -> list[dict[str, Any]]:
    app_items = _get_app_items(addon_settings)
    profiles = copy.deepcopy(
        addon_settings["project_applications"]["profiles"]
    )

    filtered_profile = None
    for profile in profiles:
        if not profile["task_types"]:
            if filtered_profile is None:
                filtered_profile = profile
            continue

        if task_type in profile["task_types"]:
            filtered_profile = profile
            break

    if filtered_profile is None:
        return []

    if filtered_profile["allow_type"] == "all_applications":
        app_items.sort(key=_sort_getter)
        return app_items

    app_items_by_name = {
        app_item["value"]: app_item
        for app_item in app_items
    }

    fitlered_items = []
    for app_name in filtered_profile["applications"]:
        app_item = app_items_by_name.get(app_name)
        if app_item is not None:
            fitlered_items.append(app_item)
    return fitlered_items


async def _get_task_action_manifests(
    addon: "ApplicationsAddon",
    context: ActionContext,
    variant: str,
) -> list[DynamicActionManifest]:
    project_name = context.project_name
    if (
        not project_name
        or not context.entity_ids
        or len(context.entity_ids) != 1
    ):
        return []

    task_id = context.entity_ids[0]
    task_entity = await TaskEntity.load(project_name, task_id)
    if task_entity is None:
        return []

    settings_model = await addon.get_project_settings(
        project_name, variant=variant
    )
    addon_settings = settings_model.dict()

    app_items = _get_applications_for_task_type(
        addon_settings,
        task_entity.task_type,
    )
    return [
        DynamicActionManifest(
            identifier=f"{IDENTIFIER_PREFIX}{app_item['value']}",
            label=app_item["variant_label"],
            group_label=app_item["group_label"],
            category="Applications",
            icon=app_item["icon"],
            order=0,
            addon_name=addon.name,
        )
        for app_item in app_items
    ]


async def _get_workfile_action_manifests(
    addon: "ApplicationsAddon",
    context: ActionContext,
    variant: str,
) -> list[DynamicActionManifest]:
    project_name = context.project_name
    if (
        not project_name
        or not context.entity_ids
        or len(context.entity_ids) != 1
    ):
        return []

    entity_id = context.entity_ids[0]
    workfile_entity = await WorkfileEntity.load(
        project_name=project_name,
        entity_id=entity_id,
    )
    if not workfile_entity:
        return []

    host_name = workfile_entity.data.get("host_name")
    if not host_name:
        return []

    task_id = workfile_entity.task_id
    task_entity = await TaskEntity.load(project_name, task_id)
    if task_entity is None:
        return []

    settings_model = await addon.get_project_settings(
        project_name, variant=variant
    )
    addon_settings = settings_model.dict()

    app_items = _get_applications_for_task_type(
        addon_settings,
        task_entity.task_type,
    )
    manifests = []
    for app_item in app_items:
        if app_item["host_name"] != host_name:
            continue
        manifests.append(
            DynamicActionManifest(
                identifier=f"{IDENTIFIER_WORKFILE_PREFIX}{app_item['value']}",
                label=app_item["variant_label"],
                group_label=app_item["group_label"],
                category="Applications",
                icon=app_item["icon"],
                order=0,
                addon_name=addon.name,
            )
        )
    return manifests


async def get_dynamic_action_manifests(
    addon: "ApplicationsAddon",
    context: ActionContext,
    variant: str,
) -> list[DynamicActionManifest]:
    if context.entity_type == "task":
        return await _get_task_action_manifests(
            addon,
            context,
            variant,
        )

    if context.entity_type == "workfile":
        return await _get_workfile_action_manifests(
            addon,
            context,
            variant,
        )
    return []
