import subprocess
from ayon_applications import PreLaunchHook, LaunchTypes


class LaunchTerminalWindowsCreationflags(PreLaunchHook):
    """Avoid running the terminal without new console"""

    # Should be as last hook because must change launch arguments to string
    order = 1000
    app_groups = {"terminal"}
    platforms = {"windows"}
    launch_types = {LaunchTypes.local}

    def execute(self):
        # Change `creationflags` to CREATE_NEW_CONSOLE
        # - on Windows some apps will create new window using its console
        # Set `stdout` and `stderr` to None so new created console does not
        #   have redirected output to DEVNULL in build
        self.launch_context.kwargs.update({
            "creationflags": subprocess.CREATE_NEW_CONSOLE,
            "stdout": None,
            "stderr": None
        })
