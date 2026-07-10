# Running the SpoolSense middleware in Docker

Run the middleware on any machine on your printer's LAN — a home server, NAS,
or the box already running Spoolman. Useful when the printer host can't run it
directly (e.g. Snapmaker U1) or you just prefer containers.

The middleware is fully network-native: it talks to your MQTT broker, Moonraker,
and Spoolman over the LAN. Nothing needs to run on the printer host itself.

> **Run exactly ONE middleware instance per printer.** A second instance (for
> example one on the printer host *and* this container, pointed at the same
> broker) will fight over the retained MQTT status topics and double-process
> every scan.

## Prerequisites

- Docker with the compose plugin (`docker compose version` should work)
- Network reachability from this machine to your MQTT broker, Moonraker, and
  Spoolman

## Install

```bash
git clone https://github.com/SpoolSense/spoolsense_middleware.git
cd spoolsense_middleware/docker
docker compose up -d
```

The first start seeds `./spoolsense-data/` with the config examples and waits.
Create your config from the example matching your printer:

```bash
cp spoolsense-data/config.example.single.yaml spoolsense-data/config.yaml
# or config.example.afc.yaml / config.example.toolchanger.yaml / config.example.happy_hare.yaml
```

Edit `spoolsense-data/config.yaml` — at minimum:

- `mqtt.broker` — your broker's IP (and username/password if required)
- `moonraker_url` — e.g. `http://192.168.1.50:7125`
- `spoolman_url` — e.g. `http://192.168.1.32:7912` (optional, tag-only mode without it)
- `scanners` — your scanner device IDs and actions

The waiting container starts the middleware automatically within a few
seconds of `config.yaml` appearing — no restart needed:

```bash
docker compose logs -f     # watch it come up
```

A healthy start logs `Starting SpoolSense Middleware v…`, connects to MQTT,
and (if configured) refreshes the Spoolman cache.

## Validate a config without starting

```bash
docker compose run --rm spoolsense python middleware/spoolsense.py --check-config
```

## Web panel / mobile app

If `mobile.enabled: true` in your config, the REST API and web panel are served
on port 5001 (mapped in `compose.yaml`): `http://<docker-host>:5001`.

Saving config from the web panel restarts the container automatically so the
new settings take effect (the container self-terminates cleanly and Docker's
restart policy brings it back).

Not using the mobile app or panel? Remove the `ports:` mapping from
`compose.yaml`.

## Upgrade

```bash
cd spoolsense_middleware
git pull
cd docker
docker compose up -d --build
```

## Logs and state

Everything lives in `./spoolsense-data/` (mounted at `~/SpoolSense` inside the
container, same layout as a bare install):

| File | Purpose |
|---|---|
| `config.yaml` | your configuration |
| `middleware/spoolsense.log` | rotating log file (same content as `docker compose logs`) |
| `deductions.json` | pending mobile filament deductions |

## Uninstall

```bash
docker compose down
# your config/state stays in ./spoolsense-data — delete it if you're done
```
