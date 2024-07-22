from ayon_server.addons import BaseServerAddon, AddonLibrary

from .settings import ApplicationsAddonSettings, DEFAULT_VALUES


class ApplicationsAddon(BaseServerAddon):
    settings_model = ApplicationsAddonSettings

    async def get_default_settings(self):
        return self.get_settings_model()(**DEFAULT_VALUES)
