import os
import json
import copy

from ayon_server.addons import BaseServerAddon, AddonLibrary

from .settings import ApplicationsAddonSettings, DEFAULT_VALUES


class ApplicationsAddon(BaseServerAddon):
    settings_model = ApplicationsAddonSettings

    async def get_default_settings(self):
        server_dir = os.path.join(self.addon_dir, "server")
        applications_path = os.path.join(server_dir, "applications.json")
        tools_path = os.path.join(server_dir, "tools.json")
        default_values = copy.deepcopy(DEFAULT_VALUES)
        with open(applications_path, "r") as stream:
            default_values.update(json.load(stream))

        with open(tools_path, "r") as stream:
            default_values.update(json.load(stream))

        return self.get_settings_model()(**default_values)
