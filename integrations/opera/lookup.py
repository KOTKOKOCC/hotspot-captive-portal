import sqlite3
import re
import unicodedata
import logging
from datetime import date

from config import OPERA_CACHE_DB_PATH

DB_PATH = OPERA_CACHE_DB_PATH
logger = logging.getLogger(__name__)

def normalize_user_surname(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().strip()
    value = re.sub(r"[^a-zа-я0-9\s\-]", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip()
    parts = value.split()
    return parts[-1] if parts else ""

def yymmdd_to_date(value: str):
    value = (value or "").strip()
    if len(value) != 6 or not value.isdigit():
        return None
    yy = int(value[:2])
    mm = int(value[2:4])
    dd = int(value[4:6])
    year = 2000 + yy
    try:
        return date(year, mm, dd)
    except ValueError:
        return None

def room_auth_allowed(room_num: str, surname: str) -> bool:
    room_num = (room_num or "").strip()
    surname_norm = normalize_user_surname(surname)

    if not room_num or not surname_norm:
        return False

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            cur.execute("""
                SELECT departure_date
                FROM opera_stays
                WHERE status='active'
                  AND room_num=?
                  AND guest_surname_norm=?
            """, (room_num, surname_norm))

            rows = cur.fetchall()
    except sqlite3.Error as exc:
        logger.warning("opera room auth lookup failed: %s", exc)
        return False

    if not rows:
        return False

    today = date.today()
    for row in rows:
        departure = yymmdd_to_date(row["departure_date"])
        if departure is None:
            continue
        if departure >= today:
            return True

    return False

def debug_find_guests_by_room_and_surname(room_num: str, surname: str):
    room_num = (room_num or "").strip()
    surname_norm = normalize_user_surname(surname)

    if not room_num or not surname_norm:
        return []

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            cur.execute("""
                SELECT id, guest_num, room_num, guest_name_raw, guest_first_name,
                       arrival_date, departure_date, share_flag, status
                FROM opera_stays
                WHERE status='active'
                  AND room_num=?
                  AND guest_surname_norm=?
                ORDER BY id
            """, (room_num, surname_norm))

            rows = [dict(r) for r in cur.fetchall()]
            return rows
    except sqlite3.Error as exc:
        logger.warning("opera debug lookup failed: %s", exc)
        return []
