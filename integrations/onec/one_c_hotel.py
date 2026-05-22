import requests



class OneCHotelError(Exception):
    pass


def check_wifi_guest(
    room: str,
    last_name: str,
    token: str | None = None,
    base_url: str | None = None,
    timeout: int | None = None,
) -> dict:
    use_token = token
    use_base_url = base_url
    use_timeout = timeout or 5
    room = (room or "").strip()
    last_name = (last_name or "").strip()

    if not room or not last_name:
        return {
            "ok": False,
            "error": "Не указан номер комнаты или фамилия",
            "raw": None,
        }

    use_token = token

    if not use_base_url or not use_token:
        raise OneCHotelError("1C site config incomplete")

    params = {
        "token": use_token,
        "room": room,
        "LastName": last_name,
    }

    try:
        response = requests.get(
            use_base_url,
            params=params,
            timeout=use_timeout,
        )
        response.raise_for_status()
    except Exception as e:
        raise OneCHotelError(f"Ошибка запроса к 1С: {e}")

    try:
        data = response.json()
    except Exception:
        raise OneCHotelError(f"1С вернула не JSON: {response.text[:300]}")

    return {
        "ok": data.get("Success") is True,
        "error": data.get("Error") or "",
        "reservation_number": data.get("ReservationNumber") or "",
        "checkin_date": data.get("CheckInDate") or "",
        "checkout_date": data.get("CheckOutDate") or "",
        "url": data.get("URL") or "",
        "raw": data,
    }
