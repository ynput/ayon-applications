import pyblish.api


class IntegrateVersionAppNameData(pyblish.api.InstancePlugin):
    """Add application name to version data."""
    label = "Add app name to version data"
    order = pyblish.api.IntegratorOrder - 0.49

    def process(self, instance):
        version_data = instance.data.setdefault("versionData", {})
        app_name: str = instance.context.data["appName"]
        version_data["ayon_app_name"] = app_name
        self.log.debug(f"Version data 'ayon_app_name' set to: {app_name}")
