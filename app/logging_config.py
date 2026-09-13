"""Runtime logging configuration."""

import json
import logging
import os
from datetime import UTC, datetime


class JsonLogFormatter(logging.Formatter):
    """Emit one machine-readable object without serializing exception values."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Configure JSON logs by default, with an opt-in traditional text format."""
    handler = logging.StreamHandler()
    if os.getenv("LOG_FORMAT", "json").strip().lower() == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    requested_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    root.setLevel(
        requested_level
        if requested_level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        else "INFO"
    )
