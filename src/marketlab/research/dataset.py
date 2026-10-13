"""학습용 데이터 로딩: 필요한 기간·컬럼만, float32, 표본 간격 적용 (DuckDB 컬럼 선택 읽기)."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..config import get_settings
from ..storage.catalog import catalog, memory_connection
from .features import FEATURES, feature_glob

KEYS = ["security_id", "trade_date"]


def trading_days(market: str) -> list[date]:
    with catalog(read_only=True) as cat:
        return [r[0] for r in cat.execute("SELECT trade_date FROM trading_calendars WHERE market=? ORDER BY 1",
                                          [market]).fetchall()]


def purge_cutoff(market: str, boundary: date, horizon: int) -> date:
    """boundary 이전 학습 행 중 라벨 기간(t+1 ~ t+horizon)이 boundary 이후로 넘어가는 행을 제거하기 위한 마지막 허용일."""
    days = [d for d in trading_days(market) if d < boundary]
    if len(days) <= horizon + 1:
        return boundary - timedelta(days=int(horizon * 1.6) + 3)
    return days[-(horizon + 2)]


def load(market: str, start: date | str, end: date | str, features: list[str] | None = None,
         labels: list[str] | None = None, sample_every: int | None = None, extra: list[str] | None = None,
         require_label: str | None = None) -> pd.DataFrame:
    s = get_settings()
    feats = features or FEATURES
    labs = labels or []
    every = sample_every if sample_every is not None else int(s.get("research.sampling_every_n_days", 5))
    cols = KEYS + ["market", "close", "amount_20", "market_cap", "entry_ok"] + (extra or []) + feats + labs
    cols = list(dict.fromkeys(cols))
    con = memory_connection()
    try:
        where = f"trade_date BETWEEN DATE '{start}' AND DATE '{end}' AND eligible"
        if require_label:
            where += f" AND {require_label} IS NOT NULL AND entry_ok"
        sample = ""
        if every and every > 1:
            sample = f"QUALIFY (dense_rank() OVER (ORDER BY trade_date) - 1) % {every} = 0"
        df = con.execute(f"""
            SELECT {", ".join(cols)} FROM read_parquet('{feature_glob(market)}', hive_partitioning=false, union_by_name=true)
            WHERE {where} {sample}
        """).df()
    finally:
        con.close()
    for c in feats + [x for x in labs if not x.startswith("y_up") and not x.startswith("y_trunc")]:
        if c in df:
            df[c] = df[c].astype(np.float32)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def count_rows(market: str, start, end, require_label: str | None = None, sample_every: int = 1) -> int:
    con = memory_connection()
    try:
        where = f"trade_date BETWEEN DATE '{start}' AND DATE '{end}' AND eligible"
        if require_label:
            where += f" AND {require_label} IS NOT NULL AND entry_ok"
        n, d = con.execute(f"SELECT count(*), count(DISTINCT trade_date) FROM read_parquet('{feature_glob(market)}') WHERE {where}").fetchone()
    finally:
        con.close()
    return int(n / max(1, sample_every))
