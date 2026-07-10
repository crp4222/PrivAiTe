from __future__ import annotations

import json
import logging
import sys


class _JsonFormatter(logging.Formatter):
    # json.dumps every field: a message containing quotes or newlines must still
    # produce one valid JSON line (the old %-template emitted broken JSON).
    # Deliberately omit exception tracebacks. Third-party detector exceptions can
    # include the text they inspected, and a privacy proxy must never serialize
    # that text into an operator log.
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        return json.dumps(payload, ensure_ascii=False)


class _PrivacySafeTextFormatter(logging.Formatter):
    """Format text logs without an exception traceback.

    ``logging.Formatter`` normally appends ``record.exc_info`` automatically.
    Strip it for this handler, then restore the record so other application
    handlers retain their normal behavior.
    """

    def format(self, record: logging.LogRecord) -> str:
        exc_info, exc_text = record.exc_info, record.exc_text
        record.exc_info = None
        record.exc_text = None
        try:
            return super().format(record)
        finally:
            record.exc_info = exc_info
            record.exc_text = exc_text


def setup_logging(level: str = "info", fmt: str = "text") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stderr)

    formatter: logging.Formatter
    if fmt == "json":
        formatter = _JsonFormatter()
    else:
        formatter = _PrivacySafeTextFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    handler.setFormatter(formatter)

    root = logging.getLogger("privaite")
    root.setLevel(log_level)
    root.addHandler(handler)
    root.propagate = False
