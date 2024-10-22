import os
import json
from pydantic import validator

from ayon_server.addons import BaseServerAddon
from ayon_server.settings import (
    BaseSettingsModel,
    SettingsField,
    ensure_unique_names,
    task_types_enum,
)
from ayon_server.exceptions import BadRequestException

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ICONS_DIR = os.path.join(
    os.path.dirname(CURRENT_DIR),
    "public",
    "icons"
)
DEFAULT_APP_GROUPS = {
    "maya",
    "adsk_3dsmax",
    "flame",
    "nuke",
    "nukeassist",
    "nukex",
    "nukestudio",
    "hiero",
    "fusion",
    "resolve",
    "houdini",
    "blender",
    "harmony",
    "tvpaint",
    "photoshop",
    "aftereffects",
    "celaction",
    "substancepainter",
    "unreal",
    "wrap",
    "openrv",
    "zbrush",
    "equalizer",
    "motionbuilder",
    "cinema4d",
    "terminal",
}


async def applications_enum(
    project_name: str | None = None,
    addon: BaseServerAddon = None,
    settings_variant: str = None,
):
    if project_name is None:
        settings = await addon.get_studio_settings(variant=settings_variant)
    else:
        settings = await addon.get_project_settings(
            project_name=project_name, variant=settings_variant
        )

    apps_settings = settings.applications
    apps_fields = apps_settings.__fields__
    apps_groups = set(apps_fields.keys())
    apps_groups.discard("additional_apps")
    all_variants_by_group_label = {}
    for group_name in apps_groups:
        app_group = getattr(apps_settings, group_name)
        # Skip disabled group
        if not app_group.enabled:
            continue

        # Skip group without variants
        app_variants = list(app_group.variants)
        if not app_variants:
            continue

        app_field = apps_fields[group_name]
        group_label = app_field.field_info.title

        app_variants = list(app_group.variants)
        app_variants.sort(key=lambda x: x.label or x.name, reverse=True)
        enum_variants = all_variants_by_group_label.setdefault(
            group_label, []
        )
        enum_variants.extend([
            {
                "label": f"{group_label} {variant.label or variant.name}",
                "value": f"{group_name}/{variant.name}",
            }
            for variant in app_variants
        ])

    for additional_app in apps_settings.additional_apps:
        group_name = additional_app.name
        if not additional_app.enabled or not group_name:
            continue

        app_variants = list(additional_app.variants)
        if not app_variants:
            continue

        group_label = additional_app.label
        if not group_label:
            group_label = group_name

        app_variants.sort(key=lambda x: x.label or x.name, reverse=True)
        enum_variants = all_variants_by_group_label.setdefault(
            group_label, []
        )
        enum_variants.extend([
            {
                "label": f"{group_label} {variant.label or variant.name}",
                "value": f"{group_name}/{variant.name}",
            }
            for variant in app_variants
        ])

    all_variants = []
    for key, value in sorted(all_variants_by_group_label.items()):
        all_variants.extend(value)
    return all_variants


async def tools_enum(
    project_name: str | None = None,
    addon: BaseServerAddon = None,
    settings_variant: str = None,
):
    if project_name is None:
        settings = await addon.get_studio_settings(variant=settings_variant)
    else:
        settings = await addon.get_project_settings(
            project_name=project_name, variant=settings_variant
        )

    enum_variants = []
    for tool_group in settings.tool_groups:
        group_name = tool_group.name
        group_label = tool_group.label or group_name
        enum_variants.extend([
            {
                "label": f"{group_label} {variant.label or variant.name}",
                "value": f"{group_name}/{variant.name}",
            }
            for variant in tool_group.variants
        ])
    return enum_variants


def validate_json_dict(value):
    if not value.strip():
        return "{}"
    try:
        converted_value = json.loads(value)
        success = isinstance(converted_value, dict)
    except json.JSONDecodeError as exc:
        print(exc)
        success = False

    if not success:
        raise BadRequestException(
            "Environment's can't be parsed as json object"
        )
    return value


class MultiplatformStrList(BaseSettingsModel):
    windows: list[str] = SettingsField(default_factory=list, title="Windows")
    linux: list[str] = SettingsField(default_factory=list, title="Linux")
    darwin: list[str] = SettingsField(default_factory=list, title="MacOS")


class AppVariant(BaseSettingsModel):
    name: str = SettingsField("", title="Name")
    label: str = SettingsField("", title="Label")
    executables: MultiplatformStrList = SettingsField(
        default_factory=MultiplatformStrList, title="Executables"
    )
    arguments: MultiplatformStrList = SettingsField(
        default_factory=MultiplatformStrList, title="Arguments"
    )
    environment: str = SettingsField(
        "{}", title="Environment", widget="textarea"
    )

    @validator("environment")
    def validate_json(cls, value):
        return validate_json_dict(value)


