#!/usr/bin/env bash
# Unified XFCE Desktop + Spine Touch Monitor Stack Runner
# Supports environment overrides:
#   DISPLAY_NUM: X display number (default: 101)
#   RFB_PORT: TigerVNC port (default: 5902)
#   WEB_PORT: websockify HTTP port (default: 6080)
#   CTRL_PORT: mobile control API port (default: 6081)
#   GEOMETRY: desktop resolution (default: 1920x1080)

set -u

DISPLAY_NUM="${DISPLAY_NUM:-101}"
RFB_PORT="${RFB_PORT:-5902}"
WEB_PORT="${WEB_PORT:-6080}"
CTRL_PORT="${CTRL_PORT:-6081}"
GEOMETRY="${GEOMETRY:-1920x1080}"
WEB_ROOT="${HOME}/.local/share/ai-desktop-web"
CTRL_SCRIPT="${HOME}/.local/share/ai-desktop-control/server.py"

pkill -f "Xvnc :${DISPLAY_NUM}" 2>/dev/null || true
pkill -f "websockify.*${WEB_PORT}" 2>/dev/null || true
pkill -f "server.py" 2>/dev/null || true
pkill -f "xfce4-session" 2>/dev/null || true
sleep 1

# 1. TigerVNC X server
Xvnc ":${DISPLAY_NUM}" -geometry "${GEOMETRY}" -depth 24 -rfbport "${RFB_PORT}" -localhost -SecurityTypes None -AlwaysShared > /tmp/desktop-xvnc.log 2>&1 &
XVNC_PID=$!
sleep 2

export DISPLAY=":${DISPLAY_NUM}"
export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# 2. XFCE4 Session
dbus-launch --exit-with-session startxfce4 > /tmp/desktop-xfce.log 2>&1 &
XFCE_PID=$!
sleep 3

# 3. Spine Mouse/Touch Controller API
ARCH_CONTROL_PORT="${CTRL_PORT}" DISPLAY=":${DISPLAY_NUM}" python3 "${CTRL_SCRIPT}" > /tmp/desktop-control.log 2>&1 &
CTRL_PID=$!

# 4. Websockify serving mobile touch web interface
websockify --heartbeat=30 --web="${WEB_ROOT}" "127.0.0.1:${WEB_PORT}" "127.0.0.1:${RFB_PORT}" > /tmp/desktop-websockify.log 2>&1 &
WS_PID=$!

cleanup() {
  kill "$XFCE_PID" "$CTRL_PID" "$WS_PID" "$XVNC_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

wait -n "$XVNC_PID" "$WS_PID" "$CTRL_PID" "$XFCE_PID"