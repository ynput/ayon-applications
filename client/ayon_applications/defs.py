from __future__ import annotations

import os
import platform
import json
import copy
import re
import subprocess
import typing
from typing import Optional, Any, Generator

try:
    from ayon_core.lib import (
        find_executable,
        merge_env_variables,
        compute_env_variables_structure,
    )
except ImportError:
    try:
        from ayon_core.lib.env_tools import (
            merge_env_variables,
            compute_env_variables_structure,
        )
        from ayon_core.lib import find_executable
    except ImportError:
        from ayon_core.lib import find_executable
        merge_env_variables = None
        compute_env_variables_structure = None

if typing.TYPE_CHECKING:
    from ayon_applications.manager import ApplicationManager


class LaunchTypes:
    """Launch types are filters for pre/post-launch hooks.

    Please use these variables in case they'll change values.
    """

    # Local launch - application is launched on local machine
    local = "local"
    # Farm render job - application is on farm
    farm_render = "farm-render"
    # Farm publish job - integration post-render job
    farm_publish = "farm-publish"
    # Remote launch - application is launched on remote machine from which
    #     can be started publishing
    remote = "remote"
    # Automated launch - application is launched with automated publishing
    automated = "automated"


class ApplicationExecutable:
    """Representation of executable loaded from settings."""

    def __init__(
        self,
        executable: str,
        env: Optional[dict[str, str]] = None,
    ):
        """Initialize ApplicationExecutable.

        Args:
            executable (str): Executable path from settings.
            env (Optional[dict[str, str]]): Environment variables mapping for
                formatting and variable expansion. Defaults to `os.environ`.
        """
        if not executable or not isinstance(executable, str):
            self.executable_path = executable or ""
            return

        if env is None:
            env = dict(os.environ)

        executable = self.expand_executable_path(executable, env)

        # On MacOS check if exists path to executable when ends with `.app`
        # - it is common that path will lead to "/Applications/Blender" but
        #   real path is "/Applications/Blender.app"
        if platform.system().lower() == "darwin":
            executable = self.macos_executable_prep(executable)

        self.executable_path = executable

    def __str__(self) -> str:
        return self.executable_path

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}> {self.executable_path}"

    @classmethod
    def expand_executable_path(
        cls,
        executable: str,
        env: dict[str, str],
    ) -> str:
        """Expand user home, environment variables, and format tokens in path.

        Expands:
            - Escaped dollar (`$$` -> `$`)
            - Shell-style environment variables (`$VAR`, `${VAR}`, `%VAR%`)
            - Template tokens (`{VAR}`) matching keys in `env`
            - User home directory (`~` or `~user`) via `os.path.expanduser`

        Args:
            executable (str): Executable path string from settings.
            env (dict[str, str]): Environment variables mapping.

        Returns:
            str: Path with expanded variables and tokens.
        """
        if not executable or not isinstance(executable, str):
            return executable

        is_windows = platform.system().lower() == "windows"
        lookup_env = env
        if is_windows:
            lookup_env = {
                k.lower(): v for k, v in env.items() if v is not None
            }

        def _get_val(key: str) -> Optional[str]:
            if not is_windows:
                val = env.get(key)
            else:
                val = lookup_env.get(key.lower())
            return str(val) if val is not None else None

        # Unified single-pass regex for environment variables and tokens:
        # 1: Escaped dollar ($$)
        # 2: %VAR% or %ProgramFiles(x86)%
        # 3: ${VAR}
        # 4: $VAR
        # 5: {VAR}
        token_pattern = re.compile(
            r"(\$\$)"
            r"|%([a-zA-Z0-9_()]+)%"
            r"|\$\{([a-zA-Z0-9_]+)\}"
            r"|\$([a-zA-Z0-9_]+)"
            r"|\{([a-zA-Z0-9_]+)\}"
        )

        def _replace_token(match: re.Match) -> str:
            if match.group(1):  # $$ -> literal $
                return "$"
            var_name = (
                match.group(2)
                or match.group(3)
                or match.group(4)
                or match.group(5)
            )
            val = _get_val(var_name)
            return val if val is not None else match.group(0)

        executable = token_pattern.sub(_replace_token, executable)

        # Expand user home directory (e.g. ~/bin/app -> /home/user/bin/app)
        executable = os.path.expanduser(executable)

        return executable

    @staticmethod
    def macos_executable_prep(executable: str) -> str:
        """Try to find full path to executable file.

        Real executable is stored in '*.app/Contents/MacOS/<executable>'.

        Having path to '*.app' gives ability to read it's plist info and
        use "CFBundleExecutable" key from plist to know what is "executable."

        Plist is stored in '*.app/Contents/Info.plist'.

        This is because some '*.app' directories don't have same permissions
        as real executable.
        """
        # Try to find if there is `.app` file
        if not os.path.exists(executable):
            _executable = executable + ".app"
            if os.path.exists(_executable):
                executable = _executable

        # Try to find real executable if executable has `Contents` subfolder
        contents_dir = os.path.join(executable, "Contents")
        if os.path.exists(contents_dir):
            executable_filename = None
            # Load plist file and check for bundle executable
            plist_filepath = os.path.join(contents_dir, "Info.plist")
            if os.path.exists(plist_filepath):
                import plistlib

                if hasattr(plistlib, "load"):
                    with open(plist_filepath, "rb") as stream:
                        parsed_plist = plistlib.load(stream)
                else:
                    parsed_plist = plistlib.readPlist(plist_filepath)
                executable_filename = parsed_plist.get("CFBundleExecutable")

            if executable_filename:
                executable = os.path.join(
                    contents_dir, "MacOS", executable_filename
                )

        return executable

    def as_args(self) -> list[str]:
        return [self.executable_path]

    def _realpath(self) -> Optional[str]:
        """Check if path is valid executable path."""
        # Check for executable in PATH
        result = find_executable(self.executable_path)
        if result is not None:
            return result

        # This is not 100% validation but it is better than remove ability to
        #   launch .bat, .sh or extentionless files
        if os.path.isfile(self.executable_path):
            return self.executable_path
        return None

    def exists(self) -> bool:
        if not self.executable_path:
            return False
        return bool(self._realpath())


