"""설정 로딩. 소스코드 수정 없이 데이터 경로를 바꿀 수 있도록 환경변수를 우선한다."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """의존성 없이 .env 를 읽는다. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value and key.strip() not in os.environ:
            os.environ[key.strip()] = value


@dataclass(frozen=True)
class Paths:
    root: Path
    data: Path
    logs: Path
    reports: Path
    models: Path

    @property
    def raw(self) -> Path:
        return self.data / "raw"

    @property
    def standardized(self) -> Path:
        return self.data / "standardized"

    @property
    def curated(self) -> Path:
        return self.data / "curated"

    @property
    def metadata(self) -> Path:
        return self.data / "metadata"

    @property
    def quarantine(self) -> Path:
        return self.data / "quarantine"

    @property
    def tmp(self) -> Path:
        return self.data / "tmp"

    @property
    def catalog_db(self) -> Path:
        return self.metadata / "catalog.duckdb"

    @property
    def state_dir(self) -> Path:
        return self.metadata / "state"

    def ensure(self) -> None:
        for p in (self.raw, self.standardized, self.curated, self.metadata, self.quarantine,
                  self.tmp, self.state_dir, self.logs, self.reports, self.models):
            p.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]
    paths: Paths
    config_path: Path

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def source(self, name: str) -> dict[str, Any]:
        return dict(self.raw.get("sources", {}).get(name, {}))


def _resolve(base: Path, value: str) -> Path:
    p = Path(os.path.expanduser(value))
    return p if p.is_absolute() else (base / p)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(PROJECT_ROOT / ".env")
    cfg_path = Path(os.environ.get("MARKETLAB_CONFIG", PROJECT_ROOT / "config" / "settings.yaml"))
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    p = raw.get("paths", {})
    data_dir = os.environ.get("MARKETLAB_DATA_DIR") or p.get("data_dir", "data")
    paths = Paths(
        root=PROJECT_ROOT,
        data=_resolve(PROJECT_ROOT, data_dir),
        logs=_resolve(PROJECT_ROOT, os.environ.get("MARKETLAB_LOGS_DIR") or p.get("logs_dir", "logs")),
        reports=_resolve(PROJECT_ROOT, p.get("reports_dir", "reports")),
        models=_resolve(PROJECT_ROOT, p.get("models_dir", "models")),
    )
    return Settings(raw=raw, paths=paths, config_path=cfg_path)
