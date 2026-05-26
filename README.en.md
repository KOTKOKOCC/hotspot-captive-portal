# Hotspot Captive Portal

[Русский](README.md) | [English](README.en.md)

Native guest Wi-Fi captive portal for hotels and resorts.

The project combines a FastAPI admin panel, MikroTik Hotspot/RADIUS integration,
phone call verification, room/surname PMS checks, vouchers, audit, exports, and
basic service monitoring in one installable server application.

Current version: `0.9.2`

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

The `0.9.x` line is a working production line. It is already suitable for
controlled deployments, but the codebase is still being cleaned up. The main
application file will gradually be split into smaller modules without changing
behavior.

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

This file contains the generated admin credentials and FreeRADIUS shared secret.
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
- `Настройки`: MikroTik, PBX, PMS API, 1C/PMS objects, and Opera/FIAS objects.
- `Сервис`: service state, readiness, CPU/RAM/Disk, logs, and restart.

Most operational settings are stored in SQLite and managed through the UI.
The `.env` file remains an installation/runtime file for secrets, paths, and
base runtime parameters.

## Guest Authorization Flow

1. A guest connects to MikroTik Hotspot.
2. MikroTik sends a request to FreeRADIUS.
3. FreeRADIUS calls the portal `/radius-check` endpoint.
4. The portal detects the object by IP, subnet, VLAN, and network settings.
5. Known active sessions and MAC addresses are accepted immediately.
6. Phone users enter pending state until the PBX call confirms them.
7. Room/surname users are checked against the configured PMS source.
8. On success, the portal returns a RADIUS access response.
9. FreeRADIUS sends accounting events to `/radius-accounting`.

## Integrations

### MikroTik

MikroTik should use FreeRADIUS as the RADIUS server for Hotspot. The shared
secret is generated during installation and saved in `setup-summary.txt`.

Guest networks are configured in the admin UI under `Система -> Сети`. The
portal uses those settings to determine which object the guest belongs to.

### FreeRADIUS

On first install, `setup.sh` can install and configure FreeRADIUS with:

- UDP `1812` for authorization.
- UDP `1813` for accounting.
- REST bridge to `http://127.0.0.1:8080/radius-check`.
- Accounting forwarder to `http://127.0.0.1:8080/radius-accounting`.

During `--upgrade`, FreeRADIUS is skipped unless `--with-radius` is passed.

### PBX / Asterisk

Call confirmation is configured in the UI. Restrict the PBX endpoint by allowed
IP addresses.

### 1C / PMS

1C/PMS objects are configured in the UI. The portal sends room and surname to
the configured API and decides by the PMS response.

### Opera / FIAS

Opera/FIAS objects are configured in the UI. For room/surname checks, the portal
can use a local `opera_stays.db` database. The FIAS listener/collector depends on
the specific deployment and should be managed carefully in production.

## Services

The standard install manages:

```text
hotspot-captive-portal.service
hotspot-cleanup-worker.service
hotspot-mikrotik-sync-worker.service
freeradius.service
```

Some deployments also use:

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

The admin UI also has `Система -> Сервис`, which shows readiness checks, service
state, load, and logs.

## Production Safety

Do not run a clean install over an existing production directory unless you know
exactly which files will be replaced.

For legacy installs without proper Git linkage, use a blue/green migration:

1. Keep the old portal running.
2. Clone the Git version into a new directory.
3. Move `.env` and data into the new directory.
4. Start the new copy on a temporary port.
5. Verify admin UI, networks, PMS settings, and service readiness.
6. During final switch, disable authorization or enable temporary MikroTik bypass.
7. Stop the portal, copy the final SQLite database, switch systemd, and start.
8. Keep the old directory as rollback until the new version is proven.

SQLite databases can be large and constantly written to in production. For an
exact final copy, stop the portal and workers before copying the database.

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

## Project Layout

```text
app.py                  Main FastAPI application and admin routes
db.py                   SQLite schema and connection helpers
services.py             Authorization, sessions, and audit business logic
admin_auth.py           Admin users and admin sessions
app_services/           Settings, 1C/PMS, Opera/FIAS stores
integrations/           External integration helpers
workers/                Cleanup and MikroTik sync workers
scripts/                FreeRADIUS accounting forwarder
tools/smoke_check.py    Basic safety and route checks
static/                 Admin UI static files
setup.sh                Installer and upgrader
```

## Roadmap

- Split `app.py` into smaller modules.
- Improve first-run UX and reduce terminal work.
- Move production data into a dedicated data directory.
- Add clear backup/restore tooling.
- Expand readiness checks and PMS diagnostics.
- Document MikroTik, FreeRADIUS, PBX, 1C, and Opera/FIAS setup in detail.

## License

This project is licensed under the [MIT License](LICENSE).
