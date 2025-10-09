# Shell Script PID Tracking Solution for AYON

## Problem
When launching applications through shell scripts on Linux, the process ID returned by the `app_launcher` (mid-process) corresponds to the shell process, not the actual application process that the shell script launches.

## Architecture Overview
- **AYON Applications Manager** (`manager.py`) creates launch contexts and manages application processes
- **App Launcher** (`app_launcher.py`/`app_launcher.cpp` in [ayon-launcher](https://github.com/ynput/ayon-launcher)) is the mid-process that handles detached process launching on Linux
- **Shell Scripts** are often used as executables that set up environment and launch actual applications

## Solution
The solution implements a **PID file convention** that allows shell scripts to communicate the actual application PID back to AYON through the app_launcher mid-process.

### How it Works

1. **PID File Creation**: The AYON manager creates a temporary PID file and provides its path to the app_launcher via JSON data
2. **Environment Variable**: The app_launcher passes the PID file path to shell scripts via `AYON_PID_FILE` environment variable
3. **Shell Script Responsibility**: Shell scripts can write the actual application PID to this file after launching the application
4. **PID Discovery**: The app_launcher waits briefly and checks for an updated PID in the file, using it if found

### Shell Script Implementation Examples

#### Bash Example
```bash
#!/bin/bash

# Launch the actual application in background
/path/to/actual/application "$@" &

# Get the PID of the actual application
APP_PID=$!

# Write the PID to the AYON PID file if available
if [ -n "$AYON_PID_FILE" ]; then
    echo "$APP_PID" > "$AYON_PID_FILE"
fi

# Wait for the application to finish (optional)
wait $APP_PID
```

#### Python Launcher Example
```python
#!/usr/bin/env python3
import os
import subprocess
import sys

# Launch the actual application
process = subprocess.Popen(["/path/to/actual/application"] + sys.argv[1:])

# Write the PID to the AYON PID file if available
pid_file = os.environ.get("AYON_PID_FILE")
if pid_file:
    try:
        with open(pid_file, "w") as f:
            f.write(str(process.pid))
    except OSError:
        pass  # Ignore if we can't write to the file

# Wait for the application to finish
process.wait()
sys.exit(process.returncode)
```

#### Complex Shell Script Example
```bash
#!/bin/bash

# Set up environment
export SOME_APP_PATH="/opt/myapp"
export LD_LIBRARY_PATH="$SOME_APP_PATH/lib:$LD_LIBRARY_PATH"

# Change to application directory
cd "$SOME_APP_PATH"

# Launch the actual application with custom arguments
./myapp --custom-flag --config=/path/to/config "$@" &

# Capture the actual application PID
ACTUAL_PID=$!

# Communicate the PID back to AYON if PID file is available
if [ -n "$AYON_PID_FILE" ]; then
    echo "$ACTUAL_PID" > "$AYON_PID_FILE"
    echo "Wrote actual application PID $ACTUAL_PID to $AYON_PID_FILE"
fi

# Wait for the application
wait $ACTUAL_PID
EXIT_CODE=$?

# Cleanup if needed
cleanup_function

exit $EXIT_CODE
```
### Environment Variables Available to Shell Scripts

`AYON_PID_FILE`: Path to the file where the actual application PID should be written

### Integration Requirements

For shell scripts to take advantage of this feature:
1. Check if `$AYON_PID_FILE` environment variable exists
2. Launch your application in the background using `&`
3. Capture the PID using `$!` (bash) or equivalent
4. Write the PID to the file specified by `$AYON_PID_FILE`
5. Optionally wait for the application to complete