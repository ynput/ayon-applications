import semver

from ayon_server.addons import AddonLibrary
from ayon_server.entities.core import attribute_library
from ayon_server.lib.postgres import Postgres


def parse_versions(versions):
    version_objs = []
    invalid_versions = []
    output = (version_objs, invalid_versions)
    for version in versions:
        try:
            version_objs.append(
                (version, semver.VersionInfo.parse(version))
            )
        except ValueError:
            invalid_versions.append(version)
    return output


class ApplicationsLE_0_2:
    def _sort_versions(self, addon_versions, reverse=False):
        version_objs, invalid_versions = parse_versions(addon_versions)

        valid_versions = [
            addon_version
            for addon_version, version_obj in (
                sorted(version_objs, key=lambda x: x[1])
            )
            # Skip versions greater than 0.2
            if (version_obj.major, version_obj.minor) <= (0, 2)
        ]
        sorted_versions = list(sorted(invalid_versions)) + valid_versions
        if reverse:
            sorted_versions = reversed(sorted_versions)
        for addon_version in sorted_versions:
            yield addon_version

    def _merge_groups(self, output, new_groups):
        groups_by_name = {
            o_group["name"]: o_group
            for o_group in output
        }
        extend_groups = []
        for new_group in new_groups:
            group_name = new_group["name"]
            if group_name not in groups_by_name:
                extend_groups.append(new_group)
                continue
            existing_group = groups_by_name[group_name]
            existing_variants = existing_group["variants"]
            existing_variants_by_name = {
                variant["name"]: variant
                for variant in existing_variants
            }
            for new_variant in new_group["variants"]:
                if new_variant["name"] not in existing_variants_by_name:
                    existing_variants.append(new_variant)

        output.extend(extend_groups)

    def _get_enum_items_from_groups(self, groups):
        label_by_name = {}
        for group in groups:
            group_name = group["name"]
            group_label = group["label"] or group_name
            for variant in group["variants"]:
                variant_name = variant["name"]
                if not variant_name:
                    continue
                variant_label = variant["label"] or variant_name
                full_name = f"{group_name}/{variant_name}"
                full_label = f"{group_label} {variant_label}"
                label_by_name[full_name] = full_label

        return [
            {"value": full_name, "label": label_by_name[full_name]}
            for full_name in sorted(label_by_name)
        ]

    async def _update_enums(self):
        """Updates applications and tools enums based on the addon settings.
        This method is called when the addon is started (after we are sure that the
        'applications' and 'tools' attributes exist) and when the addon settings are
        updated (using on_settings_updated method).
        """

        instance = AddonLibrary.getinstance()
        app_defs = instance.data.get(self.name)
        all_applications = []
        all_tools = []
        for addon_version in self._sort_versions(
            app_defs.versions.keys(), reverse=True
        ):
            addon = app_defs.versions[addon_version]
            for variant in ("production", "staging"):
                settings_model = await addon.get_studio_settings(variant)
                studio_settings = settings_model.dict()
                application_settings = studio_settings["applications"]
                app_groups = application_settings.pop("additional_apps")
                for group_name, value in application_settings.items():
                    value["name"] = group_name
                    app_groups.append(value)
                self._merge_groups(all_applications, app_groups)
                self._merge_groups(all_tools, studio_settings["tool_groups"])

        apps_attrib_name = "applications"
        tools_attrib_name = "tools"

        apps_enum = self._get_enum_items_from_groups(all_applications)
        tools_enum = self._get_enum_items_from_groups(all_tools)

        apps_attribute_data = {
            "type": "list_of_strings",
            "title": "Applications",
            "enum": apps_enum,
        }
        tools_attribute_data = {
            "type": "list_of_strings",
            "title": "Tools",
            "enum": tools_enum,
        }

        apps_scope = ["project"]
        tools_scope = ["project", "folder", "task"]

        apps_matches = False
        tools_matches = False

        async for row in Postgres.iterate(
            "SELECT name, position, scope, data from public.attributes"
        ):
            if row["name"] == apps_attrib_name:
                # Check if scope is matching ftrack addon requirements
                if (
                    set(row["scope"]) == set(apps_scope)
                    and row["data"].get("enum") == apps_enum
                ):
                    apps_matches = True

            elif row["name"] == tools_attrib_name:
                if (
                    set(row["scope"]) == set(tools_scope)
                    and row["data"].get("enum") == tools_enum
                ):
                    tools_matches = True

        if apps_matches and tools_matches:
            return

        if not apps_matches:
            await Postgres.execute(
                """
                UPDATE attributes SET
                    scope = $1,
                    data = $2
                WHERE 
                    name = $3
                """,
                apps_scope,
                apps_attribute_data,
                apps_attrib_name,
            )

        if not tools_matches:
            await Postgres.execute(
                """
                UPDATE attributes SET
                    scope = $1,
                    data = $2
                WHERE 
                    name = $3
                """,
                tools_scope,
                tools_attribute_data,
                tools_attrib_name,
            )

        # Reset attributes cache on server
        await attribute_library.load()