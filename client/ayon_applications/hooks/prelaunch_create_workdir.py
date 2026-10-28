import os

from ayon_applications import (
    ApplicationLaunchFailed,
    LaunchTypes,
    PreLaunchHook,
)


class CreateWorkdirHook(PreLaunchHook):
    """Create workdir folder for host application on local launch."""

    # Execute right after GlobalHostDataHook (-100) and before hooks
    # that copy templates or create extra folders in workdir.
    order = -90
    launch_types = {LaunchTypes.local}

    def execute(self) -> None:
        if not self.application.is_host:
            return

        workdir = self.launch_context.env.get("AYON_WORKDIR")
        if not workdir:
            return

        if not os.path.exists(workdir):
            self.log.debug(f'Creating workdir folder: "{workdir}"')
            try:
                os.makedirs(workdir, exist_ok=True)
            except Exception as exc:
                raise ApplicationLaunchFailed(
                    f"Couldn't create workdir because: {exc}"
                ) from exc
