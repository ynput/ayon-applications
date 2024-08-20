import collections
import os
import copy

from ayon_server.actions import SimpleActionManifest
from ayon_server.entities import ProjectEntity

from .constants import LABELS_BY_GROUP_NAME, ICONS_BY_GROUP_NAME

IDENTIFIER_PREFIX = "application.launch."


def get_items_for_app_groups(groups):
    label_by_name = {}
    icon_by_name = {}
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
                "url": "{addon_url}/public/icons/" + icon_name,
            }

        for variant in group["variants"]:
            variant_name = variant["name"]
            if not variant_name:
                continue
            variant_label = variant["label"] or variant_name
            full_name = f"{group_name}/{variant_name}"
            full_label = f"{group_label} {variant_label}"
            label_by_name[full_name] = full_label
            icon_by_name[full_name] = icon

    return [
        {
            "value": full_name,
            "label": label_by_name[full_name],
            "icon": icon_by_name[full_name],
        }
        for full_name in sorted(label_by_name)
    ]


async def _get_action_manifests_with_attributes(app_groups, project_entity):
    project_apps = project_entity.original_attributes.get(
        "applications", []
    )
    output = []
    for item in get_items_for_app_groups(app_groups):
        app_full_name = item["value"]
        if app_full_name not in project_apps:
            continue

        output.append(
            SimpleActionManifest(
                identifier=f"{IDENTIFIER_PREFIX}{app_full_name}",
                label=item["label"],
                category="Applications",
                icon=item["icon"],
                order=100,
                entity_type="task",
                entity_subtypes=None,
                allow_multiselection=False,
            )
        )
    return output


async def get_action_manifests(addon, project_name, variant):
    if not project_name:
        return []

    settings_model = await addon.get_studio_settings(variant=variant)
    addon_settings = settings_model.dict()

    app_settings = addon_settings["applications"]
    app_groups = app_settings.pop("additional_apps")
    for group_name, value in app_settings.items():
        value["name"] = group_name
        app_groups.append(value)

    project_entity = await ProjectEntity.load(project_name)
    if not addon_settings["project_applications"]["enabled"]:
        output = await _get_action_manifests_with_attributes(
            app_groups, project_entity
        )
        return output

    # This is very simplified profiles logic
    app_items = get_items_for_app_groups(app_groups)
    app_items_by_name = {
        item["value"]: item
        for item in app_items
    }
    task_types_by_app_name = collections.defaultdict(set)

    profiles = copy.deepcopy(
        addon_settings["project_applications"]["profiles"]
    )

    generic_apps = None
    used_task_types = set()
    for profile in profiles:
        if profile["allow_type"] == "all_applications":
            allowed_apps = list(app_items_by_name.keys())
        else:
            allowed_apps = list(profile["applications"])

        if not profile["task_types"]:
            if generic_apps is not None:
                continue
            generic_apps = allowed_apps
            continue

        task_types = set(profile["task_types"]) - used_task_types
        if not task_types:
            continue

        for app_name in allowed_apps:
            task_types_by_app_name[app_name] |= task_types

        used_task_types |= task_types

    project_task_types = {
        task_type["name"]
        for task_type in project_entity.task_types
    }
    generic_task_types = project_task_types - used_task_types
    if generic_task_types:
        for app_name in generic_apps:
            task_types_by_app_name[app_name] |= generic_task_types

    output = []
    for app_item in app_items:
        app_name = app_item["value"]
        task_types = task_types_by_app_name[app_name]
        if not task_types:
            continue
        output.append(
            SimpleActionManifest(
                identifier=f"{IDENTIFIER_PREFIX}{app_name}",
                label=app_item["label"],
                category="Applications",
                icon=app_item["icon"],
                order=100,
                entity_type="task",
                entity_subtypes=list(task_types),
                allow_multiselection=False,
            )
        )
    return output
