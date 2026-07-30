import typing

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

from .utils import (
    get_app_names_by_task_type,
    get_application_items,
    ApplicationItem,
)

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

    app_items = get_application_items(
        addon_settings,
        version=addon.version,
        fill_icon_url=False,
    )
    app_items_by_name = {
        item.full_name: item
        for item in app_items
    }
    task_type_names = {
        task_type["name"]
        for task_type in project_entity.task_types
    }
    app_names_by_task_type = get_app_names_by_task_type(
        addon_settings,
        task_type_names,
        app_items=app_items,
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

    app_items = get_application_items(
        addon_settings,
        version=addon.version,
        fill_icon_url=False,
    )
    app_items_by_name = {
        item.full_name: item
        for item in app_items
    }

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
    app_names_by_task_type = get_app_names_by_task_type(
        addon_settings,
        task_types,
        app_items=app_items,
    )

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
