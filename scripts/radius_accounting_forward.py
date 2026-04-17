#!/usr/bin/env python3
import json
import os
import sys
import urllib.request
from pathlib import Path


ENV_FILE = Path(__file__).with_name("radius_accounting_forward.env")

if ENV_FILE.exists():
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


FIELDS = [
    "acct_status_type",
    "acct_session_id",
    "username",
    "mac",
    "ip",
    "nas_ip",
    "nas_id",
    "nas_port_id",
    "called_station_id",
    "terminate_cause",
    "session_time",
    "event_time",
]

BACKEND_URL = os.getenv(
    "HOTSPOT_BACKEND_URL",
    "http://127.0.0.1:8080/radius-accounting",
)


def main() -> int:
    values = sys.argv[1:]
    while len(values) < len(FIELDS):
        values.append("")

    payload = dict(zip(FIELDS, values))

    try:
        payload["session_time"] = int(payload["session_time"]) if payload["session_time"] else None
    except Exception:
        payload["session_time"] = None

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        BACKEND_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=3) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
        print(body)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())