from __future__ import annotations

import os
import copy
import json
import platform
import collections
import logging
import typing
from typing import Optional, Any

import ayon_api

from ayon_core import AYON_CORE_ROOT
from ayon_core.settings import get_project_settings
from ayon_core.lib import Logger, get_ayon_username, filter_profiles
from ayon_core.addon import AddonsManager
from ayon_core.pipeline.template_data import get_template_data
from ayon_core.pipeline.workfile import (
    get_workfile_template_key,
    get_workdir_with_workdir_data,
    get_last_workfile,
    should_use_last_workfile_on_launch,
    should_copy_last_published_workfile_on_launch,
    should_open_workfiles_tool_on_launch,
    copy_last_published_workfile,
)

from .constants import (
    APPLICATIONS_ADDON_ROOT,
    DEFAULT_ENV_SUBGROUP,
    PLATFORM_NAMES,
)
from .exceptions import MissingRequiredKey, ApplicationLaunchFailed
from .manager import ApplicationManager

try:
    # Functions were not in '__init__.py'
    from ayon_core.lib import (
        merge_env_variables,
        compute_env_variables_structure,
    )
except ImportError:
    from ayon_core.lib.env_tools import (
        merge_env_variables,
        compute_env_variables_structure,
    )

if typing.TYPE_CHECKING:
    from .defs import Application


def parse_environments(
    env_data: dict[str, Any],
    env_group: Optional[str] = None,
    platform_name: Optional[str] = None,
) -> dict[str, str]:
    """Parse environment values from settings byt group and platform.

    Data may contain up to 2 hierarchical levels of dictionaries. At the end
    of the last level must be string or list. List is joined using platform
    specific joiner (';' for windows and ':' for linux and mac).

    Hierarchical levels can contain keys for subgroups and platform name.
    Platform specific values must be always last level of dictionary. Platform
    names are "windows" (MS Windows), "linux" (any linux distribution) and
    "darwin" (any MacOS distribution).

    Subgroups are helpers added mainly for standard and on farm usage. Farm
    may require different environments for e.g. licence related values or
    plugins. Default subgroup is "standard".

    Examples:
    ```
    {
        # Unchanged value
        "ENV_KEY1": "value",
        # Empty values are kept (unset environment variable)
        "ENV_KEY2": "",

        # Join list values with ':' or ';'
        "ENV_KEY3": ["value1", "value2"],

        # Environment groups
        "ENV_KEY4": {
            "standard": "DEMO_SERVER_URL",
            "farm": "LICENCE_SERVER_URL"
        },

        # Platform specific (and only for windows and mac)
        "ENV_KEY5": {
            "windows": "windows value",
            "darwin": ["value 1", "value 2"]
        },

        # Environment groups and platform combination
        "ENV_KEY6": {
            "farm": "FARM_VALUE",
            "standard": {
                "windows": ["value1", "value2"],
                "linux": "value1",
                "darwin": ""
            }
        }
    }
    ```
    """
    output = {}
    if not env_data:
        return output

    if not env_group:
        env_group = DEFAULT_ENV_SUBGROUP

    if not platform_name:
        platform_name = platform.system().lower()

    for key, value in env_data.items():
        if isinstance(value, dict):
            # Look if any key is platform key
            #   - expect that represents environment group if does not contain
            #   platform keys
            if not PLATFORM_NAMES.intersection(set(value.keys())):
                # Skip the key if group is not available
                if env_group not in value:
                    continue
                value = value[env_group]

        # Check again if value is dictionary
        #   - this time there should be only platform keys
        if isinstance(value, dict):
            value = value.get(platform_name)

        # Check if value is list and join it's values
        # QUESTION Should empty values be skipped?
        if isinstance(value, (list, tuple)):
            value = os.pathsep.join(value)

        # Set key to output if value is string
        if isinstance(value, str):
            output[key] = value
    return output


class EnvironmentPrepData(dict):
    """Helper dictionary for storin temp data during environment prep.

    Args:
        data (dict): Data must contain required keys.
    """
    required_keys = (
        "project_entity",
        "folder_entity",
        "task_entity",
        "app",
        "anatomy",
    )

    def __init__(self, data: dict[str, Any]):
        for key in self.required_keys:
            if key not in data:
                raise MissingRequiredKey(key)

        if not data.get("log"):
            data["log"] = Logger.get_logger("EnvironmentPrepData")

        if data.get("env") is None:
            data["env"] = os.environ.copy()

        project_name = data["project_entity"]["name"]
        if "project_settings" not in data:
            data["project_settings"] = get_project_settings(project_name)

        super().__init__(data)


