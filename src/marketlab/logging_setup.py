"""구조화(JSON Lines) 로그. 사람이 보는 콘솔 출력과 기계가 읽는 파일 로그를 분리한다."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from .config import get_settings

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    s = get_settings()
    s.paths.logs.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("marketlab")
    root.setLevel(level)
    fh = logging.FileHandler(s.paths.logs / "marketlab.jsonl", encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(fh)
    root.addHandler(ch)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(f"marketlab.{name}")


def log_event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, msg, extra={"fields": fields})
