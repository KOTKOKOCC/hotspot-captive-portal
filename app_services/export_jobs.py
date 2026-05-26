import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from db import db


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_export_jobs_table() -> None:
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS export_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            table_name TEXT NOT NULL,
            fmt TEXT NOT NULL,
            date_from TEXT,
            date_to TEXT,
            status TEXT NOT NULL,
            file_path TEXT,
            file_name TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_export_jobs_created_at ON export_jobs(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_export_jobs_status ON export_jobs(status)")
    conn.commit()
    conn.close()


def create_export_job(
    username: str,
    table_name: str,
    fmt: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    init_export_jobs_table()
    conn = db()
    cur = conn.execute("""
        INSERT INTO export_jobs (
            username, table_name, fmt, date_from, date_to, status, created_at
        )
        VALUES (?, ?, ?, ?, ?, 'queued', ?)
    """, (
        username,
        table_name,
        fmt,
        date_from or "",
        date_to or "",
        now_iso(),
    ))
    conn.commit()
    job_id = int(cur.lastrowid)
    conn.close()
    return job_id


def get_export_job(job_id: int) -> dict[str, Any] | None:
    init_export_jobs_table()
    conn = db()
    row = conn.execute("SELECT * FROM export_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_export_jobs(limit: int = 20) -> list[dict[str, Any]]:
    init_export_jobs_table()
    conn = db()
    rows = conn.execute("""
        SELECT *
        FROM export_jobs
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def mark_export_job_running(job_id: int) -> None:
    init_export_jobs_table()
    conn = db()
    conn.execute("""
        UPDATE export_jobs
        SET status = 'running',
            started_at = ?,
            error = ''
        WHERE id = ?
    """, (now_iso(), job_id))
    conn.commit()
    conn.close()


def mark_export_job_done(job_id: int, file_path: str | Path, file_name: str) -> None:
    init_export_jobs_table()
    conn = db()
    conn.execute("""
        UPDATE export_jobs
        SET status = 'done',
            file_path = ?,
            file_name = ?,
            finished_at = ?,
            error = ''
        WHERE id = ?
    """, (str(file_path), file_name, now_iso(), job_id))
    conn.commit()
    conn.close()


def mark_export_job_failed(job_id: int, error: str) -> None:
    init_export_jobs_table()
    conn = db()
    conn.execute("""
        UPDATE export_jobs
        SET status = 'failed',
            error = ?,
            finished_at = ?
        WHERE id = ?
    """, (error[:2000], now_iso(), job_id))
    conn.commit()
    conn.close()


def _safe_existing_path(path: str, export_root: Path) -> Path | None:
    if not path:
        return None

    real_path = Path(path).resolve()
    real_root = export_root.resolve()
    try:
        if os.path.commonpath([str(real_root), str(real_path)]) != str(real_root):
            return None
    except ValueError:
        return None

    return real_path


def cleanup_export_job_files(
    export_dir: str | Path,
    retention_days: int,
    batch_size: int = 200,
    now_dt: datetime | None = None,
) -> dict[str, int]:
    init_export_jobs_table()

    export_root = Path(export_dir).resolve()
    current = now_dt or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=max(1, int(retention_days)))
    cutoff_iso = cutoff.isoformat()
    cutoff_ts = cutoff.timestamp()

    stats = {
        "expired_jobs": 0,
        "files_deleted": 0,
        "missing_files": 0,
        "stray_files_deleted": 0,
    }

    conn = db()
    try:
        rows = conn.execute(
            """
            SELECT id, file_path
            FROM export_jobs
            WHERE status = 'done'
              AND COALESCE(finished_at, created_at) < ?
            ORDER BY id
            LIMIT ?
            """,
            (cutoff_iso, max(1, int(batch_size))),
        ).fetchall()

        for row in rows:
            safe_path = _safe_existing_path(str(row["file_path"] or ""), export_root)
            if safe_path and safe_path.exists():
                try:
                    safe_path.unlink()
                    stats["files_deleted"] += 1
                except OSError:
                    pass
            elif row["file_path"]:
                stats["missing_files"] += 1

            conn.execute(
                """
                UPDATE export_jobs
                SET status = 'expired',
                    file_path = '',
                    error = 'export file removed by cleanup'
                WHERE id = ?
                """,
                (int(row["id"]),),
            )
            stats["expired_jobs"] += 1

        conn.commit()
    finally:
        conn.close()

    if export_root.exists():
        for pattern in ("*.tmp", "export_job_*.log"):
            for path in export_root.glob(pattern):
                safe_path = _safe_existing_path(str(path), export_root)
                if not safe_path or not safe_path.is_file():
                    continue
                try:
                    if safe_path.stat().st_mtime >= cutoff_ts:
                        continue
                    safe_path.unlink()
                    stats["stray_files_deleted"] += 1
                except OSError:
                    continue

    return stats
