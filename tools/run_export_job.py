#!/usr/bin/env python3
import os
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


from app_services.export_jobs import (  # noqa: E402
    get_export_job,
    init_export_jobs_table,
    mark_export_job_done,
    mark_export_job_failed,
    mark_export_job_running,
)
from exports import export_formats, export_table_names, write_export_zip_file, write_single_xlsx_file  # noqa: E402
from services import audit  # noqa: E402


def fail(job_id: int, message: str) -> None:
    mark_export_job_failed(job_id, message)
    audit("export_failed", details=f"job_id={job_id} error={message[:200]}")
    raise SystemExit(1)


def safe_name_part(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip())
    return value.strip("._") or "all"


def build_filename(job: dict) -> str:
    job_id = int(job["id"])
    table_name = safe_name_part(str(job["table_name"] or "all"))
    fmt = safe_name_part(str(job["fmt"] or "zip")).lower()
    date_from = safe_name_part(str(job["date_from"] or "start"))
    date_to = safe_name_part(str(job["date_to"] or "end"))

    if fmt == "zip" and table_name == "all":
        return f"miracleon_export_{date_from}_{date_to}_job{job_id}.zip"

    return f"{table_name}_{date_from}_{date_to}_job{job_id}.{fmt}"


def main() -> None:
    if len(sys.argv) != 2 or not str(sys.argv[1]).isdigit():
        raise SystemExit("usage: run_export_job.py <job_id>")

    job_id = int(sys.argv[1])
    init_export_jobs_table()
    job = get_export_job(job_id)
    if not job:
        raise SystemExit(f"export job {job_id} not found")

    table_name = str(job["table_name"] or "all").strip().lower()
    fmt = str(job["fmt"] or "zip").strip().lower()
    allowed_tables = {"all", *export_table_names()}

    if table_name not in allowed_tables:
        fail(job_id, "unknown export table")
    if fmt not in export_formats():
        fail(job_id, "unknown export format")
    if fmt == "xlsx" and table_name == "all":
        fail(job_id, "xlsx export supports one table only")

    output_dir = Path(os.getenv("EXPORT_JOBS_DIR", str(PROJECT_ROOT / "export_jobs")))
    output_dir.mkdir(parents=True, exist_ok=True)
    file_name = build_filename(job)
    output_path = output_dir / file_name
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    mark_export_job_running(job_id)
    try:
        if fmt == "zip":
            write_export_zip_file(
                temp_path,
                date_from=job["date_from"] or None,
                date_to=job["date_to"] or None,
                table_name=table_name,
            )
        else:
            write_single_xlsx_file(
                temp_path,
                table_name=table_name,
                date_from=job["date_from"] or None,
                date_to=job["date_to"] or None,
            )

        os.replace(temp_path, output_path)
        mark_export_job_done(job_id, output_path, file_name)
        audit(
            "export_completed",
            details=(
                f"user={job['username'] or '-'} job_id={job_id} table={table_name} format={fmt} "
                f"date_from={job['date_from'] or '-'} date_to={job['date_to'] or '-'}"
            ),
        )
    except Exception as exc:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        fail(job_id, str(exc))


if __name__ == "__main__":
    main()
