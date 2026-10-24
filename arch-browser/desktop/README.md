# Unified Desktop Workstation & Spine Touch Monitor

This package turns any Linux machine or VM (Oracle VM, Spine, Legion, etc.) into a **unified workstation** where graphical apps (Chromium, 3D slicers like OrcaSlicer/Bambu Studio, terminals) run on a single 1080p XFCE desktop session, controllable via a phone-friendly touch HUD and automated by the Arch Browser agent daemon.

## Features

- **Single Desktop Session (`DISPLAY=:101`)**: Eliminates fragmented, multi-port setups. Any app you open shares the same display, clipboard, and filesystem.
- **Spine Touch Monitor Parity**: Decoupled OS-level `xdotool` controller on port `6081` with read-only noVNC streaming on port `6080`. Offers an analog joystick, continuous scroll wheel, primary/secondary click cluster, touch gestures, and native mobile keyboard summoning.
- **Autonomous Arch Browser Daemon**: Persistent `archd` service connecting to the visible desktop Chromium via Chrome DevTools Protocol (`--remote-debugging-port=9222`). AI agents can navigate, click, fill forms, and run tasks while you watch live.
- **Tailscale Serve Ready**: Exposes the desktop securely on tailnet port `6080` (or `8445`) with zero exposed public ports.

## 1-Command Installation

On any Debian / Ubuntu host:

```bash
cd arch-browser/desktop
./install.sh
```

## Tailscale Serve Setup

Expose the touch monitor on port `6080`:

```bash
sudo tailscale serve --bg --https=6080 --set-path=/ http://127.0.0.1:6080
sudo tailscale serve --bg --https=6080 --set-path=/control http://127.0.0.1:6081
```

Now open on your phone or tablet:
`https://<node-name>.taila7272b.ts.net:6080/mobile.html?v=12`

## Architecture & Ports

| Component | Port | Description |
|-----------|------|-------------|
| **TigerVNC** (`Xvnc`) | `5902` (RFB) | Virtual X11 server on `DISPLAY=:101` (1920x1080) |
| **WebSockify** | `6080` (HTTP/WS) | Proxies RFB to WebSocket and serves `ai-desktop-web` |
| **Control Server** (`server.py`) | `6081` (HTTP) | REST API executing `xdotool` for pointer, scroll, and key inputs |
| **Chromium CDP** | `9222` (HTTP/WS) | Chrome DevTools Protocol for browser automation |
| **Arch Daemon** (`archd.py`) | `8901` (HTTP) | Multi-tier decision router, numbered DOM, and lease broker |

## CLI Commands

From anywhere on the host:

```bash
# Check browser state
arch status

# Navigate the desktop browser
arch goto https://makerworld.com

# Inspect numbered DOM elements
arch dom

# Click element #3
arch click 3

# Execute background AI task using cheapest model
arch task "search for voron 2.4 parts list"
```