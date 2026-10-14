"""3단계 설계 보완 테스트 (합성 데이터).

- walk_forward: 기본은 valid_end 연도까지만, 최종 검증 단계에서 through='test' 로 이어서 계산(기록 남김)
- evaluation: 날짜별 IC t값의 Newey-West 보정 (라그 = ceil(horizon / 표본 간격))
- strategies.search: 시도 횟수를 반영한 deflated Sharpe
- 비용: 거래대금 구간별 슬리피지
"""
from __future__ import annotations

import math
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from marketlab.research import backtest as bt
from marketlab.research import evaluation as ev
from marketlab.research.portfolio import Portfolio


# ---------------------------------------------------------------- walk-forward 범위
def _wf_config(isolated, monkeypatch, years, valid_end="2021-12-31"):
    from marketlab.research import experiments as E
    monkeypatch.setattr(E, "available_years", lambda market: list(years))
    r = isolated.raw.setdefault("research", {})
    r["splits"] = {"train_end": "2019-12-31", "valid_start": "2020-01-01", "valid_end": valid_end, "test_start": "2022-01-01"}
    r["walkforward"] = {"first_train_years": 2, "min_test_year": {"KR": 2000}, "rolling_years": 8}
    return E


def test_wf_test_years_stop_at_valid_end(isolated, monkeypatch):
    E = _wf_config(isolated, monkeypatch, range(2016, 2026))
    v, t = E.wf_test_years("KR"), E.wf_test_years("KR", "test")
    assert max(v) == 2021 and min(v) == 2019
    assert t[: len(v)] == v and t[len(v):] == [2022, 2023, 2024, 2025]   # fold 번호가 유지되도록 앞부분 동일
    with pytest.raises(ValueError):
        E.wf_test_years("KR", "all")


def test_wf_test_years_mid_year_valid_end_excludes_that_year(isolated, monkeypatch):
    E = _wf_config(isolated, monkeypatch, range(2016, 2026), valid_end="2021-06-30")
    assert max(E.wf_test_years("KR")) == 2020   # 2021 년 fold 는 test 구간(하반기)까지 예측하므로 제외


def test_walk_forward_default_has_no_test_predictions_then_extends(synthetic_up, isolated, monkeypatch):
    E = _wf_config(isolated, monkeypatch, range(2016, 2024))
    from marketlab.research.store import research_db
    rid = "wf-KR-return-20-expanding"
    E.walk_forward("KR", 20, "return", ["ridge"], run_id=rid)
    with research_db(read_only=True) as db:
        mx = db.execute("SELECT max(trade_date) FROM predictions WHERE run_id=?", [rid]).fetchone()[0]
        folds = db.execute("SELECT fold FROM fold_results WHERE run_id=? ORDER BY 1", [rid]).fetchall()
        n_log = db.execute("SELECT count(*) FROM test_access_log").fetchone()[0]
    assert mx <= date(2021, 12, 31), "기본 walk-forward 가 최종 검증 구간을 예측함"
    assert [f[0] for f in folds] == [0, 1, 2] and n_log == 0
    E.walk_forward("KR", 20, "return", ["ridge"], run_id=rid, through="test")
    with research_db(read_only=True) as db:
        mx2 = db.execute("SELECT max(trade_date) FROM predictions WHERE run_id=?", [rid]).fetchone()[0]
        folds2 = db.execute("SELECT fold FROM fold_results WHERE run_id=? ORDER BY 1", [rid]).fetchall()
        log = db.execute("SELECT who, ref FROM test_access_log").fetchall()
    assert mx2 >= date(2023, 12, 1)
    assert [f[0] for f in folds2] == [0, 1, 2, 3, 4]
    assert log == [("walk_forward", rid)]
    # 다시 불러도 이미 계산된 fold 는 건너뛰고, 기록도 늘지 않음
    E.walk_forward("KR", 20, "return", ["ridge"], run_id=rid, through="test")
    with research_db(read_only=True) as db:
        assert db.execute("SELECT count(*) FROM test_access_log").fetchone()[0] == 1


# ---------------------------------------------------------------- Newey-West
def test_newey_west_lag_and_plain_equivalence():
    assert ev.nw_lag(20, 5) == 4 and ev.nw_lag(20, 1) == 20 and ev.nw_lag(5, 10) == 1 and ev.nw_lag(None, 5) == 0
    x = np.random.default_rng(0).normal(0.02, 0.1, 400)
    assert ev.newey_west_tstat(x, 0) == pytest.approx(x.mean() / (x.std() / math.sqrt(len(x))))


def test_newey_west_shrinks_t_for_overlapping_ic():
    # 20일 겹치는 라벨 → 날짜별 IC 는 20일 이동합 구조(강한 자기상관). 보통 t값은 약 sqrt(20) 배 부풀려진다.
    rng = np.random.default_rng(1)
    shocks = rng.normal(0.003, 0.05, 3000)
    ic = pd.Series(shocks).rolling(20).mean().dropna().to_numpy()
    naive = ic.mean() / (ic.std(ddof=1) / math.sqrt(len(ic)))
    nw = ev.newey_west_tstat(ic, 20)
    assert nw < naive / 2.5
    # 이 구조의 참 t값(독립 충격 기준)과 비슷한 크기
    true_t = shocks.mean() / (shocks.std(ddof=1) / math.sqrt(len(shocks)))
    assert nw == pytest.approx(true_t, rel=0.5)


