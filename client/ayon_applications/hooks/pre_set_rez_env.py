import os
import subprocess
import json

from ayon_applications import PreLaunchHook, ApplicationLaunchFailed, \
    LaunchTypes

from ayon_core.lib.vendor_bin_utils import find_executable
from ayon_applications.defs import ApplicationExecutable


class PreLaunchSetRezEnv(PreLaunchHook):
    """Add Rez packages to the launch environment.

    It will merge the parent launch context environment with a rez resolved
    environment defined by the `AYON_REZ_PACKAGES` environment variable.

    The `AYON_REZ_PACKAGES` variable should contain a list of rez package
    names separated by the OS path separator (os.pathsep). This can be
    set in AYON in e.g. application environments or tool environments to build
    up a list of packages to resolve with.

    Note that the rez resolved environment will be resolved with all parent
    variables enabled and will merge into the launch context environment.
    """
    order = -98  # leave some space to egg bootstrap
    # the path to rez itself in a hook
    launch_types = {LaunchTypes.local}

    def execute(self):
        ayon_rez_packages = self.launch_context.env.get("AYON_REZ_PACKAGES")
        if not ayon_rez_packages:
            return
        self.log.info(f"AYON_REZ_PACKAGES: {ayon_rez_packages}")

        packages: list[str] = ayon_rez_packages.split(os.pathsep)
        python_cmd = (
            "import json;"
            "from rez.resolved_context import ResolvedContext;"
            f"print(json.dumps(ResolvedContext({repr(packages)}).get_environ()))"
        )

        # We assume `rez` is available on PATH as command-line and has the rez
        # python available with rez python library so we can resolve the env
        # easily to JSON and merge it into the launch context environment.
        # TODO: If the current environment would have `rez` python package
        #  available then we could avoid the subprocess call here and resolve
        #  directly within current Python process.

        command = [
            "rez",
            "python",
            "-c",
            python_cmd
        ]

        # Enforce upstream environment to be included so that it includes the
        # parent AYON environment completely
        tmp_env = self.launch_context.env.copy()
        tmp_env["REZ_ALL_PARENT_VARIABLES"] = "1"

        # Get the rez resolved enviroment
        result = subprocess.run(
            command,
            env=tmp_env,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            output = ""
            if result.stdout:
                output += result.stdout.decode("utf-8")
            if result.stderr:
                output += result.stderr.decode("utf-8")

            rez_packages = " ".join(packages)
            self.log.error(output)

            # Assume we can parse last line as traceback message
            message = output.splitlines()[-1].split(":", 1)[-1].strip()
            raise ApplicationLaunchFailed(
                f"Rez environment resolution failed for packages: {rez_packages}."
                f"\n\n{message}"
            )
        # self.log.info(result.stdout.decode("utf-8"))
        # Update self.launch_context.env with the rez environment
        rez_env: dict[str, str] = json.loads(result.stdout)

        # Sanitize rez_env: strip leading path separator from values
        # Rez might prepend pathsep to variables if they were empty
        for key, value in rez_env.items():
            if isinstance(value, str) and value.startswith(os.pathsep):
                rez_env[key] = value.lstrip(os.pathsep)

        self.launch_context.env.update(rez_env)

        # filter paths for duplicates
        paths = self.launch_context.env.get("PATH", "").split(os.pathsep)
        # dict.fromkeys is a fast way to get unique items in order
        unique_paths = list(dict.fromkeys(p for p in paths if p))
        self.launch_context.env["PATH"] = os.pathsep.join(unique_paths)

        for k in sorted(self.launch_context.env.keys()):
            v = self.launch_context.env[k]
            self.log.debug(f"{k}={v}")

        # patch the executable in launch_context so later executed prelaunch hooks continue to function
        executable = find_executable(
            str(self.launch_context.executable),
            env=self.launch_context.env)
        self.launch_context.executable = ApplicationExecutable(executable)