class AppGroup(BaseSettingsModel):
    enabled: bool = SettingsField(True)
    host_name: str = SettingsField("", title="Host name")
    environment: str = SettingsField(
        "{}", title="Environment", widget="textarea"
    )

    variants: list[AppVariant] = SettingsField(
        default_factory=list,
        title="Variants",
        description="Different variants of the applications",
        section="Variants",
    )

    @validator("variants")
    def validate_unique_name(cls, value):
        ensure_unique_names(value)
        return value


class AdditionalAppGroup(BaseSettingsModel):
    enabled: bool = SettingsField(True)
    name: str = SettingsField("", title="Name")
    label: str = SettingsField("", title="Label")
    host_name: str = SettingsField("", title="Host name")
    icon: str = SettingsField("", title="Icon")
    environment: str = SettingsField(
        "{}", title="Environment", widget="textarea"
    )

    variants: list[AppVariant] = SettingsField(
        default_factory=list,
        title="Variants",
        description="Different variants of the applications",
        section="Variants",
    )

    @validator("variants")
    def validate_unique_name(cls, value):
        ensure_unique_names(value)
        return value


class ToolVariantModel(BaseSettingsModel):
    name: str = SettingsField("", title="Name")
    label: str = SettingsField("", title="Label")
    host_names: list[str] = SettingsField(default_factory=list, title="Hosts")
    # TODO use applications enum if possible
    app_variants: list[str] = SettingsField(
        default_factory=list,
        title="Applications",
        enum_resolver=applications_enum,
    )
    environment: str = SettingsField(
        "{}", title="Environments", widget="textarea"
    )

    @validator("environment")
    def validate_json(cls, value):
        return validate_json_dict(value)


class ToolGroupModel(BaseSettingsModel):
    name: str = SettingsField("", title="Name")
    label: str = SettingsField("", title="Label")
    environment: str = SettingsField(
        "{}", title="Environments", widget="textarea"
    )
    variants: list[ToolVariantModel] = SettingsField(default_factory=list)

    @validator("environment")
    def validate_json(cls, value):
        return validate_json_dict(value)

    @validator("variants")
    def validate_unique_name(cls, value):
        ensure_unique_names(value)
        return value


class ApplicationsSettings(BaseSettingsModel):
    """Applications settings"""

    maya: AppGroup = SettingsField(
        default_factory=AppGroup, title="Maya")
    adsk_3dsmax: AppGroup = SettingsField(
        default_factory=AppGroup, title="3ds Max")
    flame: AppGroup = SettingsField(
        default_factory=AppGroup, title="Flame")
    nuke: AppGroup = SettingsField(
        default_factory=AppGroup, title="Nuke")
    nukeassist: AppGroup = SettingsField(
        default_factory=AppGroup, title="Nuke Assist")
    nukex: AppGroup = SettingsField(
        default_factory=AppGroup, title="Nuke X")
    nukestudio: AppGroup = SettingsField(
        default_factory=AppGroup, title="Nuke Studio")
    hiero: AppGroup = SettingsField(
        default_factory=AppGroup, title="Hiero")
    fusion: AppGroup = SettingsField(
        default_factory=AppGroup, title="Fusion")
    resolve: AppGroup = SettingsField(
        default_factory=AppGroup, title="Resolve")
    houdini: AppGroup = SettingsField(
        default_factory=AppGroup, title="Houdini")
    blender: AppGroup = SettingsField(
        default_factory=AppGroup, title="Blender")
    harmony: AppGroup = SettingsField(
        default_factory=AppGroup, title="Harmony")
    tvpaint: AppGroup = SettingsField(
        default_factory=AppGroup, title="TVPaint")
    photoshop: AppGroup = SettingsField(
        default_factory=AppGroup, title="Photoshop")
    aftereffects: AppGroup = SettingsField(
        default_factory=AppGroup, title="After Effects")
    celaction: AppGroup = SettingsField(
        default_factory=AppGroup, title="Celaction 2D")
    substancepainter: AppGroup = SettingsField(
        default_factory=AppGroup, title="Substance Painter")
    unreal: AppGroup = SettingsField(
        default_factory=AppGroup, title="Unreal Editor")
    wrap: AppGroup = SettingsField(
        default_factory=AppGroup, title="Wrap")
    openrv: AppGroup = SettingsField(
        default_factory=AppGroup, title="OpenRV")
    zbrush: AppGroup = SettingsField(
        default_factory=AppGroup, title="Zbrush")
    equalizer: AppGroup = SettingsField(
        default_factory=AppGroup, title="3DEqualizer")
    motionbuilder: AppGroup = SettingsField(
        default_factory=AppGroup, title="Motion Builder")
    cinema4d: AppGroup = SettingsField(
        default_factory=AppGroup, title="Cinema4D")
    terminal: AppGroup = SettingsField(
        default_factory=AppGroup,
        title="Terminal",
        description="Terminal application")
    additional_apps: list[AdditionalAppGroup] = SettingsField(
        default_factory=list, title="Additional Applications")

    @validator("additional_apps")
    def validate_unique_name(cls, value):
        ensure_unique_names(value)
        for item in value:
            if item.name in DEFAULT_APP_GROUPS:
                raise BadRequestException(f"Duplicate name '{item.name}'")
        return value