def get_app_environments_for_context(
    project_name: str,
    folder_path: str,
    task_name: str,
    app_name: str,
    env_group: Optional[str] = None,
    launch_type: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    addons_manager: Optional[AddonsManager] = None,
) -> dict[str, str]:
    """Prepare environment variables by context.
    Args:
        project_name (str): Name of project.
        folder_path (str): Folder path.
        task_name (str): Name of task.
        app_name (str): Name of application that is launched and can be found
            by ApplicationManager.
        env_group (Optional[str]): Name of environment group. If not passed
            default group is used.
        launch_type (Optional[str]): Type for which prelaunch hooks are
            executed.
        env (Optional[dict[str, str]]): Initial environment variables.
            `os.environ` is used when not passed.
        addons_manager (Optional[AddonsManager]): Initialized modules
            manager.

    Returns:
        dict: Environments for passed context and application.

    """
    # Prepare app object which can be obtained only from ApplicationManager
    app_manager = ApplicationManager()
    context = app_manager.create_launch_context(
        app_name,
        project_name=project_name,
        folder_path=folder_path,
        task_name=task_name,
        env_group=env_group,
        launch_type=launch_type,
        env=env,
        addons_manager=addons_manager,
        modules_manager=addons_manager,
    )
    context.run_prelaunch_hooks()
    return context.env


def _add_python_version_paths(
    app: "Application",
    env: dict[str, str],
    logger: logging.Logger,
    addons_manager: AddonsManager,
) -> None:
    """Add vendor packages specific for a Python version."""

    for addon in addons_manager.get_enabled_addons():
        if hasattr(addon, "modify_application_launch_arguments"):
            addon.modify_application_launch_arguments(app, env)

    # Skip adding if host name is not set
    if not app.host_name:
        return

    # Add Python 2/3 modules
    python_vendor_dir = os.path.join(
        AYON_CORE_ROOT,
        "vendor",
        "python"
    )
    if app.use_python_2:
        pythonpath = os.path.join(python_vendor_dir, "python_2")
    else:
        pythonpath = os.path.join(python_vendor_dir, "python_3")

    if not os.path.exists(pythonpath):
        return

    logger.debug("Adding Python version specific paths to PYTHONPATH")
    python_paths = [pythonpath]

    # Load PYTHONPATH from current launch context
    python_path = env.get("PYTHONPATH")
    if python_path:
        python_paths.append(python_path)

    # Set new PYTHONPATH to launch context environments
    env["PYTHONPATH"] = os.pathsep.join(python_paths)


def _get_app_full_names_from_settings(
    applications_settings: dict[str, Any]
) -> list[str]:
    """Get full names of applications from settings.

    Args:
        applications_settings (dict): Applications settings.

    Returns:
        list[str]: Full names of applications.

    """
    apps = copy.deepcopy(applications_settings["applications"])
    additional_apps = apps.pop("additional_apps")

    full_names = []
    for group_name, group_info in apps.items():
        for variant in group_info["variants"]:
            variant_name = variant["name"]
            full_names.append(f"{group_name}/{variant_name}")

    for additional_app in additional_apps:
        group_name = additional_app["name"]
        for variant in additional_app["variants"]:
            variant_name = variant["name"]
            full_names.append(f"{group_name}/{variant_name}")

    return full_names