class UndefinedApplicationExecutable(ApplicationExecutable):
    """Some applications do not require executable path from settings.

    In that case this class is used to "fake" existing executable.
    """
    def __init__(self):
        pass

    def __str__(self) -> str:
        return self.__class__.__name__

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"

    def as_args(self) -> list[str]:
        return []

    def exists(self) -> bool:
        return True


class ApplicationGroup:
    """Hold information about application group.

    Application group wraps different versions(variants) of application.
    e.g. "maya" is group and "maya_2020" is variant.

    Group hold `host_name` which is implementation name used in AYON. Also
    holds `enabled` if whole app group is enabled or `icon` for application
    icon path in resources.

    Group has also `environment` which hold same environments for all variants.

    Args:
        name (str): Groups' name.
        data (dict): Group defying data loaded from settings.
        manager (ApplicationManager): Manager that created the group.

    """
    def __init__(
        self,
        name: str,
        data: dict[str, Any],
        manager: "ApplicationManager",
    ):
        icon = manager.get_app_icon(name)

        label = data.get("label")
        if not label:
            label = manager.get_app_label(name)

        self.name = name
        self.manager = manager
        self._data = data

        self.enabled = data["enabled"]
        self.label = label
        self.icon: dict[str, str] | None = icon
        env = {}
        try:
            env = json.loads(data["environment"])
        except Exception:
            pass
        self._environment = env

        host_name = data["host_name"] or None
        self.is_host = host_name is not None
        self.host_name = host_name

        settings_variants = data["variants"]
        variants = {}
        for variant_data in settings_variants:
            app_variant = Application(variant_data, self)
            variants[app_variant.name] = app_variant

        self.variants = variants

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}> - {self.name}"

    def __iter__(self) -> Generator["Application", None, None]:
        for variant in self.variants.values():
            yield variant

    @property
    def environment(self) -> dict[str, str]:
        return copy.deepcopy(self._environment)


