import sqlite3
from pathlib import Path

from config import DB_PATH


def db():
    db_path = Path(DB_PATH)
    if db_path.parent and str(db_path.parent) != ".":
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(db_path),
        timeout=10,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


def fetch_all(query: str, params: tuple = ()):
    conn = db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def fetch_one(query: str, params: tuple = ()):
    conn = db()
    row = conn.execute(query, params).fetchone()
    conn.close()
    return row


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS guests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT NOT NULL UNIQUE,
        first_verified_at TEXT,
        first_hotel TEXT,
        auth_method TEXT NOT NULL DEFAULT 'call',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_auth_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending_auth (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT NOT NULL,
        mac TEXT,
        ip TEXT,
        nas_id TEXT,
        hotel TEXT,
        ssid TEXT,
        vlan_id TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        status TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS guest_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guest_id INTEGER,
        phone TEXT NOT NULL,
        mac TEXT,
        ip TEXT,
        nas_id TEXT,
        hotel TEXT,
        ssid TEXT,
        vlan_id TEXT,
        acct_session_id TEXT,
        started_at TEXT NOT NULL,
        last_seen_at TEXT,
        ended_at TEXT,
        expires_at TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        terminate_cause TEXT,
        acct_session_time INTEGER,
        device_name TEXT,
        FOREIGN KEY (guest_id) REFERENCES guests(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS call_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT,
        callerid_raw TEXT,
        source_ip TEXT,
        created_at TEXT NOT NULL,
        result TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT,
        mac TEXT,
        ip TEXT,
        nas_id TEXT,
        hotel TEXT,
        ssid TEXT,
        vlan_id TEXT,
        event_type TEXT NOT NULL,
        event_time TEXT NOT NULL,
        details TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS radius_accounting (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        acct_session_id TEXT,
        username TEXT,
        mac TEXT,
        ip TEXT,
        nas_ip TEXT,
        nas_id TEXT,
        nas_port_id TEXT,
        called_station_id TEXT,
        acct_status_type TEXT,
        terminate_cause TEXT,
        session_time INTEGER,
        event_time TEXT NOT NULL,
        raw_json TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS network_map (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hotel_name TEXT,
        ssid_name TEXT,
        vlan_id TEXT,
        subnet_cidr TEXT,
        mikrotik_interface TEXT,
        hotspot_server TEXT,
        is_active INTEGER NOT NULL DEFAULT 1
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_guests_phone ON guests(phone)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_auth_phone ON pending_auth(phone)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_auth_status ON pending_auth(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_auth_mac ON pending_auth(mac)")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_guest_sessions_phone ON guest_sessions(phone)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_guest_sessions_mac ON guest_sessions(mac)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_guest_sessions_status ON guest_sessions(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_guest_sessions_started_at ON guest_sessions(started_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_guest_sessions_acct_session_id ON guest_sessions(acct_session_id)")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_call_events_phone ON call_events(phone)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_call_events_created_at ON call_events(created_at)")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_phone ON audit_log(phone)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_event_time ON audit_log(event_time)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_mac ON audit_log(mac)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_ip ON audit_log(ip)")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_radius_accounting_session_id ON radius_accounting(acct_session_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_radius_accounting_username ON radius_accounting(username)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_radius_accounting_mac ON radius_accounting(mac)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_radius_accounting_event_time ON radius_accounting(event_time)")

    conn.commit()
    conn.close()


def ensure_guests_last_auth_at():
    conn = db()
    cols = conn.execute("PRAGMA table_info(guests)").fetchall()
    col_names = {row["name"] for row in cols}

    if "last_auth_at" not in col_names:
        conn.execute("ALTER TABLE guests ADD COLUMN last_auth_at TEXT")
        conn.execute("""
            UPDATE guests
            SET last_auth_at = COALESCE(updated_at, first_verified_at, created_at)
            WHERE last_auth_at IS NULL
        """)
        conn.commit()

    conn.close()


def ensure_guest_sessions_device_name():
    conn = db()
    cols = conn.execute("PRAGMA table_info(guest_sessions)").fetchall()
    col_names = {row["name"] for row in cols}

    if "device_name" not in col_names:
        conn.execute("ALTER TABLE guest_sessions ADD COLUMN device_name TEXT")
        conn.commit()

    conn.close()