#!/bin/sh
# Stage 1 (root): Docker creates missing bind-mount sources owned by root,
# which would break the non-root middleware (it writes config/log/deductions
# into the volume). Fix ownership, then drop privileges and re-exec.
# Stage 2 (spoolsense): first-run config guard, then hand off to the CMD.
set -e

CONFIG_DIR="$HOME/SpoolSense"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$CONFIG_DIR"
    chown -R spoolsense:spoolsense "$CONFIG_DIR"
    exec setpriv --reuid=spoolsense --regid=spoolsense --clear-groups "$0" "$@"
fi

mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    cp -n /app/middleware/config.example.*.yaml "$CONFIG_DIR/" 2>/dev/null || true
    echo "============================================================"
    echo " No config.yaml found in the SpoolSense volume."
    echo ""
    echo " Config examples have been copied into the volume."
    echo " On the host, copy the one matching your printer to"
    echo " config.yaml and edit it (MQTT broker, Moonraker URL,"
    echo " scanners):"
    echo ""
    echo "   cp spoolsense-data/config.example.single.yaml \\"
    echo "      spoolsense-data/config.yaml"
    echo ""
    echo " The middleware starts automatically once config.yaml"
    echo " appears — no restart needed. Waiting..."
    echo "============================================================"
    # Waiting (not exiting) avoids a restart/backoff loop under
    # restart: unless-stopped. Trap so `docker stop` works promptly
    # while we're PID 1 in the wait loop.
    trap 'exit 0' TERM INT
    while [ ! -f "$CONFIG_DIR/config.yaml" ]; do
        sleep 5
    done
    echo "config.yaml detected — starting middleware"
fi

exec "$@"
