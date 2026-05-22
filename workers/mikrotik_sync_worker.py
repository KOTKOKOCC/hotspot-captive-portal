import logging
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from integrations.mikrotik.api import sync_session_device_names

from logging_config import setup_logging

setup_logging()
logger = logging.getLogger("mikrotik-sync-worker")

MIKROTIK_SYNC_INTERVAL_SECONDS = int(os.getenv("MIKROTIK_SYNC_INTERVAL_SECONDS", "300"))




def main():
    logger.info(
        "mikrotik sync worker started, interval=%s seconds",
        MIKROTIK_SYNC_INTERVAL_SECONDS,
    )

    while True:
        try:
            sync_session_device_names()
            logger.info("mikrotik sync iteration done")
        except Exception:
            logger.exception("mikrotik sync iteration failed")

        time.sleep(MIKROTIK_SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