def get_applications_for_context(
    project_name: str,
    folder_entity: dict[str, Any],
    task_entity: dict[str, Any],
    project_settings: Optional[dict[str, Any]] = None,
    project_entity: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Get applications for context based on project settings.

    Args:
        project_name (str): Name of project.
        folder_entity (dict): Folder entity.
        task_entity (dict): Task entity.
        project_settings (Optional[dict]): Project settings.
        project_entity (Optional[dict]): Project entity.

    Returns:
        list[str]: List of applications that can be used in given context.

    """
    if project_settings is None:
        project_settings = get_project_settings(project_name)
    apps_settings = project_settings["applications"]

    # Use attributes to get available applications
    # - this is older source of the information, will be deprecated in future
    project_applications = apps_settings["project_applications"]
    if not project_applications["enabled"]:
        if project_entity is None:
            project_entity = ayon_api.get_project(project_name)
        apps = project_entity["attrib"].get("applications")
        return apps or []

    task_type = None
    if task_entity:
        task_type = task_entity["taskType"]

    profile = filter_profiles(
        project_applications["profiles"],
        {"task_types": task_type}
    )
    if profile:
        if profile["allow_type"] == "applications":
            return profile["applications"]
        return _get_app_full_names_from_settings(apps_settings)
    return []


def get_tools_for_context(
    project_name: str,
    folder_entity: dict[str, Any],
    task_entity: dict[str, Any],
    project_settings: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Get tools for context based on project settings.

    Args:
        project_name (str): Name of project.
        folder_entity (dict): Folder entity.
        task_entity (dict): Task entity.
        project_settings (Optional[dict]): Project settings.

    Returns:
        list[str]: List of applications that can be used in given context.

    """
    if project_settings is None:
        project_settings = get_project_settings(project_name)
    apps_settings = project_settings["applications"]

    project_tools = apps_settings["project_tools"]
    # Use attributes to get available tools
    # - this is older source of the information, will be deprecated in future
    if not project_tools["enabled"]:
        tools = None
        if task_entity:
            tools = task_entity["attrib"].get("tools")

        if tools is None and folder_entity:
            tools = folder_entity["attrib"].get("tools")

        return tools or []

    folder_path = task_type = task_name = None
    if folder_entity:
        folder_path = folder_entity["path"]
    if task_entity:
        task_type = task_entity["taskType"]
        task_name = task_entity["name"]

    profile = filter_profiles(
        project_tools["profiles"],
        {
            "folder_paths": folder_path,
            "task_types": task_type,
            "task_names": task_name,
        },
        keys_order=["folder_paths", "task_names", "task_types"]
    )
    if profile:
        return profile["tools"]
    return []


def prepare_app_environments(
    data: dict[str, Any],
    env_group: Optional[str] = None,
    implementation_envs: bool = True,
    addons_manager: Optional[AddonsManager] = None
) -> None:
    """Modify launch environments based on launched app and context.

    Args:
        data (EnvironmentPrepData): Dictionary where result and intermediate
            result will be stored.

    """
    app = data["app"]
    log = data["log"]
    source_env = data["env"].copy()

    if addons_manager is None:
        addons_manager = AddonsManager()

    _add_python_version_paths(app, source_env, log, addons_manager)

    # Use environments from local settings
    filtered_local_envs = {}
    # NOTE Overrides for environment variables are not implemented in AYON.
    # project_settings = data["project_settings"]
    # whitelist_envs = project_settings["general"].get("local_env_white_list")
    # if whitelist_envs:
    #     local_settings = get_local_settings()
    #     local_envs = local_settings.get("environments") or {}
    #     filtered_local_envs = {
    #         key: value
    #         for key, value in local_envs.items()
    #         if key in whitelist_envs
    #     }

    # Apply local environment variables for already existing values
    for key, value in filtered_local_envs.items():
        if key in source_env:
            source_env[key] = value

    # `app_and_tool_labels` has debug purpose
    app_and_tool_labels = [app.full_name]
    # Environments for application
    environments = [
        app.group.environment,
        app.environment
    ]

    tools = get_tools_for_context(
        data.get("project_name"),
        data.get("folder_entity"),
        data.get("task_entity"),
    )

    # Add tools environments
    groups_by_name = {}
    tool_by_group_name = collections.defaultdict(dict)
    used_tool_names = []
    for key in tools:
        tool = app.manager.tools.get(key)
        if not tool or not tool.is_valid_for_app(app):
            continue
        used_tool_names.append(tool.full_name)
        groups_by_name[tool.group.name] = tool.group
        tool_by_group_name[tool.group.name][tool.name] = tool

    for group_name in sorted(groups_by_name.keys()):
        group = groups_by_name[group_name]
        environments.append(group.environment)
        for tool_name in sorted(tool_by_group_name[group_name].keys()):
            tool = tool_by_group_name[group_name][tool_name]
            environments.append(tool.environment)
            app_and_tool_labels.append(tool.full_name)

    log.info(
        "Will add environments for apps and tools: {}".format(
            ", ".join(app_and_tool_labels)
        )
    )

    env_values = {}
    for _env_values in environments:
        if not _env_values:
            continue

        # Choose right platform
        tool_env = parse_environments(_env_values, env_group)

        # Apply local environment variables
        # - must happen between all values because they may be used during
        #   merge
        for key, value in filtered_local_envs.items():
            if key in tool_env:
                tool_env[key] = value

        # Merge dictionaries
        env_values = merge_env_variables(tool_env, env_values)

    merged_env = merge_env_variables(env_values, source_env)
    loaded_env = compute_env_variables_structure(merged_env)

    final_env = None
    # Add host specific environments
    if app.host_name and implementation_envs:
        host_addon = addons_manager.get_host_addon(app.host_name)
        add_implementation_envs = None
        if host_addon:
            add_implementation_envs = getattr(
                host_addon, "add_implementation_envs", None
            )
        if add_implementation_envs:
            # Function may only modify passed dict without returning value
            final_env = add_implementation_envs(loaded_env, app)

    if final_env is None:
        final_env = loaded_env

    keys_to_remove = set(source_env.keys()) - set(final_env.keys())

    # Update env
    data["env"].update(final_env)
    for key in keys_to_remove:
        data["env"].pop(key, None)
    data["env"]["AYON_APP_TOOLS"] = ";".join(used_tool_names)


def apply_project_environments_value(
    project_name: str,
    env: dict[str, str],
    project_settings: Optional[dict[str, Any]] = None,
    env_group: Optional[str] = None,
) -> dict[str, str]:
    """Apply project specific environments on passed environments.

    The environments are applied on passed `env` argument value so it is not
    required to apply changes back.

    Args:
        project_name (str): Name of project for which environments should be
            received.
        env (dict): Environment values on which project specific environments
            will be applied.
        project_settings (dict): Project settings for passed project name.
            Optional if project settings are already prepared.

    Returns:
        dict: Passed env values with applied project environments.

    Raises:
        KeyError: If project settings do not contain keys for project specific
            environments.

    """
    if project_settings is None:
        project_settings = get_project_settings(project_name)

    env_value = project_settings["core"]["project_environments"]
    if env_value:
        env_value = json.loads(env_value)
        parsed_value = parse_environments(env_value, env_group)
        env.update(compute_env_variables_structure(
            merge_env_variables(parsed_value, env)
        ))
    return env


def prepare_context_environments(
    data: EnvironmentPrepData,
    env_group: Optional[str] = None,
    addons_manager: Optional[AddonsManager] = None,
) -> None:
    """Modify launch environments with context data for launched host.

    Args:
        data (EnvironmentPrepData): Dictionary where result and intermediate
            result will be stored.

    """
    # Context environments
    log = data["log"]

    project_entity = data["project_entity"]
    folder_entity = data["folder_entity"]
    task_entity = data["task_entity"]
    if not project_entity:
        log.info(
            "Skipping context environments preparation."
            " Launch context does not contain required data."
        )
        return

    # Load project specific environments
    project_name = project_entity["name"]
    project_settings = get_project_settings(project_name)
    data["project_settings"] = project_settings

    app = data["app"]
    context_env = {
        "AYON_PROJECT_NAME": project_entity["name"],
        "AYON_APP_NAME": app.full_name
    }
    if folder_entity:
        folder_path = folder_entity["path"]
        context_env["AYON_FOLDER_PATH"] = folder_path

        if task_entity:
            context_env["AYON_TASK_NAME"] = task_entity["name"]

    log.debug(
        "Context environments set:\n{}".format(
            json.dumps(context_env, indent=4)
        )
    )
    data["env"].update(context_env)

    # Apply project specific environments on current env value
    # - apply them once the context environments are set
    apply_project_environments_value(
        project_name, data["env"], project_settings, env_group
    )

    if not app.is_host:
        return

    data["env"]["AYON_HOST_NAME"] = app.host_name

    if not folder_entity or not task_entity:
        # QUESTION replace with log.info and skip workfile discovery?
        # - technically it should be possible to launch host without context
        raise ApplicationLaunchFailed(
            "Host launch require folder and task context."
        )

    workdir_data = get_template_data(
        project_entity,
        folder_entity,
        task_entity,
        app.host_name,
        project_settings
    )
    data["workdir_data"] = workdir_data

    anatomy = data["anatomy"]

    task_type = workdir_data["task"]["type"]
    # Temp solution how to pass task type to `_prepare_last_workfile`
    data["task_type"] = task_type

    try:
        workdir = get_workdir_with_workdir_data(
            workdir_data,
            anatomy.project_name,
            anatomy,
            project_settings=project_settings
        )

    except Exception as exc:
        raise ApplicationLaunchFailed(
            f"Error in anatomy.format: {exc}"
        )

    if not os.path.exists(workdir):
        log.debug(f"Creating workdir folder: \"{workdir}\"")
        try:
            os.makedirs(workdir, exist_ok=True)
        except Exception as exc:
            raise ApplicationLaunchFailed(
                f"Couldn't create workdir because: {exc}"
            )

    data["env"]["AYON_WORKDIR"] = workdir

    _prepare_last_workfile(data, workdir, addons_manager)


def _prepare_last_workfile(
    data: EnvironmentPrepData,
    workdir: str,
    addons_manager: AddonsManager,
) -> None:
    """last workfile workflow preparation.

    Function check if should care about last workfile workflow and tries
    to find the last workfile. Both information are stored to `data` and
    environments.

    Last workfile is filled always (with version 1) even if any workfile
    exists yet.

    Args:
        data (EnvironmentPrepData): Dictionary where result and intermediate
            result will be stored.
        workdir (str): Path to folder where workfiles should be stored.

    """
    if not addons_manager:
        addons_manager = AddonsManager()

    log = data["log"]

    _workdir_data = data.get("workdir_data")
    if not _workdir_data:
        log.info(
            "Skipping last workfile preparation."
            " Key `workdir_data` not filled."
        )
        return

    app = data["app"]
    workdir_data = copy.deepcopy(_workdir_data)
    project_name = data["project_name"]
    task_name = data["task_name"]
    task_type = data["task_type"]

    start_last_workfile = data.get("start_last_workfile")
    if start_last_workfile is None:
        start_last_workfile = should_use_last_workfile_on_launch(
            project_name, app.host_name, task_name, task_type
        )
    elif start_last_workfile is False:
        log.info("Opening of last workfile was disabled by user")

    data["start_last_workfile"] = start_last_workfile

    workfile_startup = should_open_workfiles_tool_on_launch(
        project_name, app.host_name, task_name, task_type
    )
    data["workfile_startup"] = workfile_startup

    # Store boolean as "0"(False) or "1"(True)
    data["env"]["AVALON_OPEN_LAST_WORKFILE"] = (
        str(int(bool(start_last_workfile)))
    )
    data["env"]["AYON_WORKFILE_TOOL_ON_START"] = (
        str(int(bool(workfile_startup)))
    )

    _sub_msg = "" if start_last_workfile else " not"
    log.debug(
        f"Last workfile should{_sub_msg} be opened on start."
    )

    # Last workfile path
    last_workfile_path = data.get("last_workfile_path") or ""
    if not last_workfile_path:
        host_addon = addons_manager.get_host_addon(app.host_name)
        extensions = None
        if host_addon:
            extensions = host_addon.get_workfile_extensions()

        if extensions:
            anatomy = data["anatomy"]
            project_settings = data["project_settings"]
            task_type = workdir_data["task"]["type"]
            template_key = get_workfile_template_key(
                project_name,
                task_type,
                app.host_name,
                project_settings=project_settings
            )
            # Find last workfile
            file_template = anatomy.get_template_item(
                "work", template_key, "file"
            ).template

            workdir_data.update({
                "version": 1,
                "user": get_ayon_username(),
                "ext": extensions[0]
            })

            last_workfile_path = get_last_workfile(
                workdir, file_template, workdir_data, extensions, True
            )

            # Optionally copy last published workfile into workdir before launch
            use_published = should_copy_last_published_workfile_on_launch(
                project_name,
                app.host_name,
                task_name,
                task_type,
                project_settings=project_settings,
            )
            if use_published and start_last_workfile:
                folder_entity = data.get("folder_entity")
                task_entity = data.get("task_entity")
                if folder_entity and task_entity:
                    published_path = copy_last_published_workfile(
                        project_name=project_name,
                        folder_id=folder_entity["id"],
                        task_id=task_entity["id"],
                        workdir=workdir,
                        extensions=extensions,
                        anatomy=anatomy,
                        file_template=file_template,
                        workdir_data=workdir_data,
                        host_name=app.host_name,
                        project_settings=project_settings,
                        log=log,
                    )
                    if published_path:
                        last_workfile_path = published_path

    if not os.path.exists(last_workfile_path):
        log.debug((
            "Workfiles for launch context does not exists"
            " yet but path will be set."
        ))
    log.debug(
        f"Setting last workfile path: {last_workfile_path}"
    )

    data["env"]["AYON_LAST_WORKFILE"] = last_workfile_path
    data["last_workfile_path"] = last_workfile_path


def get_app_icon_path(icon_filename: str) -> Optional[str]:
    """Get icon path.

    Args:
        icon_filename (str): Icon filename.

    Returns:
        Union[str, None]: Icon path or None if not found.

    """
    if not icon_filename:
        return None
    icon_name = os.path.basename(icon_filename)
    path = os.path.join(APPLICATIONS_ADDON_ROOT, "icons", icon_name)
    if os.path.exists(path):
        return path
    return None
