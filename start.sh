#!/bin/bash
# start.sh - start the whole project with ONE command (run as your normal user, not with sudo)
#
#   cd ~/cn-d2 && ./start.sh
#
# It cleans old processes, makes sure Flask is available, starts the web manager as root
# (Mininet needs root) and opens the browser at http://127.0.0.1:8000.
# Everything else (Ryu, Mininet, game, tests, experiments) is controlled from the web page.
# Stop with Ctrl-C in this terminal (or "Stop everything" in the page, then Ctrl-C).

cd "$(dirname "$0")" || exit 1
DIR="$(pwd)"

if [ "$(id -u)" = "0" ]; then echo "Run ./start.sh as your normal user (it uses sudo itself)."; exit 1; fi
for tool in ovs-vsctl ovs-ofctl tc mn; do
  command -v $tool >/dev/null 2>&1 || { echo "Missing tool: $tool  (sudo apt install mininet openvswitch-switch iproute2)"; exit 1; }
done

# Ryu (existing virtual environment from D1 - not modified)
RYU="${RYU_MANAGER:-$HOME/cn-project/ryu-env/bin/ryu-manager}"
if [ ! -x "$RYU" ]; then
  RYU="$(command -v ryu-manager)"
fi
if [ -z "$RYU" ] || [ ! -x "$RYU" ]; then
  echo "ryu-manager not found. Run:  RYU_MANAGER=/path/to/ryu-env/bin/ryu-manager ./start.sh"; exit 1
fi
echo "Using Ryu: $RYU"

# Flask for the web UI
PY=python3
if ! python3 -c "import flask" 2>/dev/null; then
  echo "Flask not found - creating a local virtual environment (needs internet once) ..."
  [ -d .venv ] || python3 -m venv --system-site-packages .venv || exit 1
  .venv/bin/pip install -q flask || { echo "pip install flask failed"; exit 1; }
  PY="$DIR/.venv/bin/python"
fi

echo "Cleaning old Mininet / Ryu processes ..."
pkill -f ryu-manager 2>/dev/null
sudo pkill -f "manager.py" 2>/dev/null
sudo mn -c >/dev/null 2>&1       # NOTE: mn -c also kills ryu-manager, so it runs BEFORE the manager starts Ryu
mkdir -p logs results

( for i in $(seq 1 30); do sleep 1; python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=1)" 2>/dev/null && { xdg-open http://127.0.0.1:8000 >/dev/null 2>&1; break; }; done ) >/dev/null 2>&1 &

echo "Starting the web manager on http://127.0.0.1:8000 (sudo password may be asked) ..."
sudo env RYU_MANAGER="$RYU" "$PY" manager.py
sudo chown -R "$(id -un)":"$(id -gn)" logs results 2>/dev/null
