from __future__ import annotations

import json
import logging
import sys


class _JsonFormatter(logging.Formatter):
    # json.dumps every field: a message containing quotes or newlines must still
    # produce one valid JSON line (the old %-template emitted broken JSON).
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "info", fmt: str = "text") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stderr)

    formatter: logging.Formatter
    if fmt == "json":
        formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )

    handler.setFormatter(formatter)

    root = logging.getLogger("privaite")
    root.setLevel(log_level)
    root.addHandler(handler)
    root.propagate = False
