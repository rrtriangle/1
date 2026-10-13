"""이동평균 계열 특징 재현 테스트 (가상 주가, 격리된 임시 데이터 디렉터리).

버그: li_anchor = first_value(li) OVER (ROWS 260 PRECEDING) 가 행마다 달라, 상장 후 261거래일부터
avg(exp(li - li_anchor)) 가 서로 다른 기준점으로 잰 값을 평균했다. 꾸준히 연 20% 오르는 주가에서는
창 안의 모든 값이 exp(260·g) 로 같아져 ma_gap_20 이 정확히 0 이 된다(정답은 약 +0.69%).

검증 방법: 같은 가상 주가로 pandas rolling 계산값(독립 구현)과 특징 파일 값을 비교한다.
기업행위가 없으므로 수익률 지수는 종가에 비례하고, 비율형 특징은 종가로 계산한 값과 같아야 한다.
"""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from marketlab.research import features as F
from marketlab.storage.catalog import catalog, memory_connection

AFFECTED = ["ma_gap_5", "ma_gap_20", "ma_gap_60", "ma_gap_120", "ma_5_20", "ma_20_60", "ma_60_120",
            "macd", "macd_signal", "bb_pctb_20", "bb_width_20"]
OTHER = ["ret_5", "ret_20", "ret_60", "ret_120", "rv_5", "rv_20", "rv_60", "pos_high_252", "pos_low_252", "dd_60",
         "up_ratio_20", "rsi_14", "er_14", "sharpe_60"]
G = np.log(1.2) / 252                       # 연 20% 꾸준한 상승


def _synthetic_prices() -> dict[str, pd.DataFrame]:
    days = pd.bdate_range("2017-01-02", "2021-12-31")
    n = len(days)
    rng = np.random.default_rng(7)
    series = {
        "KR-STEADY-20170102": 10_000 * np.exp(G * np.arange(n)),
        "KR-RANDOM-20170102": 10_000 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, n))),
    }
    out = {}
    for sid, close in series.items():
        close = np.round(close, 6)
        prev = np.r_[np.nan, close[:-1]]
        out[sid] = pd.DataFrame({
            "security_id": sid, "country": "KR", "market": "KOSPI", "symbol": sid[3:9], "name": sid,
            "trade_date": days.date, "open": close, "high": close * 1.001, "low": close * 0.999, "close": close,
            "volume": np.int64(1_000_000), "amount": close * 1_000_000, "market_cap": close * 1e8,
            "base_price": prev, "prev_close": prev, "quality_grade": "A", "usable": True, "is_traded": True,
        })
    return out


@pytest.fixture
def synthetic(isolated):
    s = isolated
    data = _synthetic_prices()
    allrows = pd.concat(data.values(), ignore_index=True)
    for y, g in allrows.groupby(pd.to_datetime(allrows["trade_date"]).dt.year):
        p = s.paths.standardized / "daily_prices" / "country=KR" / f"year={y}" / "part-0.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        g.drop(columns=[]).to_parquet(p, index=False)
    # 시장 지표 파일 1개 (시장 수익률 0)
    mi = s.paths.standardized / "market_indicators"
    mi.mkdir(parents=True, exist_ok=True)
    days = sorted(allrows["trade_date"].unique())
    pd.DataFrame({"indicator_id": "MARCAP:KOSPI_CW_RETURN", "obs_date": days, "value": 0.0,
                  "available_at": pd.to_datetime(days).tz_localize("Asia/Seoul") + pd.Timedelta(hours=18)}).to_parquet(mi / "kr_breadth.parquet", index=False)
    with catalog() as con:
        for sid in data:
            con.execute("INSERT INTO securities (security_id, country, market, security_type, name, status) VALUES (?, 'KR', 'KOSPI', 'common', ?, 'listed')", [sid, sid])
    return data


