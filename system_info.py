import shutil
import os
import time


def human_bytes(size: int) -> str:
    size = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def get_disk_usage(path: str = "/") -> dict:
    total, used, free = shutil.disk_usage(path)
    percent = round((used / total) * 100, 1)

    return {
        "path": path,
        "total": total,
        "used": used,
        "free": free,
        "percent": percent,
        "total_h": human_bytes(total),
        "used_h": human_bytes(used),
        "free_h": human_bytes(free),
    }


def disk_status_class(percent: float) -> str:
    if percent >= 85:
        return "bad"
    if percent >= 70:
        return "warn"
    return "ok"



def get_memory_usage() -> dict:
    data = {}
    with open("/proc/meminfo", "r") as f:
        for line in f:
            key, value = line.split(":", 1)
            data[key] = int(value.strip().split()[0]) * 1024

    total = data.get("MemTotal", 0)
    available = data.get("MemAvailable", 0)
    used = total - available
    percent = round((used / total) * 100, 1) if total else 0

    return {
        "total": total,
        "used": used,
        "available": available,
        "percent": percent,
        "total_h": human_bytes(total),
        "used_h": human_bytes(used),
        "available_h": human_bytes(available),
    }


def get_cpu_load() -> dict:
    load1, load5, load15 = os.getloadavg()
    cores = os.cpu_count() or 1
    percent = round((load1 / cores) * 100, 1)

    return {
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
        "cores": cores,
        "percent": percent,
    }


def get_uptime() -> dict:
    with open("/proc/uptime", "r") as f:
        seconds = int(float(f.read().split()[0]))

    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60

    if days:
        text = f"{days}д {hours}ч {minutes}м"
    elif hours:
        text = f"{hours}ч {minutes}м"
    else:
        text = f"{minutes}м"

    return {
        "seconds": seconds,
        "text": text,
    }