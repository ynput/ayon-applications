from __future__ import annotations

import platform
import logging
from abc import ABC, abstractmethod
import warnings
import typing
from typing import Optional, Any

from ayon_core.lib import Logger
from ayon_core.addon import AddonsManager

from .defs import LaunchTypes, ApplicationGroup, Application

if typing.TYPE_CHECKING:
    from .manager import ApplicationLaunchContext


class LaunchHook(ABC):
    """Abstract base class of launch hook."""
    # Order of prelaunch hook, will be executed as last if set to None.
    order: Optional[int] = None
    # List of host implementations, skipped if empty.
    hosts: set[str] = set()
    # Set of application groups
    app_groups: set[str] = set()
    # Set of specific application names
    app_names: set[str] = set()
    # Set of platform availability
    platforms: set[str] = set()
    # Set of launch types for which is available
    # - if empty then is available for all launch types
    # - by default has 'local' which is most common reason for launc hooks
    launch_types: set[str] = {LaunchTypes.local}

    def __init__(self, launch_context: "ApplicationLaunchContext"):
        """Constructor of launch hook.

        Always should be called
        """
        self.log: logging.Logger = Logger.get_logger(self.__class__.__name__)

        self.launch_context: "ApplicationLaunchContext" = launch_context

        is_valid = self.class_validation(launch_context)
        if is_valid:
            is_valid = self.validate()

        self.is_valid: bool = is_valid

    @classmethod
    def class_validation(
        cls, launch_context: "ApplicationLaunchContext"
    ) -> bool:
        """Validation of class attributes by launch context.

        Args:
            launch_context (ApplicationLaunchContext): Context of launching
                application.

        Returns:
            bool: Is launch hook valid for the context by class attributes.
        """
        if cls.platforms:
            low_platforms = tuple(
                _platform.lower()
                for _platform in cls.platforms
            )
            if platform.system().lower() not in low_platforms:
                return False

        if cls.hosts:
            if launch_context.host_name not in cls.hosts:
                return False

        if cls.app_groups:
            if launch_context.app_group.name not in cls.app_groups:
                return False

        if cls.app_names:
            if launch_context.app_name not in cls.app_names:
                return False

        if cls.launch_types:
            if launch_context.launch_type not in cls.launch_types:
                return False

        return True

    @property
    def data(self) -> dict[str, Any]:
        return self.launch_context.data

    @property
    def application(self) -> Application:
        return self.launch_context.application

    @property
    def manager(self):
        return self.application.manager

    @property
    def host_name(self) -> Optional[str]:
        return self.application.host_name

    @property
    def app_group(self) -> ApplicationGroup:
        return self.application.group

    @property
    def app_name(self) -> str:
        return self.application.full_name

    @property
    def addons_manager(self) -> AddonsManager:
        return self.launch_context.addons_manager

    @property
    def modules_manager(self):
        """
        Deprecated:
            Use 'addons_wrapper' instead.

        """
        warnings.warn(
            "Used deprecated 'modules_manager' attribute,"
            " use 'addons_manager' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.addons_manager

    def validate(self) -> bool:
        """Optional validation of launch hook on initialization.

        Returns:
            bool: Hook is valid (True) or invalid (False).

        """
        # QUESTION Not sure if this method has any usable potential.
        # - maybe result can be based on settings
        return True

    @abstractmethod
    def execute(self) -> None:
        """Abstract execute method where logic of hook is."""
        pass


class PreLaunchHook(LaunchHook):
    """Abstract class of prelaunch hook.

    This launch hook will be processed before application is launched.

    If any exception will happen during processing the application won't be
    launched.
    """


class PostLaunchHook(LaunchHook):
    """Abstract class of postlaunch hook.

    This launch hook will be processed after application is launched.

    Nothing will happen if any exception will happen during processing. And
    processing of other postlaunch hooks won't stop either.
    """