def _expected(close: pd.Series) -> pd.DataFrame:
    """pandas rolling 으로 계산한 정답 (DuckDB 와 같은 정의: 창=최근 k행, 표본표준편차)."""
    e = pd.DataFrame(index=close.index)
    ma = {k: close.rolling(k).mean() for k in (5, 12, 20, 26, 60, 120)}
    for k in (5, 20, 60, 120):
        e[f"ma_gap_{k}"] = close / ma[k] - 1
    e["ma_5_20"] = ma[5] / ma[20] - 1
    e["ma_20_60"] = ma[20] / ma[60] - 1
    e["ma_60_120"] = ma[60] / ma[120] - 1
    e["macd"] = (ma[12] - ma[26]) / close
    e["macd_signal"] = e["macd"].rolling(9).mean()
    sd20 = close.rolling(20).std(ddof=1)
    e["bb_pctb_20"] = (close - ma[20]) / (2 * sd20)
    e["bb_width_20"] = 4 * sd20 / ma[20]
    lr = np.log(close / close.shift(1)).fillna(0.0)
    r = close / close.shift(1) - 1
    for k in (5, 20, 60, 120):
        e[f"ret_{k}"] = close / close.shift(k) - 1
    for k in (5, 20, 60):
        e[f"rv_{k}"] = lr.rolling(k).std(ddof=1) * np.sqrt(252)
    e["pos_high_252"] = close / close.rolling(252, min_periods=1).max() - 1
    e["pos_low_252"] = close / close.rolling(252, min_periods=1).min() - 1
    e["dd_60"] = close / close.rolling(60, min_periods=1).max() - 1
    e["up_ratio_20"] = np.where(r > 0, 1.0, np.where(r < 0, 0.0, 0.5))
    e["up_ratio_20"] = pd.Series(e["up_ratio_20"], index=close.index).rolling(20, min_periods=1).mean()
    up = lr.clip(lower=0).rolling(14, min_periods=1).sum()
    dn = (-lr).clip(lower=0).rolling(14, min_periods=1).sum()
    e["rsi_14"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    e["er_14"] = (np.log(close) - np.log(close.shift(14))).abs() / lr.abs().rolling(14, min_periods=1).sum().replace(0, np.nan)
    e["sharpe_60"] = lr.rolling(60).mean() / lr.rolling(60).std(ddof=1) * np.sqrt(252)
    return e


@pytest.mark.parametrize("year", [2019, 2021])      # 2019: 상장 후 260일 이내 구간 포함, 2021: 500일 이후
def test_moving_average_features_match_independent_calculation(synthetic, tmp_path, year):
    con = memory_connection()
    try:
        F.build_market_env(con, "KR")
        out = tmp_path / f"f{year}.parquet"
        F.build_year(con, "KR", year, out_path=out)
        got = con.execute(f"SELECT * FROM read_parquet('{out}') ORDER BY security_id, trade_date").df()
    finally:
        con.close()
    got["trade_date"] = pd.to_datetime(got["trade_date"]).dt.date
    for sid, df in synthetic.items():
        # 특징 계산 구간(year-2 ~)과 같은 데이터로 정답 계산
        src = df[pd.to_datetime(df["trade_date"]).dt.year >= year - 2].set_index("trade_date")["close"]
        exp = _expected(src)
        g = got[got["security_id"] == sid].set_index("trade_date")
        assert len(g) > 100
        for c in AFFECTED + OTHER:
            a = g[c].astype(float)
            b = exp.loc[g.index, c].astype(float)
            ok = np.isclose(a, b, rtol=2e-4, atol=1e-6, equal_nan=True)
            assert ok.all(), f"{sid} {c}: {(~ok).sum()}행 불일치 (예: {a[~ok].iloc[0]:.6f} vs 정답 {b[~ok].iloc[0]:.6f})"


def test_steady_growth_ma_gap_is_not_zero_after_260_days(synthetic, tmp_path):
    """꾸준히 연 20% 오르는 주가: ma_gap_20 은 항상 1/mean(exp(-g·j), j<20) − 1 ≈ +0.69% 여야 하며 0 이 되면 안 된다."""
    con = memory_connection()
    try:
        F.build_market_env(con, "KR")
        out = tmp_path / "f.parquet"
        F.build_year(con, "KR", 2021, out_path=out)
        v = con.execute(f"SELECT ma_gap_20 FROM read_parquet('{out}') WHERE security_id='KR-STEADY-20170102'").df()["ma_gap_20"]
    finally:
        con.close()
    analytic = 1 / np.mean(np.exp(-G * np.arange(20))) - 1
    assert analytic == pytest.approx(0.0069, abs=2e-4)
    assert (v.abs() > 1e-3).all(), "상장 후 260일 이후 ma_gap_20 이 0 으로 붕괴 (기준점 버그)"
    assert np.allclose(v, analytic, rtol=1e-3)
