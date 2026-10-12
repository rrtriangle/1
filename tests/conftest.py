"""테스트 공통 설정.

- 'data' 마커: 실제 수집된 데이터(기본 data/ 또는 MARKETLAB_DATA_DIR)가 있어야 실행된다.
- 'network' 마커: 실제 원천 접속. 오프라인이면 건너뛴다.
- isolated 픽스처: 임시 데이터 디렉터리로 설정을 바꿔 실제 데이터를 건드리지 않는다.
"""
from __future__ import annotations

import pytest

from marketlab.config import get_settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETLAB_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    s = get_settings()
    s.paths.ensure()
    yield s
    get_settings.cache_clear()


def _has_real_data() -> bool:
    get_settings.cache_clear()
    s = get_settings()
    return any((s.paths.standardized / "daily_prices" / "country=KR").glob("year=*/*.parquet"))


def pytest_collection_modifyitems(config, items):
    has = _has_real_data()
    skip = pytest.mark.skip(reason="수집된 실제 데이터 없음 (marketlab ingest --market KR 먼저 실행)")
    for it in items:
        if "data" in it.keywords and not has:
            it.add_marker(skip)
