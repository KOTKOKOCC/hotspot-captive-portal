from io import StringIO, BytesIO, TextIOWrapper
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import csv

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from db import db, fetch_all
from ui import format_dt, humanize_details
from labels import (
    COLUMN_LABELS,
    EVENT_LABELS,
    RESULT_LABELS,
    AUTH_METHOD_LABELS,
    STATUS_LABELS,
    TERMINATE_CAUSE_LABELS,
)


EXPORT_TABLES = {
    "guests": {
        "query": "SELECT * FROM guests",
        "order": "created_at DESC",
        "date_column": None,
        "columns": ["id", "phone", "first_verified_at", "first_hotel", "auth_method", "status", "created_at", "updated_at"],
        "sheet": "Guests",
        "filename": "guests.csv",
    },
    "sessions": {
        "query": "SELECT * FROM guest_sessions",
        "order": "started_at DESC",
        "date_column": "started_at",
        "columns": ["guest_id", "phone", "mac", "ip", "device_name", "started_at", "last_seen_at", "ended_at", "status", "terminate_cause", "acct_session_time", "hotel", "ssid", "vlan_id", "nas_id", "acct_session_id"],
        "sheet": "Sessions",
        "filename": "guest_sessions.csv",
    },
    "pending": {
        "query": "SELECT * FROM pending_auth",
        "order": "created_at DESC",
        "date_column": "created_at",
        "columns": ["id", "phone", "mac", "ip", "nas_id", "hotel", "ssid", "vlan_id", "created_at", "expires_at", "status"],
        "sheet": "Pending",
        "filename": "pending_auth.csv",
    },
    "calls": {
        "query": "SELECT * FROM call_events",
        "order": "created_at DESC",
        "date_column": "created_at",
        "columns": ["id", "phone", "callerid_raw", "source_ip", "created_at", "result"],
        "sheet": "Calls",
        "filename": "call_events.csv",
    },
    "audit": {
        "query": "SELECT * FROM audit_log",
        "order": "event_time DESC",
        "date_column": "event_time",
        "columns": ["id", "phone", "mac", "ip", "nas_id", "hotel", "ssid", "vlan_id", "event_type", "event_time", "details"],
        "sheet": "Audit",
        "filename": "audit_log.csv",
    },
    "networks": {
        "query": "SELECT * FROM network_map",
        "order": "vlan_id",
        "date_column": None,
        "columns": ["id", "hotel_name", "ssid_name", "vlan_id", "subnet_cidr", "mikrotik_interface", "hotspot_server", "is_active"],
        "sheet": "Networks",
        "filename": "network_map.csv",
    },
}


EXPORT_TABLE_ORDER = ["guests", "sessions", "pending", "calls", "audit", "networks"]


DATETIME_COLUMNS = {
    "created_at", "updated_at", "first_verified_at",
    "started_at", "ended_at", "expires_at",
    "event_time", "last_seen_at", "last_auth_at",
}


def export_table_names() -> tuple[str, ...]:
    return tuple(EXPORT_TABLE_ORDER)


def export_formats() -> tuple[str, ...]:
    return ("zip", "xlsx")


def _date_where(col: str | None, date_from: str | None, date_to: str | None) -> tuple[str, list[str]]:
    if not col:
        return "", []

    clauses = []
    params: list[str] = []

    if date_from:
        clauses.append(f"date({col}) >= date(?)")
        params.append(date_from)
    if date_to:
        clauses.append(f"date({col}) <= date(?)")
        params.append(date_to)

    if not clauses:
        return "", []

    return " WHERE " + " AND ".join(clauses), params


def _build_query(table_name: str, date_from: str | None = None, date_to: str | None = None) -> tuple[str, list[str], list[str], str]:
    spec = EXPORT_TABLES.get(table_name)
    if not spec:
        raise ValueError("unknown export table")

    where_sql, params = _date_where(spec["date_column"], date_from, date_to)
    query = f"{spec['query']}{where_sql} ORDER BY {spec['order']}"
    return query, params, list(spec["columns"]), str(spec["sheet"])


def _format_export_value(col_name: str, value):
    if value is None:
        return ""

    if col_name in DATETIME_COLUMNS:
        return format_dt(value)
    if col_name == "event_type":
        return EVENT_LABELS.get(str(value), str(value))
    if col_name == "result":
        return RESULT_LABELS.get(str(value), str(value))
    if col_name == "details":
        return humanize_details(str(value))
    if col_name == "auth_method":
        return AUTH_METHOD_LABELS.get(str(value), str(value))
    if col_name == "status":
        return STATUS_LABELS.get(str(value).lower(), str(value))
    if col_name == "terminate_cause":
        return TERMINATE_CAUSE_LABELS.get(str(value), str(value))

    return value


