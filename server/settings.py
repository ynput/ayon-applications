import json
from pydantic import validator

from ayon_server.settings import (
    BaseSettingsModel,
    SettingsField,
    ensure_unique_names,
)
from ayon_server.exceptions import BadRequestException


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


class AdditionalAppGroup(BaseSettingsModel):
    enabled: bool = SettingsField(True)
    name: str = SettingsField("", title="Name")
    label: str = SettingsField("", title="Label")
    host_name: str = SettingsField("", title="Host name")
    icon: str = SettingsField("", title="Icon")
    environment: str = SettingsField(
        "{}", title="Environment", widget="textarea"
    )

    variants: list[AppVariantWithPython] = SettingsField(
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
        default_factory=list, title="Applications"
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
        default_factory=AppGroup, title="Autodesk Maya")
    adsk_3dsmax: AppGroup = SettingsField(
        default_factory=AppGroup, title="Autodesk 3ds Max")
    flame: AppGroup = SettingsField(
        default_factory=AppGroup, title="Autodesk Flame")
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
        default_factory=AppGroup, title="Adobe Photoshop")
    aftereffects: AppGroup = SettingsField(
        default_factory=AppGroup, title="Adobe After Effects")
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
    additional_apps: list[AdditionalAppGroup] = SettingsField(
        default_factory=list, title="Additional Applications")

    @validator("additional_apps")
    def validate_unique_name(cls, value):
        ensure_unique_names(value)
        return value


class ApplicationsAddonSettings(BaseSettingsModel):
    applications: ApplicationsSettings = SettingsField(
        default_factory=ApplicationsSettings,
        title="Applications",
        scope=["studio"]
    )
    tool_groups: list[ToolGroupModel] = SettingsField(
        default_factory=list,
        scope=["studio"]
    )
    only_available: bool = SettingsField(
        True,
        title="Show only available applications",
        description="Enable to show only applications in AYON Launcher"
                    " for which the executable paths are found on the running machine."
                    " This applies as an additional filter to the applications defined in a "
                    " project's anatomy settings to ignore unavailable applications."
    )

    @validator("tool_groups")
    def validate_unique_name(cls, value):
        ensure_unique_names(value)
        return value


DEFAULT_VALUES = {
    "only_available": True
}
