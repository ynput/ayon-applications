from dataclasses import dataclass
import os
from typing import Any
import urllib.parse

from .constants import LABELS_BY_GROUP_NAME, ICONS_BY_GROUP_NAME


@dataclass
class ApplicationItem:
    host_name: str
    full_name: str
    full_label: str
    group_label: str
    variant_label: str
    icon: dict[str, str] | None
    show_grouped: bool


@dataclass
class ToolItem:
    full_name: str
    full_label: str
    group_label: str
    variant_label: str
    host_names: list[str]
    app_variants: list[str]


def get_application_items(
    addon_settings: dict[str, Any],
    *,
    version: str = "",
    fill_icon_url: bool = False,
) -> list[ApplicationItem]:
    app_settings = addon_settings["applications"]
    app_groups = app_settings.pop("additional_apps")
    for group_name, value in app_settings.items():
        if not value["enabled"]:
            continue
        value["name"] = group_name
        app_groups.append(value)

    return get_items_for_app_groups(
        app_groups,
        version=version,
        fill_icon_url=fill_icon_url,
    )


def get_tool_items(
    addon_settings: dict[str, Any]
) -> list[ToolItem]:
    return get_items_for_tool_groups(addon_settings["tool_groups"])


def _sort_getter(item: ApplicationItem):
    return item.group_label, item.variant_label


def get_items_for_app_groups(
    groups: list[dict[str, Any]],
    *,
    version: str = "",
    fill_icon_url: bool = False,
) -> list[ApplicationItem]:
    items = []
    for group in groups:
        group_name = group["name"]
        group_label = group.get(
            "label", LABELS_BY_GROUP_NAME.get(group_name)
        ) or group_name
        icon_name = ICONS_BY_GROUP_NAME.get(group_name)
        if not icon_name:
            icon_name = group.get("icon")

        icon = None
        if icon_name:
            url = urllib.parse.urlparse(icon_name)
            if not url.scheme:
                # it's a bare filename served from this addons public folder
                icon_name = os.path.basename(icon_name)
                icon_name = f"/api{{addon_url}}/icons/{icon_name}"
                if fill_icon_url:
                    icon_name = icon_name.format(
                        addon_url=f"/addons/applications/{version}"
                    )

            icon = {
                "type": "url",
                "url": icon_name,
            }

        for variant in group["variants"]:
            variant_name = variant["name"]
            if not variant_name:
                continue
            variant_group_label = variant["group_label"]
            if not variant_group_label:
                variant_group_label = group_label
            variant_label = variant["label"] or variant_name
            full_label = f"{variant_group_label} {variant_label}"
            full_name = f"{group_name}/{variant_name}"
            items.append(ApplicationItem(
                host_name=group["host_name"],
                full_name=full_name,
                full_label=full_label,
                group_label=variant_group_label,
                variant_label=variant_label,
                icon=icon,
                show_grouped=variant["show_grouped"],
            ))

    items.sort(key=_sort_getter)
    return items


def get_items_for_tool_groups(groups):
    items = []
    for group in groups:
        group_name = group["name"]
        group_label = group["label"] or group_name

        for variant in group["variants"]:
            variant_name = variant["name"]
            if not variant_name:
                continue
            variant_label = variant["label"] or variant_name
            full_label = f"{group_label} {variant_label}"
            full_name = f"{group_name}/{variant_name}"
            items.append(ToolItem(
                full_name=full_name,
                full_label=full_label,
                group_label=group_label,
                variant_label=variant_label,
                host_names=list(variant["host_names"]),
                app_variants=list(variant["app_variants"]),
            ))

    items.sort(key=_sort_getter)
    return items


def get_app_names_by_task_type(
    addon_settings: dict[str, Any],
    task_types: set[str],
    app_items: list[ApplicationItem],
) -> dict[str, list[str]]:
    app_names_by_task_type = {
        task_type: []
        for task_type in task_types
    }
    if not task_types:
        return app_names_by_task_type

    profiles = addon_settings["project_applications"]["profiles"]

    app_names = {item.full_name for item in app_items}
    default_profile = None
    profiles_by_task_type = {}
    for profile in profiles:
        if not profile["task_types"]:
            if default_profile is None:
                default_profile = profile
            continue

        for task_type in profile["task_types"]:
            profiles_by_task_type.setdefault(task_type, profile)

    for task_type_name in app_names_by_task_type:
        task_type_profile = profiles_by_task_type.get(task_type_name)
        if task_type_profile is None:
            task_type_profile = default_profile
            if task_type_profile is None:
                continue

        if task_type_profile["allow_type"] == "all_applications":
            profile_apps = list(app_names)
            profile_apps.sort()
        else:
            profile_apps = [
                app_name
                for app_name in task_type_profile["applications"]
                if app_name in app_names
            ]

        if profile_apps:
            app_names_by_task_type[task_type_name] = profile_apps

    return app_names_by_task_type
