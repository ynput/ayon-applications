import pyblish.api


class IntegrateVersionAppNameData(pyblish.api.InstancePlugin):
    """Add application name to version data."""
    order = pyblish.api.IntegratorOrder - 0.49

    def process(self, instance):
        version_data = instance.data.setdefault("versionData", {})
        version_data["ayon_app_name"] = instance.context.data["appName"]
