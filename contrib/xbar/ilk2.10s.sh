#!/bin/sh
#  <xbar.title>ilk2 status</xbar.title>
#  <xbar.desc>ilk2: ok / ▶ running / ⚠ needs you / ✗ supervisor down.</xbar.desc>
#
# Renders `ilk2 xbar` from the installed release (~/.ilk2/current), so the menu always
# matches the engine that is running. Install: symlink this file into SwiftBar's plugin
# folder. Never prints credentials: the menu shows batch, step and needs_human text only.
PY="$HOME/.ilk2/current/venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "ilk2 ✗ | color=red"
  echo "---"
  echo "ilk2 is not installed (~/.ilk2/current)"
  exit 0
fi
exec "$PY" -m ilk2 xbar