def _iter_rows(query: str, params: list[str], chunk_size: int = 1000):
    conn = db()
    try:
        cur = conn.execute(query, tuple(params))
        while True:
            rows = cur.fetchmany(chunk_size)
            if not rows:
                break
            for row in rows:
                yield row
    finally:
        conn.close()


def write_export_zip_file(
    output_path: str | Path,
    date_from: str | None = None,
    date_to: str | None = None,
    table_name: str = "all",
) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    table_names = EXPORT_TABLE_ORDER if table_name == "all" else [table_name]

    with ZipFile(output_path, "w", ZIP_DEFLATED, allowZip64=True) as zf:
        for current_table in table_names:
            spec = EXPORT_TABLES[current_table]
            query, params, columns, _sheet_name = _build_query(current_table, date_from, date_to)

            with zf.open(str(spec["filename"]), "w", force_zip64=True) as raw:
                text_stream = TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                writer = csv.writer(text_stream, delimiter=";", quoting=csv.QUOTE_MINIMAL)
                writer.writerow([COLUMN_LABELS.get(col, col) for col in columns])
                for row in _iter_rows(query, params):
                    writer.writerow([_format_export_value(col, row[col]) for col in columns])
                text_stream.flush()


def write_single_xlsx_file(
    output_path: str | Path,
    table_name: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    query, params, columns, sheet_name = _build_query(table_name, date_from, date_to)

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title=sheet_name[:31])
    ws.append([COLUMN_LABELS.get(col, col) for col in columns])

    for row in _iter_rows(query, params):
        ws.append([_format_export_value(col, row[col]) for col in columns])

    wb.save(output_path)


def rows_to_csv_bytes(rows, columns):
    sio = StringIO()
    writer = csv.writer(sio, delimiter=';', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row[col] if row[col] is not None else "" for col in columns])
    return sio.getvalue().encode("utf-8-sig")


def rows_to_xlsx_bytes(rows, columns, sheet_name="Sheet1"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]

    border_side = Side(style="medium", color="B8C4D6")
    header_fill = PatternFill(fill_type="solid", fgColor="DCE6F1")

    # заголовки
    for col_idx, col_name in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=col_idx, value=COLUMN_LABELS.get(col_name, col_name))
        cell.font = Font(bold=True, size=12)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(left=border_side, right=border_side, top=border_side, bottom=border_side)

    # данные
    for row_idx, row in enumerate(rows, start=2):
        for col_idx, col_name in enumerate(columns, start=1):
            value = row[col_name] if row[col_name] is not None else ""

            value = _format_export_value(col_name, value)

            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = Font(size=11)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = Border(left=border_side, right=border_side, top=border_side, bottom=border_side)

    # заморозка шапки
    ws.freeze_panes = "A2"

    # автофильтр
    ws.auto_filter.ref = ws.dimensions

    # высота строки заголовка
    ws.row_dimensions[1].height = 36

    # автоширина колонок
    for col_idx, col_name in enumerate(columns, start=1):
        header_text = str(COLUMN_LABELS.get(col_name, col_name))
        max_len = len(header_text)

        for row_idx in range(2, ws.max_row + 1):
            cell_val = ws.cell(row=row_idx, column=col_idx).value
            if cell_val is not None:
                max_len = max(max_len, len(str(cell_val)))

        width = min(max(max_len + 4, 14), 40)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio


def build_export_zip(date_from: str | None = None, date_to: str | None = None):
    files = []

    for table_name in EXPORT_TABLE_ORDER:
        spec = EXPORT_TABLES[table_name]
        query, params, columns, _sheet_name = _build_query(table_name, date_from, date_to)
        files.append((spec["filename"], fetch_all(query, tuple(params)), columns))

    bio = BytesIO()
    with ZipFile(bio, "w", ZIP_DEFLATED) as zf:
        for filename, rows, cols in files:
            zf.writestr(filename, rows_to_csv_bytes(rows, cols))

    bio.seek(0)
    return bio


def build_single_xlsx(table_name: str, date_from: str | None = None, date_to: str | None = None):
    query, params, columns, sheet_name = _build_query(table_name, date_from, date_to)
    rows = fetch_all(query, tuple(params))
    return rows_to_xlsx_bytes(rows, columns, sheet_name)