def test_regression_metrics_reports_nw_t():
    rng = np.random.default_rng(2)
    days = pd.bdate_range("2020-01-01", periods=120).date
    df = pd.DataFrame([(d, rng.normal(), 0.0) for d in days for _ in range(30)], columns=["trade_date", "pred", "y"])
    df["y"] = 0.3 * df["pred"] + rng.normal(size=len(df))
    m = ev.regression_metrics(df, "pred", "y", horizon=20, sample_every=5)
    assert m["nw_lag"] == 4 and m["rank_ic_t_nw"] is not None and m["ic_t_nw"] is not None
    assert ev.regression_metrics(df, "pred", "y")["nw_lag"] == 0


# ---------------------------------------------------------------- Deflated Sharpe
def test_deflated_sharpe_single_trial_is_psr_and_more_trials_deflate():
    sr, T = 0.08, 500   # 일간 샤프 0.08 ≈ 연 1.27
    psr = ev.deflated_sharpe(sr, T, 0.0, 3.0, [sr], n_trials=1)
    assert psr == pytest.approx(ev._N.cdf(sr * math.sqrt(T - 1) / math.sqrt(1 + 2 / 4 * sr ** 2)))
    trials = list(np.random.default_rng(3).normal(0.0, 0.04, 50)) + [sr]
    d10 = ev.deflated_sharpe(sr, T, 0.0, 3.0, trials, n_trials=10)
    d500 = ev.deflated_sharpe(sr, T, 0.0, 3.0, trials, n_trials=500)
    assert psr > d10 > d500
    # 음의 왜도·두꺼운 꼬리는 DSR 을 낮춘다
    assert ev.deflated_sharpe(sr, T, -1.5, 10.0, trials, n_trials=10) < d10


def test_search_deflate_uses_cumulative_trial_count(isolated):
    from marketlab.research import strategies as S
    from marketlab.research.store import research_db
    period = "2010-01-01~2023-12-31"
    with research_db() as db:   # 같은 구간에서 예전에 이미 40번 시도한 기록
        for i in range(40):
            db.execute("INSERT INTO strategy_search_log VALUES ('old', 'momentum', 'KR', '{}', ?, 'sharpe', 0.1, 1, 10, '{}', FALSE, now())",
                       [period])
    rng = np.random.default_rng(4)
    df = pd.DataFrame({"strategy": [f"s{i}" for i in range(5)], "sharpe": [0.5, 0.9, 1.3, 0.2, 0.7],
                       "sr_daily": [0.03, 0.057, 0.082, 0.013, 0.044], "skew": 0.0, "kurt": 3.0, "n_obs": 1000})
    info = S._deflate(df, "KR", period)
    assert info["n_trials"] == 40 and info["n_trials_this_search"] == 5
    assert info["best_strategy"] == "s2" and 0 <= info["best_dsr"] <= 1
    assert df["dsr"].notna().all()
    alone = df.copy()
    info2 = S._deflate(alone, "KR", "다른 구간")
    assert info2["n_trials"] == 5 and info2["best_dsr"] > info["best_dsr"]


# ---------------------------------------------------------------- 거래대금 구간별 슬리피지
TIERS = [{"min_amount": 1e10, "bps": 10}, {"min_amount": 0, "bps": 40}, {"min_amount": 2e9, "bps": 20}]


def test_slip_for_tiers_and_multiplier():
    c = bt.Costs(commission_bps=0, slippage_bps=10, slippage_tiers=TIERS)
    assert [t["min_amount"] for t in c.slippage_tiers] == [0, 2e9, 1e10]     # 정렬
    assert c.slip_for(5e8) == pytest.approx(0.004)
    assert c.slip_for(2e9) == pytest.approx(0.002)
    assert c.slip_for(5e10) == pytest.approx(0.001)
    assert c.slip_for(float("nan")) == pytest.approx(0.004) and c.slip_for(None) == pytest.approx(0.004)
    assert bt.Costs(slippage_tiers=TIERS, multiplier=2).slip_for(5e8) == pytest.approx(0.008)
    assert bt.Costs(slippage_bps=10).slip_for(5e8) == pytest.approx(0.001)   # 구간 없으면 고정값
    with pytest.raises(ValueError):
        bt.Costs(slippage_tiers=[{"min_amount": 1e9, "bps": 10}])


def test_portfolio_applies_tier_by_lagged_avg_amount():
    # 두 종목, 같은 가격. A 는 소형(평균 5억), B 는 대형(평균 500억)
    T = 2
    sidx = {"A": 0, "B": 1}
    amount = np.array([[5e8, 5e10], [5e8, 5e10]], dtype=np.float32)
    pb = SimpleNamespace(sidx=sidx, open=np.full((T, 2), 1000, np.float32), close=np.full((T, 2), 1000, np.float32),
                         amount=amount, amount_avg20=bt.PriceBook._lagged_avg(amount, 20),
                         adj=np.ones((T, 2)), split=np.ones((T, 2)), div=np.zeros((T, 2)))
    assert np.isnan(pb.amount_avg20[0]).all() and pb.amount_avg20[1, 0] == pytest.approx(5e8)   # 당일 값 미포함
    pf = Portfolio(cash=1e8, costs=bt.Costs(commission_bps=0, slippage_tiers=TIERS),
                   cons=bt.Constraints(max_weight=1.0, max_participation=1.0))
    pf.buy(pb, 1, date(2020, 1, 3), "A", 10)
    pf.buy(pb, 1, date(2020, 1, 3), "B", 10)
    a, b = pf.trades
    assert a["price"] == pytest.approx(1000 * 1.004) and b["price"] == pytest.approx(1000 * 1.001)
    assert a["cost"] == pytest.approx(10 * 1000 * 0.004) and b["cost"] == pytest.approx(10 * 1000 * 0.001)
    # 이력이 없는 날(조회 첫날)은 가장 비싼 구간
    pf.buy(pb, 0, date(2020, 1, 2), "B", 1)
    assert pf.trades[-1]["price"] == pytest.approx(1000 * 1.004)
