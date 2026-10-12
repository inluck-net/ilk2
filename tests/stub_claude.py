#!/usr/bin/env python3
"""A stand-in for `claude -p --output-format stream-json`, for tests and the stall suite.

Reads the prompt from stdin, prints an init event, then follows $STUB_SCRIPT (or
$CLAUDE_CONFIG_DIR/stub.json); {"calls": [script, ...]} gives the Nth call its own script.
A script is
{"actions": [...], "resume_actions": [...]} where each action is one of
  {"write": "rel/path", "text": "..."}        write a file in the cwd (via an Edit tool_use)
  {"append": "rel/path", "text": "..."}
  {"sleep": seconds}
  {"hang": true}                              sleep forever (S2)
  {"bash_orphan": seconds}                    double-fork a `sleep` that outlives us
  {"bash_child": seconds}                     a foreground `sleep` child in its own group
  {"api_error": 429}                          end with an API error result
  {"exit": code}                              exit without a result event
  {"ignore_term": true}                       ignore SIGTERM from here on (S17)
Each start or resume appends one line to $CLAUDE_CONFIG_DIR/stub-calls.jsonl.
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid


def emit(ev):
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()


def main():
    args = sys.argv[1:]
    resume = args[args.index("--resume") + 1] if "--resume" in args else None
    prompt = sys.stdin.read()
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", ".")
    script = {}
    p = os.environ.get("STUB_SCRIPT") or os.path.join(cfg, "stub.json")
    if os.path.exists(p):
        with open(p) as f:
            script = json.load(f)
    if "calls" in script:  # a sequence: the Nth start/resume across the test gets calls[N]
        log = p + ".calls"
        n = sum(1 for _ in open(log)) if os.path.exists(log) else 0
        with open(log, "a") as f:
            f.write("x\n")
        seq = script["calls"]
        script = seq[min(n, len(seq) - 1)]
    sid = resume or str(uuid.uuid4())
    with open(os.path.join(cfg, "stub-calls.jsonl"), "a") as f:
        f.write(json.dumps({"resume": resume, "session_id": sid, "prompt_chars": len(prompt),
                            "pid": os.getpid(), "pgid": os.getpgid(0)}) + "\n")
    emit({"type": "system", "subtype": "init", "session_id": sid, "model": "stub-model",
          "cwd": os.getcwd()})
    actions = script.get("resume_actions" if resume and "resume_actions" in script else "actions", [])
    t0 = time.time()
    for a in actions:
        if "write" in a or "append" in a:
            path = a.get("write") or a.get("append")
            emit({"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "deciding"},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": path}}]}})
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a" if "append" in a else "w") as f:
                f.write(a["text"])
        elif "sleep" in a:
            time.sleep(a["sleep"])
        elif a.get("hang"):
            while True:
                time.sleep(3600)
        elif "bash_orphan" in a:
            emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                                 "input": {"command": "sleep &"}}]}})
            subprocess.Popen(["/bin/sh", "-c", f"(sleep {a['bash_orphan']} &)"])
            time.sleep(0.3)
        elif "bash_child" in a:
            emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                                 "input": {"command": "sleep"}}]}})
            subprocess.Popen(["sleep", str(a["bash_child"])], start_new_session=True)
        elif "api_error" in a:
            code = a["api_error"]
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "result": f"API Error: {code} {{\"error\":{{\"message\":\"rate limited\"}}}}",
                  "session_id": sid, "num_turns": 1, "duration_ms": 10, "modelUsage": {}})
            return 1
        elif "exit" in a:
            return a["exit"]
        elif a.get("ignore_term"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "done",
          "session_id": sid, "num_turns": len(actions) + 1,
          "duration_ms": int((time.time() - t0) * 1000), "modelUsage": {"stub-model": {}}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
