#!/bin/bash
set -e

DISPLAY_NUM="${ARCH_DISPLAY:-:99}"
RESOLUTION="${ARCH_RESOLUTION:-1280x800}"
export DISPLAY="${DISPLAY_NUM}"
export HOME=/data

echo "==> Starting Arch Browser (The Architect) on ${DISPLAY_NUM} (${RESOLUTION})..."

# Ensure profile directory exists
mkdir -p /data/profile /data/logs

# 1. Start virtual display Xvnc on :99 (rfbport 5900)
Xvnc "${DISPLAY_NUM}" -geometry "${RESOLUTION}" -depth 24 -rfbport 5900 -localhost -SecurityTypes None -AlwaysShared > /data/logs/xvnc.log 2>&1 &
XVNC_PID=$!
sleep 1

# 2. Start websockify / noVNC on 6080
websockify --web /usr/share/novnc 0.0.0.0:6080 localhost:5900 > /data/logs/novnc.log 2>&1 &
NOVNC_PID=$!

# 3. Start cdp_bridge.py (spawns headed Chromium with pipe CDP on 127.0.0.1:9222)
python3 /app/cdp_bridge.py \
    --chrome /usr/bin/chromium \
    --profile /data/profile \
    --display "${DISPLAY_NUM}" \
    --window-size "${RESOLUTION}" \
    --log /data/logs/chrome.log > /data/logs/cdp_bridge.log 2>&1 &
CDP_PID=$!
sleep 2

# 4. Start archd daemon (S-Tier REST API + Model Router on 0.0.0.0:8901)
echo "==> Launching archd S-Tier daemon on :8901..."
python3 /app/archd.py