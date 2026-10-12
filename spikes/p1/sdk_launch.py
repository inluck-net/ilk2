"""Exec target behind sdk_launch.sh: new session, pid file, provider env scrubbed, optional sandbox.

ILK2_CLI: the claude binary to exec. ILK2_PID_FILE: where to write our pid (== pgid).
ILK2_SANDBOX_PROFILE: if set, exec through sandbox-exec with that profile.
Only ANTHROPIC_* is scrubbed: the SDK's own CLAUDE_CODE_* variables carry its protocol.
"""
import os
import sys

os.setsid()
env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
pid_file = env.get("ILK2_PID_FILE")
if pid_file:
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))
cli = env["ILK2_CLI"]
argv = [cli, *sys.argv[1:]]
profile = env.get("ILK2_SANDBOX_PROFILE")
if profile:
    argv = ["/usr/bin/sandbox-exec", "-f", profile, *argv]
os.execve(argv[0], argv, env)
