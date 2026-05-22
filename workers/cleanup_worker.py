import logging
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from services import run_cleanup

from logging_config import setup_logging

setup_logging()
logger = logging.getLogger("cleanup-worker")

CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "60"))



def main():
    logger.info("cleanup worker started, interval=%s seconds", CLEANUP_INTERVAL_SECONDS)

    while True:
        try:
            result = run_cleanup()
            logger.info("cleanup result: %s", result)
        except Exception:
            logger.exception("cleanup iteration failed")

        time.sleep(CLEANUP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()