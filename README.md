# Hotspot Captive Portal

Native guest Wi-Fi captive portal for hotels and resorts.

The project combines a FastAPI admin panel, MikroTik Hotspot/RADIUS integration,
phone call verification, room/surname PMS checks, vouchers, audit, exports, and
basic service monitoring in one installable server application.

Current version: `0.9.1`

## What It Does

- Authorizes guests by phone number and confirmation call.
- Authorizes hotel guests by room number and surname.
- Reuses verified devices across configured hotel networks.
- Routes PMS checks by guest network, VLAN, subnet, or object mapping.
- Supports 1C/PMS HTTP verification.
- Supports Opera/FIAS-based room lookup through a local stay cache.
- Receives RADIUS authorization and accounting from FreeRADIUS.
- Tracks guests, sessions, pending confirmations, call events, vouchers, and audit events.
- Provides an admin UI for networks, users, PMS objects, logs, service status, and exports.
- Installs systemd services and writes a local setup summary with generated credentials.

## Status

`0.9.x` is a working production line. It is already suitable for controlled
deployments, but the codebase is still being cleaned up. The main application
file is intentionally going to be split into smaller modules in upcoming
versions.

## Requirements

- Ubuntu 22.04+ recommended.
- Python 3.10+.
- `python3-venv`.
- Root access for systemd and FreeRADIUS setup.
- MikroTik Hotspot configured to use RADIUS.
- Optional: PBX/Asterisk callback integration.
- Optional: 1C/PMS or Opera/FIAS integration.

## Quick Install

On a clean server:

```bash
sudo -i
apt update
apt install -y git python3 python3-venv
git clone https://github.com/KOTKOKOCC/hotspot-captive-portal.git /opt/hotspot-captive-portal
cd /opt/hotspot-captive-portal
./setup.sh --yes
```

After installation, open the admin panel URL shown by the installer.

The installer also writes:

```text
/opt/hotspot-captive-portal/setup-summary.txt
```

This file contains the generated admin password and FreeRADIUS shared secret.
Keep it private.

## Upgrade

For an existing Git-based install:

```bash
cd /opt/hotspot-captive-portal
git pull --ff-only
sudo ./setup.sh --upgrade
```

By default, `--upgrade` does not reconfigure FreeRADIUS. This protects existing
RADIUS configuration on production machines.

Useful upgrade flags:

```bash
sudo ./setup.sh --upgrade --with-radius
sudo ./setup.sh --upgrade --skip-radius
sudo ./setup.sh --upgrade --skip-smoke
```

## First Login

Open:

```text
http://SERVER_IP:8080/admin/login
```

Then go to `Система` and configure:

- `Сети`: hotel/object network mappings.
- `Пользователи`: admin, IT, and reception accounts.
- `Настройки`: MikroTik, PBX, PMS API, 1C/PMS objects, Opera/FIAS objects.
- `Сервис`: readiness checks, service status, CPU/RAM/Disk, logs, restart button.

Most operational settings are stored in SQLite and managed through the UI.
The `.env` file is kept as an installation/runtime file for secrets and paths.

## Guest Authorization Flow

1. A guest connects to MikroTik Hotspot.
2. MikroTik asks FreeRADIUS.
3. FreeRADIUS calls the portal `/radius-check` endpoint.
4. The portal detects the object by IP/subnet/VLAN/network mapping.
5. Known active sessions and known MACs are accepted immediately.
6. Phone users are placed into pending state until the PBX call confirms them.
7. Room/surname users are checked against the configured PMS source.
8. Accepted users receive RADIUS attributes and get internet access.
9. FreeRADIUS forwards accounting events to `/radius-accounting`.

## Integrations

### MikroTik

MikroTik should use FreeRADIUS as the Hotspot RADIUS server. The shared secret
is generated during install and saved in `setup-summary.txt`.

Configure the guest networks in the admin UI under `Система -> Сети`. The portal
uses those mappings to understand which hotel/object the guest belongs to.

### FreeRADIUS

On first install, `setup.sh` can install and configure FreeRADIUS with:

- UDP `1812` for authentication.
- UDP `1813` for accounting.
- REST bridge to `http://127.0.0.1:8080/radius-check`.
- Accounting forwarder to `http://127.0.0.1:8080/radius-accounting`.

During `--upgrade`, FreeRADIUS is skipped unless `--with-radius` is passed.

### PBX / Asterisk

PBX callback verification is configured in the UI. Keep the PBX endpoint
restricted by allowed IPs.

### 1C / PMS

1C/PMS objects are configured in the UI. The portal sends room/surname data to
the configured API and uses the response to accept or reject the guest.

### Opera / FIAS

Opera/FIAS objects are configured in the UI. The portal can use a local
`opera_stays.db` cache for room/surname lookup. The FIAS listener/collector is
deployment-specific and should be managed carefully on production systems.

## Services

The standard install manages:

```text
hotspot-captive-portal.service
hotspot-cleanup-worker.service
hotspot-mikrotik-sync-worker.service
freeradius.service
```

Some deployments also run:

```text
opera-fias-sync.service
```

Check status:

```bash
systemctl status hotspot-captive-portal.service --no-pager
systemctl status freeradius.service --no-pager
```

Watch logs:

```bash
journalctl -u hotspot-captive-portal.service -f
```

The admin UI also has `Система -> Сервис`, which shows readiness checks and live
service status.

## Production Safety

Do not run a fresh install directly over an existing production directory unless
you know exactly what will be replaced.

For non-Git legacy deployments, use a blue/green migration:

1. Keep the old portal running.
2. Clone the Git version into a new directory.
3. Copy `.env` and data into the new directory.
4. Run the new portal on a temporary port.
5. Verify admin UI, PMS settings, networks, and service readiness.
6. Stop authorization traffic or put MikroTik into a temporary bypass mode.
7. Stop the portal, copy the final SQLite database, switch systemd, and start.
8. Keep the old directory as rollback until the new version is proven.

SQLite databases can be large and busy on production systems. For an exact final
copy, stop the portal and workers before copying the database.

## Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8080
```

Run smoke checks:

```bash
python tools/smoke_check.py --strict-secrets
```

## Repository Layout

```text
app.py                  Main FastAPI app and admin routes
db.py                   SQLite schema and connection helpers
services.py             Auth/session/audit business logic
admin_auth.py           Admin users and sessions
app_services/           Settings, 1C/PMS, Opera/FIAS stores
integrations/           External lookup helpers
workers/                Cleanup and MikroTik sync workers
scripts/                FreeRADIUS accounting forwarder
tools/smoke_check.py    Basic safety checks
static/                 Admin UI assets
setup.sh                Installer and upgrader
```

## Roadmap

- Split `app.py` into smaller modules.
- Improve first-run UI and reduce terminal configuration.
- Move production data paths toward a dedicated data directory.
- Improve backup/restore tooling.
- Expand readiness checks and PMS diagnostics.
- Document MikroTik, FreeRADIUS, PBX, 1C, and Opera/FIAS setup in detail.

## License

License is not declared yet.
