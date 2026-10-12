#!/bin/sh
# P1 step 2: cli_path for the Agent SDK. The SDK spawns the CLI in the driver's own
# process group with the whole inherited environment (claude_agent_sdk
# _internal/transport/subprocess_cli.py:819-881), so this puts it in its own group first.
exec /usr/bin/python3 -I "$(dirname "$0")/sdk_launch.py" "$@"
