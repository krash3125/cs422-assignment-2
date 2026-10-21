#!/bin/bash

set -e
cd "$(dirname "$0")"

if [ "$(uname)" != "Linux" ]; then
    echo "Needs Linux for TCP_INFO/TCP_CONGESTION/BBR, run inside the VM (see README)"
    exit 1
fi

# enable bbr and let non-root sockets pick any of the three
sudo modprobe tcp_bbr
sudo sysctl -q net.ipv4.tcp_allowed_congestion_control="reno cubic bbr"
echo "Available: $(cat /proc/sys/net/ipv4/tcp_available_congestion_control)"

# venv lives outside the shared mount
VENV="$HOME/.venv-cs422"
if [ ! -d "$VENV" ]; then
    echo "Making venv"
    python3 -m venv "$VENV"
fi
source "$VENV/bin/activate"

echo "Installing requirements.txt"
pip install -r requirements.txt > /dev/null

echo "----------------------------------------------"

python run_tests.py
python plot.py
