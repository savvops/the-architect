#!/bin/bash
set -euo pipefail

DISPLAY_NUM="${ARCH_DISPLAY:-:99}"
RESOLUTION="${ARCH_RESOLUTION:-1280x800}"
VNC_BIND="${ARCH_VNC_BIND:-127.0.0.1}"
export DISPLAY="${DISPLAY_NUM}"
export HOME=/data

echo "==> Arch Browser on ${DISPLAY_NUM} (${RESOLUTION}); noVNC ${VNC_BIND}:6080, CDP 127.0.0.1:9222"

mkdir -p /data/profile /data/logs /data/downloads
# Stale locks from an unclean stop (or a profile copied from another host) block Chromium.
rm -f /data/profile/Singleton* "/tmp/.X${DISPLAY_NUM#:}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM#:}"

Xvnc "${DISPLAY_NUM}" -geometry "${RESOLUTION}" -depth 24 -rfbport 5900 -localhost \
  -SecurityTypes None -AlwaysShared > /data/logs/xvnc.log 2>&1 &
sleep 1

websockify --web /usr/share/novnc "${VNC_BIND}:6080" 127.0.0.1:5900 > /data/logs/novnc.log 2>&1 &

# CDP stays on loopback (Chromium default when no --remote-debugging-address is given).
/usr/bin/chromium \
  --remote-debugging-port=9222 \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --no-first-run \
  --no-default-browser-check \
  --disable-blink-features=AutomationControlled \
  --start-maximized \
  --window-size="${RESOLUTION/x/,}" \
  --user-data-dir=/data/profile \
  about:blank > /data/logs/chrome.log 2>&1 &

for i in $(seq 1 30); do
  if curl -fs http://127.0.0.1:9222/json/version > /dev/null; then
    echo "==> DevTools ready after ${i}s"
    break
  fi
  sleep 1
done

exec python3 /app/archd.py
