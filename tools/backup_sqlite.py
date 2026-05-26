#!/usr/bin/env python3
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

    return values


def resolve_path(value: str, base_dir: Path = PROJECT_DIR) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return base_dir / path


def existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def human_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def ensure_free_space(db_path: Path, dest_dir: Path, multiplier: float, reserve_mb: int) -> None:
    db_size = db_path.stat().st_size
    reserve = int(reserve_mb) * 1024 * 1024
    required = int(db_size * multiplier) + reserve
    usage = shutil.disk_usage(existing_parent(dest_dir))

    if usage.free < required:
        raise SystemExit(
            "not enough free space for backup: "
            f"need {human_bytes(required)}, free {human_bytes(usage.free)}"
        )


def write_summary(backup_dir: Path, db_path: Path, backup_db: Path, quick_check: str) -> None:
    summary = backup_dir / "backup-summary.txt"
    lines = [
        "Hotspot Captive Portal database backup",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Source database: {db_path}",
        f"Backup database: {backup_db}",
        f"Backup size: {human_bytes(backup_db.stat().st_size)}",
        f"SQLite quick_check: {quick_check}",
        "",
        "Keep this directory private. It may contain personal data and secrets.",
    ]
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary.chmod(0o600)


def rotate_backups(dest_dir: Path, prefix: str, keep: int) -> None:
    if keep <= 0 or not dest_dir.exists():
        return

    backups = sorted(
        path
        for path in dest_dir.iterdir()
        if path.is_dir() and path.name.startswith(f"{prefix}-")
    )
    stale = backups[:-keep]

    for path in stale:
        shutil.rmtree(path)


def run_backup(args: argparse.Namespace) -> Path:
    env_values = read_env_file(PROJECT_DIR / ".env")
    db_raw = args.db or os.getenv("DB_PATH") or env_values.get("DB_PATH") or "hotspot.db"
    db_path = resolve_path(db_raw)

    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")

    dest_dir = resolve_path(args.dest_dir)
    ensure_free_space(db_path, dest_dir, args.min_free_multiplier, args.reserve_mb)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = dest_dir / f"{args.prefix}-{timestamp}"
    backup_db = backup_dir / db_path.name

    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_dir.chmod(0o700)

    source = sqlite3.connect(str(db_path), timeout=args.timeout)
    target = sqlite3.connect(str(backup_db))
    last_percent = -1

    def progress(status: int, remaining: int, total: int) -> None:
        nonlocal last_percent
        if total <= 0:
            return

        done = total - remaining
        percent = int(done * 100 / total)
        if percent >= last_percent + 10 or percent == 100:
            last_percent = percent
            print(f"Backup progress: {percent}% ({done}/{total} pages)")

    try:
        source.backup(target, pages=args.pages, progress=progress, sleep=args.sleep)
        quick_check = target.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise SystemExit(f"backup quick_check failed: {quick_check}")
    finally:
        target.close()
        source.close()

    backup_db.chmod(0o600)

    if args.include_env:
        env_path = PROJECT_DIR / ".env"
        if env_path.exists():
            env_copy = backup_dir / "env.snapshot"
            shutil.copy2(env_path, env_copy)
            env_copy.chmod(0o600)

    write_summary(backup_dir, db_path, backup_db, quick_check)
    rotate_backups(dest_dir, args.prefix, args.keep)
    return backup_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a verified SQLite backup for the hotspot portal.")
    parser.add_argument("--db", help="SQLite database path. Defaults to DB_PATH from .env.")
    parser.add_argument("--dest-dir", default="backups/db", help="Directory where timestamped backups are stored.")
    parser.add_argument("--prefix", default="hotspot-db", help="Backup directory prefix.")
    parser.add_argument("--keep", type=int, default=1, help="How many timestamped backups to keep, capped at 2.")
    parser.add_argument("--pages", type=int, default=1024, help="SQLite pages copied per backup step.")
    parser.add_argument("--sleep", type=float, default=0.05, help="Sleep between SQLite backup steps.")
    parser.add_argument("--timeout", type=float, default=30.0, help="SQLite connection timeout in seconds.")
    parser.add_argument("--min-free-multiplier", type=float, default=1.2, help="Required free space multiplier.")
    parser.add_argument("--reserve-mb", type=int, default=1024, help="Extra free space reserve in MB.")
    parser.add_argument("--include-env", action="store_true", help="Copy .env into backup directory as env.snapshot.")
    args = parser.parse_args()
    args.keep = min(max(int(args.keep), 1), 2)

    backup_dir = run_backup(args)
    print(f"Backup directory: {backup_dir}")
    print("Backup status: ok")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Backup interrupted", file=sys.stderr)
        raise SystemExit(130)
