#!/bin/bash
set -e

DISPLAY_NUM="${ARCH_DISPLAY:-:99}"
RESOLUTION="${ARCH_RESOLUTION:-1280x800}"
export DISPLAY="${DISPLAY_NUM}"
export HOME=/data

echo "==> Starting Arch Browser (The Architect) on ${DISPLAY_NUM} (${RESOLUTION})..."

# Ensure profile directory exists and clean stale locks
mkdir -p /data/profile /data/logs
rm -f /data/profile/Singleton* /tmp/.X99-lock /tmp/.X11-unix/X99

# 1. Start virtual display Xvnc on :99 (rfbport 5900)
Xvnc "${DISPLAY_NUM}" -geometry "${RESOLUTION}" -depth 24 -rfbport 5900 -localhost -SecurityTypes None -AlwaysShared > /data/logs/xvnc.log 2>&1 &
sleep 1

# 2. Start websockify / noVNC on 6080
websockify --web /usr/share/novnc 0.0.0.0:6080 localhost:5900 > /data/logs/novnc.log 2>&1 &
sleep 1

# 3. Launch native Chromium with DevTools port
echo "==> Launching Chromium on display ${DISPLAY_NUM}..."
/usr/bin/chromium \
  --remote-debugging-port=9222 \
  --remote-debugging-address=0.0.0.0 \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --no-first-run \
  --no-default-browser-check \
  --disable-blink-features=AutomationControlled \
  --start-maximized \
  --window-size="${RESOLUTION}" \
  --user-data-dir=/data/profile \
  about:blank > /data/logs/chrome.log 2>&1 &

# 4. Wait for DevTools port to be ready
echo "==> Waiting for DevTools endpoint on 127.0.0.1:9222..."
for i in $(seq 1 30); do
  if curl -s http://127.0.0.1:9222/json/version > /dev/null; then
    echo "==> DevTools is online and answering after ${i}s."
    break
  fi
  sleep 1
done

# 5. Start archd daemon (S-Tier REST API + Model Router on 0.0.0.0:8901)
echo "==> Launching archd S-Tier daemon on :8901..."
exec python3 /app/archd.py