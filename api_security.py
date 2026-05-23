import hmac
import ipaddress

from fastapi import HTTPException, Request


def ip_allowed(client_ip: str, allowed_ips: list[str] | tuple[str, ...]) -> bool:
    client_ip = (client_ip or "").strip()
    if not allowed_ips:
        return True

    try:
        client_addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False

    for raw_entry in allowed_ips:
        entry = (raw_entry or "").strip()
        if not entry:
            continue

        try:
            if "/" in entry:
                if client_addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif client_addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue

    return False


def optional_api_guard(
    request: Request,
    token: str = "",
    allowed_ips: list[str] | tuple[str, ...] = (),
) -> None:
    token = (token or "").strip()
    allowed_ips = tuple(ip for ip in allowed_ips if ip)

    if not token and not allowed_ips:
        return

    client_ip = request.client.host if request.client else ""
    if allowed_ips and not ip_allowed(client_ip, allowed_ips):
        raise HTTPException(status_code=403, detail="forbidden_ip")

    if not token:
        return

    supplied = (request.headers.get("X-Internal-Token") or "").strip()
    auth = (request.headers.get("Authorization") or "").strip()
    if not supplied and auth.lower().startswith("bearer "):
        supplied = auth.split(" ", 1)[1].strip()

    if not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=403, detail="forbidden_token")
