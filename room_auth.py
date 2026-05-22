from datetime import datetime, timedelta

from db import db


def ensure_room_auth_table():
    conn = db()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS room_auth (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_num TEXT NOT NULL,
        surname TEXT NOT NULL,
        surname_norm TEXT NOT NULL,
        guest_num TEXT,
        mac TEXT,
        ip TEXT,
        nas_id TEXT,
        hotel TEXT,
        status TEXT NOT NULL DEFAULT 'verified',
        expires_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_room_auth_mac ON room_auth(mac)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_room_auth_ip ON room_auth(ip)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_room_auth_room_surname ON room_auth(room_num, surname_norm)")
    conn.commit()
    conn.close()

def save_verified_room_auth(room_num, surname, surname_norm, mac, ip, nas_id="", hotel="Dusit", ttl_minutes=15):
    expires_at = (datetime.utcnow() + timedelta(minutes=ttl_minutes)).isoformat()
    conn = db()
    conn.execute("""
        INSERT INTO room_auth (room_num, surname, surname_norm, mac, ip, nas_id, hotel, status, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'verified', ?)
    """, (room_num, surname, surname_norm, mac, ip, nas_id, hotel, expires_at))
    conn.commit()
    conn.close()

def get_verified_room_auth(mac, ip):
    now = datetime.utcnow().isoformat()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM room_auth
        WHERE status='verified'
          AND expires_at > ?
          AND (mac=? OR ip=?)
        ORDER BY id DESC
        LIMIT 1
    """, (now, mac, ip))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None
