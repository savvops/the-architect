#!/usr/bin/env bash
# Unified XFCE Desktop + Spine Touch Monitor Stack Runner
# Runs on DISPLAY=:101 (rfbport 5902)
# Web monitor on :6080 and :6082, control API on :6081
set -u

DISPLAY_NUM="${DISPLAY_NUM:-101}"
RFB_PORT="${RFB_PORT:-5902}"
WEB_PORT="${WEB_PORT:-6080}"
CTRL_PORT="${CTRL_PORT:-6081}"
GEOMETRY="${GEOMETRY:-1920x1080}"
WEB_ROOT="${HOME}/.local/share/ai-desktop-web"
CTRL_SCRIPT="${HOME}/.local/share/ai-desktop-control/server.py"

pkill -f "Xvnc :${DISPLAY_NUM}" 2>/dev/null || true
pkill -f "Xvnc :102" 2>/dev/null || true
pkill -f "websockify.*${WEB_PORT}" 2>/dev/null || true
pkill -f "websockify.*6082" 2>/dev/null || true
pkill -f "websockify.*6084" 2>/dev/null || true
pkill -f "ai-desktop-control/server" 2>/dev/null || true
pkill -f "xfce4-session" 2>/dev/null || true
sleep 1

# 1. TigerVNC X server on :101
Xvnc ":${DISPLAY_NUM}" -geometry "${GEOMETRY}" -depth 24 -rfbport "${RFB_PORT}" -localhost -SecurityTypes None -AlwaysShared > /tmp/desktop-xvnc.log 2>&1 &
XVNC_PID=$!
sleep 2

export DISPLAY=":${DISPLAY_NUM}"
export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# 2. XFCE4 Session
dbus-launch --exit-with-session startxfce4 > /tmp/desktop-xfce.log 2>&1 &
XFCE_PID=$!
sleep 3

# 3. Spine Mouse/Touch Controller API on :6081
CTRL_PORT="${CTRL_PORT}" DISPLAY=":${DISPLAY_NUM}" python3 "${CTRL_SCRIPT}" > /tmp/desktop-control.log 2>&1 &
CTRL_PID=$!

# 4. Websockify serving ai-desktop-web on :6080
websockify --heartbeat=30 --web="${WEB_ROOT}" "127.0.0.1:${WEB_PORT}" "127.0.0.1:${RFB_PORT}" > /tmp/desktop-websockify.log 2>&1 &
WS_PID=$!

# 5. Secondary websockify on :6082 for 8445 serve proxy
websockify --heartbeat=30 --web="${WEB_ROOT}" 127.0.0.1:6082 "127.0.0.1:${RFB_PORT}" > /tmp/desktop-websockify-6082.log 2>&1 &
WS2_PID=$!

# 6. Launch Unified Browser (Chromium with CDP :9222) if not already running
if ! pgrep -f "remote-debugging-port=9222" >/dev/null 2>&1; then
  if [ -x "/snap/bin/chromium" ]; then
    /snap/bin/chromium --no-first-run --no-default-browser-check --disable-session-crashed-bubble --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 > /tmp/chromium.log 2>&1 &
  elif command -v chromium >/dev/null 2>&1; then
    chromium --no-first-run --no-default-browser-check --disable-session-crashed-bubble --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 > /tmp/chromium.log 2>&1 &
  fi
fi

# 7. Launch Antigravity IDE on the unified display if installed
AG_BIN="${HOME}/projects/tools/antigravity/Antigravity-arm64/antigravity"
if [ -x "${AG_BIN}" ] && ! pgrep -f "Antigravity-arm64/antigravity" >/dev/null 2>&1; then
  "${AG_BIN}" --no-sandbox --disable-gpu-sandbox > /tmp/antigravity.log 2>&1 &
fi

cleanup() {
  kill "$XFCE_PID" "$CTRL_PID" "$WS_PID" "$WS2_PID" "$XVNC_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

wait -n "$XVNC_PID" "$WS_PID" "$CTRL_PID" "$XFCE_PID"
