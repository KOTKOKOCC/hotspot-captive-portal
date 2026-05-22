import sqlite3
import re
import unicodedata
from datetime import date

DB_PATH = "/opt/hotspot-captive-portal/opera/opera_stays.db"

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

    conn = sqlite3.connect(DB_PATH)
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
    conn.close()

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

    conn = sqlite3.connect(DB_PATH)
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
    conn.close()
    return rows
