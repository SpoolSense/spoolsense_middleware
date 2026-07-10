# SpoolSense middleware — LAN deployment for printers whose host can't run
# it directly (#102). The middleware is fully network-native (MQTT, Moonraker
# HTTP/websocket, Spoolman HTTP), so it runs anywhere on the printer's LAN.
#
#   docker compose up -d          (see compose.yaml)
#
# Config, log, and deduction state live in one volume mounted at
# /home/spoolsense/SpoolSense — the same ~/SpoolSense layout as a bare install.

FROM python:3.12-slim

# Non-root user; HOME drives the middleware's ~/SpoolSense paths
RUN useradd --create-home --uid 1000 spoolsense

WORKDIR /app
COPY middleware/requirements.txt middleware/requirements.txt
RUN pip install --no-cache-dir -r middleware/requirements.txt

COPY middleware/ middleware/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && chown -R spoolsense:spoolsense /app

# The entrypoint starts as root ONLY to fix bind-mount ownership (Docker
# creates missing bind sources root-owned), then drops to the spoolsense
# user via setpriv before running anything.
ENV HOME=/home/spoolsense
# Signals rest_api's save-and-restart to self-terminate cleanly (the restart
# policy relaunches with the new config) instead of calling systemctl.
ENV SPOOLSENSE_IN_DOCKER=1

# REST API / web panel (only bound if mobile.enabled in config.yaml)
EXPOSE 5001

VOLUME ["/home/spoolsense/SpoolSense"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "middleware/spoolsense.py"]
