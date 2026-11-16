"""Small JSON log formatter; request bodies and credential values are never logged."""

import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event", record.getMessage()),
        }
        for name in ("correlation_id", "method", "path", "status_code", "duration_ms", "exception_type"):
            if hasattr(record, name):
                entry[name] = getattr(record, name)
        return json.dumps(entry)


def configure_logging() -> None:
    logger = logging.getLogger("bestapp")
    if not any(getattr(handler, "bestapp_json", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler.bestapp_json = True
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
