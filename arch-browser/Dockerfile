FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    tigervnc-standalone-server \
    novnc \
    python3 \
    python3-websockify \
    chromium \
    fonts-liberation \
    fonts-dejavu-core \
    fonts-noto-color-emoji \
    procps \
    curl \
    ca-certificates \
    libnss3-tools \
    dbus-x11 \
    xauth \
    xdotool \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 -s /bin/bash arch \
 && mkdir -p /tmp/.X11-unix /data/profile /data/logs /data/downloads /app \
 && chmod 1777 /tmp/.X11-unix \
 && chown -R arch:arch /data /app

WORKDIR /app

COPY --chown=arch:arch cdp_bridge.py archd.py router.py entrypoint.sh /app/
COPY --chown=arch:arch bin/ /app/bin/
COPY --chown=arch:arch ui/ /app/ui/
COPY --chown=arch:arch tests/ /app/tests/

RUN chmod +x /app/entrypoint.sh /app/bin/arch /app/cdp_bridge.py /app/archd.py /app/router.py

USER arch

# Loopback-only by default (ARCH_BIND / ARCH_VNC_BIND); publish via Tailscale Serve, not -p.
EXPOSE 6080 8901

VOLUME ["/data/profile"]

HEALTHCHECK --interval=20s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8901/healthz >/dev/null || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]