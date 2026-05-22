#!/usr/bin/env python3
import json
import sys
import urllib.request

import logging

logger = logging.getLogger(__name__)

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
    "http://127.0.0.1:8080/radius-accounting",
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST",
)

with urllib.request.urlopen(req, timeout=3) as resp:
    body = resp.read().decode("utf-8", errors="ignore")
    logger.debug("radius accounting forward payload received, size=%s bytes", len(body or ""))
