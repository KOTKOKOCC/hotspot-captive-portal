from integrations.opera.lookup import room_auth_allowed as opera_room_auth_allowed
from integrations.onec.one_c_hotel import check_wifi_guest, OneCHotelError
from app_services.onec_store import get_onec_site_by_code, get_onec_site_by_name_or_code
from app_services.opera_store import get_opera_site_by_name_or_code

import logging

logger = logging.getLogger(__name__)


def pms_room_auth_allowed(room_num: str, surname: str, hotel: str | None = None, vlan_id: str | None = None) -> dict:
    hotel = (hotel or "").strip()

    if not hotel:
        return {
            "ok": False,
            "source": None,
            "hotel": hotel,
            "error": "hotel_not_resolved",
        }

    opera_site_cfg = get_opera_site_by_name_or_code(hotel)

    if opera_site_cfg and opera_site_cfg.get("enabled"):

        logger.info("opera using DB config for %s", opera_site_cfg.get("code"))
        
        if opera_room_auth_allowed(
            room_num,
            surname,
            property_code=opera_site_cfg.get("property_code"),
        ):
            return {
                "ok": True,
                "source": "opera",
                "hotel": hotel,
                "error": "",
            }

        return {
            "ok": False,
            "source": "opera",
            "hotel": hotel,
            "error": "guest_not_found",
        }

    site_cfg = get_onec_site_by_name_or_code(hotel)

    if site_cfg:
        try:
            site_code = site_cfg.get("code")

            if not site_cfg or not site_cfg.get("enabled"):
                return {
                    "ok": False,
                    "source": "1c",
                    "hotel": hotel,
                    "error": "1c_site_not_configured_or_disabled",
                }

            token = site_cfg.get("token")
            base_url = site_cfg.get("base_url")
            timeout = site_cfg.get("timeout")

            if not token or not base_url:
                return {
                    "ok": False,
                    "source": "1c",
                    "hotel": hotel,
                    "error": "1c_site_missing_token_or_url",
                }

            logger.info("1c using DB config for %s", site_code)
            
            result = check_wifi_guest(
                room_num,
                surname,
                token=token,
                base_url=base_url,
                timeout=timeout,
            )

        except OneCHotelError as e:
            return {
                "ok": False,
                "source": "1c",
                "hotel": hotel,
                "error": str(e),
            }

        if result.get("ok"):
            return {
                "ok": True,
                "source": "1c",
                "hotel": hotel,
                "error": "",
                "reservation_number": result.get("reservation_number", ""),
                "checkin_date": result.get("checkin_date", ""),
                "checkout_date": result.get("checkout_date", ""),
            }

        return {
            "ok": False,
            "source": "1c",
            "hotel": hotel,
            "error": result.get("error") or "guest_not_found",
        }

    return {
        "ok": False,
        "source": None,
        "hotel": hotel,
        "error": "pms_not_configured_for_hotel",
    }