class Application:
    """Hold information about application.

    Object by itself does nothing special.

    Args:
        data (dict): Data for the version containing information about
            executables, variant label or if is enabled.
            Only required key is `executables`.
        group (ApplicationGroup): App group object that created the application
            and under which application belongs.

    """
    def __init__(self, data: dict[str, Any], group: ApplicationGroup):
        self._data = data
        name = data["name"]
        label = data["label"] or name
        enabled = False
        if group.enabled:
            enabled = data.get("enabled", True)

        if group.label:
            full_label = " ".join((group.label, label))
        else:
            full_label = label
        env = {}
        try:
            env = json.loads(data["environment"])
        except Exception:
            pass

        arguments = data["arguments"]
        if isinstance(arguments, dict):
            arguments = arguments.get(platform.system().lower())

        if not arguments:
            arguments = []

        _executables = data["executables"].get(platform.system().lower(), [])
        env_for_executables = self._get_environment_for_executables(
            group_env=group.environment,
            variant_env=env,
        )
        executables = [
            ApplicationExecutable(executable, env=env_for_executables)
            for executable in _executables
        ]

        self.group = group

        self.name = name
        self.label = label
        self.enabled = enabled
        self.use_python_2 = data.get("use_python_2", False)

        self.full_name = f"{group.name}/{name}"
        self.full_label = full_label
        self.arguments = arguments
        self.executables = executables
        self._environment = env
        self.redirect_output = data.get("redirect_output", True)

    def __repr__(self):
        return f"<{self.__class__.__name__}> - {self.full_name}"

    @classmethod
    def _get_environment_for_executables(
        cls,
        group_env: dict[str, Any],
        variant_env: dict[str, Any],
    ) -> dict[str, str]:
        """Prepare flattened and resolved environments for executable paths.

        Merges group and variant environments on top of current system
        environment (os.environ) and resolves dynamic variable structures.

        Args:
            group_env (dict[str, Any]): Application group environment.
            variant_env (dict[str, Any]): Application variant environment.

        Returns:
            dict[str, str]: Fully resolved environment mapping.
        """
        # Fast path if neither group nor variant defines custom environment
        if not group_env and not variant_env:
            return dict(os.environ)

        from .utils import parse_environments

        env_values: dict[str, str] = {}
        for raw_env in (group_env, variant_env):
            if not raw_env or not isinstance(raw_env, dict):
                continue
            parsed = parse_environments(raw_env)
            if not parsed:
                continue
            if merge_env_variables is not None:
                env_values = merge_env_variables(parsed, env_values)
            else:
                env_values.update(parsed)

        if not env_values:
            return dict(os.environ)

        source_env = dict(os.environ)
        if merge_env_variables is not None:
            merged_env = merge_env_variables(env_values, source_env)
        else:
            merged_env = source_env
            merged_env.update(env_values)

        if compute_env_variables_structure is not None:
            try:
                return compute_env_variables_structure(merged_env)
            except Exception:
                pass
        return merged_env

    @property
    def environment(self) -> dict[str, str]:
        return copy.deepcopy(self._environment)

    @property
    def manager(self) -> "ApplicationManager":
        return self.group.manager

    @property
    def host_name(self) -> Optional[str]:
        return self.group.host_name

    @property
    def icon(self) -> dict[str, str] | None:
        return self.group.icon

    @property
    def is_host(self) -> bool:
        return self.group.is_host

    def find_executable(self) -> Optional[ApplicationExecutable]:
        """Try to find existing executable for application.

        Returns (str): Path to executable from `executables` or None if any
            exists.
        """
        for executable in self.executables:
            if executable.exists():
                return executable
        return None

    def launch(self, *args, **kwargs) -> Optional[subprocess.Popen]:
        """Launch the application.

        For this purpose is used manager's launch method to keep logic at one
        place.

        Arguments must match with manager's launch method. That's why *args
        **kwargs are used.

        Returns:
            subprocess.Popen: Return executed process as Popen object.

        """
        return self.manager.launch(self.full_name, *args, **kwargs)


class EnvironmentToolGroup:
    """Hold information about environment tool group.

    Environment tool group may hold different variants of same tool and set
    environments that are same for all of them.

    e.g. "mtoa" may have different versions but all environments except one
        are same.

    Args:
        data (dict): Group information with variants.
        manager (ApplicationManager): Manager that creates the group.

    """
    def __init__(
        self,
        data: dict[str, Any],
        manager: "ApplicationManager",
    ):
        name = data["name"]
        label = data["label"]

        self.name = name
        self.label = label
        self._data = data
        self.manager = manager

        environment = {}
        try:
            environment = json.loads(data["environment"])
        except Exception:
            pass
        self._environment = environment

        variants = data.get("variants") or []
        variants_by_name = {}
        for variant_data in variants:
            tool = EnvironmentTool(variant_data, self)
            variants_by_name[tool.name] = tool
        self.variants = variants_by_name

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}> - {self.name}"

    def __iter__(self) -> Generator["EnvironmentTool", None, None]:
        for variant in self.variants.values():
            yield variant

    @property
    def environment(self) -> dict[str, str]:
        return copy.deepcopy(self._environment)


class EnvironmentTool:
    """Hold information about application tool.

    Structure of tool information.

    Args:
        variant_data (dict): Variant data with environments and
            host and app variant filters.
        group (EnvironmentToolGroup): Name of group which wraps tool.

    """
    def __init__(
        self,
        variant_data: dict[str, Any],
        group: EnvironmentToolGroup,
    ):
        # Backwards compatibility 3.9.1 - 3.9.2
        # - 'variant_data' contained only environments but contain also host
        #   and application variant filters
        name = variant_data["name"]
        label = variant_data["label"]
        host_names = variant_data["host_names"]
        app_variants = variant_data["app_variants"]

        environment = {}
        try:
            environment = json.loads(variant_data["environment"])
        except Exception:
            pass

        self.host_names = host_names
        self.app_variants = app_variants
        self.name = name
        self.variant_label = label
        self.label = " ".join((group.label, label))
        self.group = group

        self._environment = environment
        self.full_name = "/".join((group.name, name))

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}> - {self.full_name}"

    @property
    def environment(self) -> dict[str, str]:
        return copy.deepcopy(self._environment)

    def is_valid_for_app(self, app: Application) -> bool:
        """Is tool valid for an application.

        Args:
            app (Application): Application for which are prepared environments.

        """
        if self.app_variants and app.full_name not in self.app_variants:
            return False

        if self.host_names and app.host_name not in self.host_names:
            return False
        return True
