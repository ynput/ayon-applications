from ayon_server.addons import BaseServerAddon, AddonLibrary

from ._backwards import ApplicationsLE_0_2, parse_versions
from .settings import ApplicationsAddonSettings, DEFAULT_VALUES



class ApplicationsAddon(BaseServerAddon):
    settings_model = ApplicationsAddonSettings
    # Backwards compatibility for addons older than
    app_bw_lt_0_2 = ApplicationsLE_0_2()

    async def get_default_settings(self):
        return self.get_settings_model()(**DEFAULT_VALUES)

    async def pre_setup(self):
        """Make sure older version of addon use the new way of attributes."""

        instance = AddonLibrary.getinstance()
        app_defs = instance.data.get(self.name)
        old_addon = app_defs.versions.get("0.1.0")
        if old_addon is not None:
            # Override 'create_applications_attribute' for older versions
            #   - avoid infinite server restart loop
            old_addon.create_applications_attribute = (
                self.create_applications_attribute
            )

        version_objs, invalid_versions = parse_versions(app_defs.versions)
        for addon_version, version_obj in version_objs:
            if (version_obj.major, version_obj.minor) > (0, 2):
                continue
            addon = app_defs.versions[addon_version]
            addon._update_enums = self.app_bw_lt_0_2._update_enums