def _get_allow_type():
    return [
        {"label": "All applications", "value": "all_applications"},
        {"label": "Selected", "value": "applications"},
    ]


class ProjectApplicationsProfile(BaseSettingsModel):
    _layout = "expanded"
    task_types: list[str] = SettingsField(
        default_factory=list,
        title="Task Types",
        description="Filter by task types",
        enum_resolver=task_types_enum,
    )
    allow_type: str = SettingsField(
        "applications",
        title="Allow",
        enum_resolver=_get_allow_type,
        conditionalEnum=True,
    )
    applications: list[str] = SettingsField(
        default_factory=list,
        title="Applications",
        description="Applications available for filtered context",
        enum_resolver=applications_enum,
    )


class ProjectToolsProfile(BaseSettingsModel):
    _layout = "expanded"
    folder_paths: list[str] = SettingsField(
        default_factory=list,
        title="Folder paths",
        description="Filter by folder paths. Regex is supported.",
    )
    task_types: list[str] = SettingsField(
        default_factory=list,
        title="Task Types",
        description="Filter by task types",
        enum_resolver=task_types_enum,
    )
    task_names: list[str] = SettingsField(
        default_factory=list,
        title="Task Names",
        description="Filter by task names.",
    )
    tools: list[str] = SettingsField(
        default_factory=list,
        title="Tools",
        description="Tools used for filtered context",
        enum_resolver=tools_enum,
    )


class ProjectApplicationsModel(BaseSettingsModel):
    enabled: bool = SettingsField(
        True,
        title="Use Applications profiles instead of attribute",
        description=(
            "Use applications attribute on the project instead of these"
            " profiles. Attribute based applications will"
            " be deprecated in future versions of applications addon."
        ),
    )
    profiles: list[ProjectApplicationsProfile] = SettingsField(
        default_factory=list,
        title="Profiles",
    )


class ProjectToolsModel(BaseSettingsModel):
    enabled: bool = SettingsField(
        True,
        title="Use Tools profiles instead of attribute",
        description=(
            "Use tools attribute on folders and tasks instead of these"
            " profiles. Attribute based tools will"
            " be deprecated in future versions of applications addon."
        ),
    )
    profiles: list[ProjectToolsProfile] = SettingsField(
        default_factory=list,
        title="Profiles",
    )


class ApplicationsAddonSettings(BaseSettingsModel):
    only_available: bool = SettingsField(
        True,
        title="Show only available applications",
        description=(
            "Enable to show only applications in AYON Launcher for which"
            " the executable paths are found on the running machine."
            " This applies as an additional filter to the applications"
            " defined in a  project's anatomy settings to ignore"
            " unavailable applications."
        )
    )
    applications: ApplicationsSettings = SettingsField(
        default_factory=ApplicationsSettings,
        title="Applications Definitions",
        description="(Legacy) - Show / hide apps based on project's anatomy attributes.",
        scope=["studio"],
        section="Definitions"
    )
    tool_groups: list[ToolGroupModel] = SettingsField(
        default_factory=list,
        title="Tools Definitions",
        description="Configure your tools / plugins, e.g.renderer, hair addon etc.",
        scope=["studio"]
    )
    project_applications: ProjectApplicationsModel = SettingsField(
        title="Applications Filters",
        description=(
            "Enabling this feature will disable using 'applications'"
            " attribute on project. Attribute based applications will"
            " be deprecated in future versions of applications addon."
        ),
        default_factory=ProjectApplicationsModel,
        section="Filters"
    )
    project_tools: ProjectToolsModel = SettingsField(
        title="Tool Filters",
        description=(
            "Enabling this feature will disable using 'tools'"
            " attribute on folders and tasks. Attribute based tools will"
            " be deprecated in future versions of applications addon."
        ),
        default_factory=ProjectToolsModel,
    )

    @validator("tool_groups")
    def validate_unique_name(cls, value):
        ensure_unique_names(value)
        return value


def _get_applications_defaults():
    with open(os.path.join(CURRENT_DIR, "applications.json"), "r") as stream:
        applications_defaults = json.load(stream)
    return applications_defaults


def _get_tools_defaults():
    with open(os.path.join(CURRENT_DIR, "tools.json"), "r") as stream:
        tools_defaults = json.load(stream)
    return tools_defaults


DEFAULT_VALUES = {
    "only_available": True,
    "project_applications": {
        "profiles": [
            {
                "task_types": [],
                "allow_type": "all_applications",
                "applications": [],
            },
        ],
    }
}
DEFAULT_VALUES.update(_get_applications_defaults())
DEFAULT_VALUES.update(_get_tools_defaults())
