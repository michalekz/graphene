"""
Structured JSON logging for Graphene Intel.
All components use this setup — JSON lines format for easy log parsing.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = os.getenv("LOG_DIR", "/var/log/graphene-intel")


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        # Any extra fields passed via extra={...}
        for key in ("ticker", "source", "url", "score", "count"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        return json.dumps(data, ensure_ascii=False)


def setup_logging(component: str = "graphene-intel") -> logging.Logger:
    """
    Configure root logger + component logger.

    Routing strategy avoids double-writing to log files:
    - Interactive terminal (tty): stdout + file (developer sees live output).
    - Non-interactive (cron, nohup): file only — prevents duplicate lines
      when cron also redirects stdout to the same log file via >> logfile 2>&1.

    If LOG_DIR is not set and we're non-interactive, falls back to stdout.
    """
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)

    # Remove any existing handlers (avoid duplicates on re-import)
    root.handlers.clear()

    is_tty = sys.stdout.isatty()
    file_handler_added = False

    if LOG_DIR:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            log_file = os.path.join(LOG_DIR, f"{component}.log")
            fh = logging.FileHandler(log_file)
            fh.setFormatter(JsonFormatter())
            root.addHandler(fh)
            file_handler_added = True
        except OSError:
            pass

    # Add stdout handler when interactive (tty) OR when no file handler available
    if is_tty or not file_handler_added:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(JsonFormatter())
        root.addHandler(sh)

    logger = logging.getLogger(component)
    logger.info("Logging initialized", extra={"component": component, "level": LOG_LEVEL})
    return logger
