from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "info", fmt: str = "text") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stderr)

    if fmt == "json":
        formatter = logging.Formatter(
            '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )

    handler.setFormatter(formatter)

    root = logging.getLogger("privaite")
    root.setLevel(log_level)
    root.addHandler(handler)
    root.propagate = False
