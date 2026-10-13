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


@pytest.fixture
def roomy(isolated):
    """저장공간 규칙과 무관한 테스트용: 실행 머신의 남은 디스크와 상관없이 DiskGuard 가 막지 않게 한다."""
    isolated.raw.setdefault("storage", {})
    isolated.raw["storage"]["min_free_gb"] = 0
    isolated.raw["storage"]["project_budget_gb"] = 10_000
    isolated.raw["storage"]["assumed_free_gb_baseline"] = 0
    return isolated


@pytest.fixture
def synthetic_up(isolated, monkeypatch):
    """합성 특징·라벨(2016~2023, 8종목)로 dataset.load 등을 대체 — 학습·walk-forward 로직 테스트용."""
    import numpy as np
    import pandas as pd
    from marketlab.research import dataset
    from marketlab.research.features import FEATURES
    days = list(pd.bdate_range("2016-01-04", "2023-12-29").date)
    rng = np.random.default_rng(0)
    n = 8
    D = np.repeat(np.array(days, dtype=object), n)
    X = rng.normal(size=(len(D), len(FEATURES))).astype(np.float32)
    ret = 0.02 * X[:, 0] + rng.normal(scale=0.05, size=len(D))
    base = pd.DataFrame(X, columns=FEATURES)
    base.insert(0, "security_id", np.tile([f"KR-T{i:03d}" for i in range(n)], len(days)))
    base.insert(1, "trade_date", D)
    base["y_ret_20"] = ret.astype(np.float32)
    base["y_up_20"] = (ret > 0).astype(float)

    def fake_load(market, start, end, features=None, labels=None, sample_every=None, extra=None, require_label=None):
        s, e = pd.Timestamp(start).date(), pd.Timestamp(end).date()
        out = base[(base["trade_date"] >= s) & (base["trade_date"] <= e)]
        if sample_every and sample_every > 1:
            keep = sorted(out["trade_date"].unique())[::sample_every]
            out = out[out["trade_date"].isin(keep)]
        return out.reset_index(drop=True)

    monkeypatch.setattr(dataset, "load", fake_load)
    monkeypatch.setattr(dataset, "count_rows", lambda *a, **k: 1000)
    monkeypatch.setattr(dataset, "trading_days", lambda market: days)
    isolated.raw.setdefault("research", {})["train_start"] = {"KR": "2016-01-01"}
    return days
