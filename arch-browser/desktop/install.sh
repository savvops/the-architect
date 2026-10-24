#!/usr/bin/env bash
# ==============================================================================
# Arch Browser & Unified Desktop Workstation Installer
# Sets up a unified 1080p desktop (XFCE + TigerVNC + Spine Touch Monitor)
# and persistent Arch Browser agent daemon on any Debian/Ubuntu VM or device.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCH_DIR="$(dirname "${SCRIPT_DIR}")"

echo "=== Arch Browser & Unified Desktop Installer ==="

# 1. System packages
echo "[1/7] Installing prerequisite packages..."
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y --no-install-recommends \
    tigervnc-standalone-server \
    xfce4 \
    xfce4-terminal \
    novnc \
    python3-novnc \
    websockify \
    xdotool \
    dbus-x11 \
    curl \
    ca-certificates \
    imagemagick
else
  echo "Warning: apt-get not found. Ensure TigerVNC, XFCE, websockify, and xdotool are installed."
fi

# 2. CLI tool
echo "[2/7] Installing arch CLI to /usr/local/bin/arch..."
if [ -f "${ARCH_DIR}/bin/arch" ]; then
  sudo cp "${ARCH_DIR}/bin/arch" /usr/local/bin/arch
  sudo chmod +x /usr/local/bin/arch
fi

# 3. Mobile touch monitor web assets
echo "[3/7] Setting up ai-desktop-web..."
WEB_DIR="${HOME}/.local/share/ai-desktop-web"
mkdir -p "${WEB_DIR}/ui"
cp -r "${SCRIPT_DIR}/web/"* "${WEB_DIR}/"

# Link standard noVNC directories if available
if [ -d "/usr/share/novnc" ]; then
  for item in app core include utils vendor; do
    [ -d "/usr/share/novnc/${item}" ] && ln -sf "/usr/share/novnc/${item}" "${WEB_DIR}/${item}"
  done
  [ -f "/usr/share/novnc/vnc.html" ] && ln -sf "/usr/share/novnc/vnc.html" "${WEB_DIR}/classic.html"
fi

# Install npm dependencies for mobile UI if npm is present
if command -v npm >/dev/null 2>&1 && [ -f "${WEB_DIR}/ui/package.json" ]; then
  echo "Installing UI dependencies via npm..."
  (cd "${WEB_DIR}/ui" && npm install --silent) || true
fi

# 4. OS-level mouse/keyboard controller
echo "[4/7] Setting up ai-desktop-control..."
CTRL_DIR="${HOME}/.local/share/ai-desktop-control"
mkdir -p "${CTRL_DIR}"
cp "${SCRIPT_DIR}/server.py" "${CTRL_DIR}/server.py"
chmod +x "${CTRL_DIR}/server.py"

# 5. Desktop stack runner
echo "[5/7] Installing run-desktop-stack.sh..."
BIN_DIR="${HOME}/.local/bin"
mkdir -p "${BIN_DIR}"
cp "${SCRIPT_DIR}/run-desktop-stack.sh" "${BIN_DIR}/run-desktop-stack.sh"
chmod +x "${BIN_DIR}/run-desktop-stack.sh"

# 6. Autostart Chromium with remote debugging port
echo "[6/7] Configuring XFCE autostart for Chromium..."
AUTOSTART_DIR="${HOME}/.config/autostart"
mkdir -p "${AUTOSTART_DIR}"
cat << 'EOF_AUTO' > "${AUTOSTART_DIR}/chromium.desktop"
[Desktop Entry]
Encoding=UTF-8
Version=0.9.4
Type=Application
Name=Chromium
Comment=Chromium Browser with CDP remote debugging
Exec=/usr/bin/chromium --no-first-run --no-default-browser-check --disable-session-crashed-bubble --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 about:blank
OnlyShowIn=XFCE;
StartupNotify=false
Terminal=false
Hidden=false
EOF_AUTO
# If chromium is a snap, adjust path
if [ -x "/snap/bin/chromium" ] && [ ! -x "/usr/bin/chromium" ]; then
  sed -i 's|/usr/bin/chromium|/snap/bin/chromium|g' "${AUTOSTART_DIR}/chromium.desktop"
fi

# 7. Systemd user services
echo "[7/7] Configuring systemd user services..."
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
mkdir -p "${SYSTEMD_USER_DIR}"
cp "${SCRIPT_DIR}/desktop-vnc.service" "${SYSTEMD_USER_DIR}/desktop-vnc.service"
cp "${SCRIPT_DIR}/arch-daemon.service" "${SYSTEMD_USER_DIR}/arch-daemon.service"

# Update paths in arch-daemon.service to absolute path of archd.py
sed -i "s|%h/the-architect/arch-browser/archd.py|${ARCH_DIR}/archd.py|g" "${SYSTEMD_USER_DIR}/arch-daemon.service"

systemctl --user daemon-reload
systemctl --user enable --now desktop-vnc.service
systemctl --user enable --now arch-daemon.service

echo ""
echo "=== Installation Complete ==="
echo "Desktop VNC is running on DISPLAY=:101 (rfb port 5902)"
echo "Web touch monitor listening on http://127.0.0.1:6080"
echo "Mobile control API listening on http://127.0.0.1:6081"
echo "Arch Browser daemon listening on http://127.0.0.1:8901"
echo ""
echo "To share securely over Tailscale Serve, run:"
echo "  sudo tailscale serve --bg --https=6080 --set-path=/ http://127.0.0.1:6080"
echo "  sudo tailscale serve --bg --https=6080 --set-path=/control http://127.0.0.1:6081"
echo ""
echo "Access on your phone: https://<your-tailscale-node>:6080/mobile.html?v=12"