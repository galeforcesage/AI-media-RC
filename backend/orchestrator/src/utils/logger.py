"""
logger.py
Structured logger for the Orchestrator backend.
Supports JSON or human-readable output.
"""

from __future__ import annotations
import logging
import json
import sys
from typing import Any, Dict


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
        }
        return json.dumps(payload)


def get_logger(name: str, json_mode: bool = False) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        if json_mode:
            handler.setFormatter(JsonFormatter())
        else:
            handler.setFormatter(
                logging.Formatter(
                    "[%(asctime)s] [%(levelname)s] %(module)s: %(message)s",
                    "%Y-%m-%d %H:%M:%S",
                )
            )
        logger.addHandler(handler)

    return logger
