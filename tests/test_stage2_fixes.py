"""2단계 수정 재현 테스트 (합성 데이터 — 실제 수집 데이터 불필요).

- portfolio: 비중 축소 매도가 거래대금 한도로 일부만 체결되면 다음 날 '남은 수량만' 재시도 (전량 매도 아님)
- backtest.is_terminated: 거래정지(시세 공백)는 기간과 무관하게 청산하지 않고, 상장폐지 확정 종목만 청산
- strategies.ml_risk_adj: rank 과제 순위 점수(0~1)를 그대로 변동성으로 나누지 않고 날짜별 표준화
- strategies.buy_hold: 시가총액이 전부 NULL 인 시장(US)은 거래대금 상위로 대체
- experiments.train_eval: up 과제에서 보정 모델과 기준 모델을 같은 뒤쪽 절반으로 평가
- experiments.refit_live: 옛 모델 보정기 재사용 대신 최근 구간을 떼어 새로 보정
- runner._interpret: 결론 문장이 실제 수치 비교로 선택됨, rank·(lgbm|ridge) 필터의 and/or 우선순위
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from marketlab.research import backtest as bt
from marketlab.research.portfolio import Portfolio

S1 = "KR-T001-20000101"


# ---------------------------------------------------------------- portfolio
def _book(T: int, opens, amounts, splits=None):
    """PriceBook 대용: 한 종목, T 일."""
    o = np.array(opens, dtype=np.float32).reshape(T, 1)
    return SimpleNamespace(
        sidx={S1: 0}, open=o, close=o.copy(), amount=np.array(amounts, dtype=np.float32).reshape(T, 1),
        adj=np.ones((T, 1)), split=np.array(splits if splits is not None else [1.0] * T, dtype=np.float64).reshape(T, 1),
        div=np.zeros((T, 1)))


def _pf(shares: float, cash: float = 0.0, max_participation: float = 0.1) -> Portfolio:
    pf = Portfolio(cash=cash, costs=bt.Costs(commission_bps=0, slippage_bps=0),
                   cons=bt.Constraints(max_weight=1.0, max_participation=max_participation))
    pf.shares[S1] = shares
    pf.cost_basis[S1] = shares * 100.0
    return pf


def test_partial_reduce_retries_only_remaining_quantity():
    # 1000주 × 100원 = 10만원, 목표 40% → 400주가 목표 → 600주 매도 필요.
    # 0일차 거래대금 20만원 × 10% = 2만원 → 200주만 체결. 1일차는 한도 넉넉.
    pb = _book(3, [100, 100, 100], [200_000, 1e9, 1e9])
    pf = _pf(1000)
    d0, d1 = date(2020, 1, 2), date(2020, 1, 3)
    pf.rebalance(pb, 0, d0, {S1: 0.4})
    assert pf.shares[S1] == 800
    assert pf.pending_sells == {S1: 400}            # 남은 축소 수량만 대기
    pf.rebalance(pb, 1, d1, None)                    # 다음 날: 미체결 재시도만
    assert pf.shares[S1] == 400, "비중 축소 잔량 대신 전량 매도함"
    assert pf.pending_sells == {}
    sells = [t for t in pf.trades if t["side"] == "SELL"]
    assert [t["shares"] for t in sells] == [200, 400]


def test_full_exit_partial_fill_still_sells_rest_next_day():
    pb = _book(2, [100, 100], [200_000, 1e9])
    pf = _pf(1000)
    pf.rebalance(pb, 0, date(2020, 1, 2), {})        # 목표에서 빠짐 → 전량 매도
    assert pf.shares[S1] == 800 and pf.pending_sells == {S1: 800}
    pf.rebalance(pb, 1, date(2020, 1, 3), None)
    assert S1 not in pf.shares


def test_suspended_sell_keeps_requested_quantity_and_split_scales_it():
    # 0일차 시가 없음(거래정지) → 축소 수량 그대로 대기, 1일차 2:1 분할 → 대기 수량도 2배
    pb = _book(3, [np.nan, 50, 50], [0, 1e9, 1e9], splits=[1.0, 2.0, 1.0])
    pf = _pf(1000)
    pf.sell(pb, 0, date(2020, 1, 2), S1, 300, "비중 축소")
    assert pf.pending_sells == {S1: 300}
    pf.corporate_actions(pb, 1, date(2020, 1, 3))
    assert pf.shares[S1] == 2000 and pf.pending_sells == {S1: 600}
    pf.rebalance(pb, 1, date(2020, 1, 3), None)
    assert pf.shares[S1] == 1400


def test_new_target_replaces_stale_pending():
    pb = _book(2, [100, 100], [200_000, 1e9])
    pf = _pf(1000)
    pf.rebalance(pb, 0, date(2020, 1, 2), {S1: 0.4})   # 200주 매도, 400주 대기
    pf.rebalance(pb, 1, date(2020, 1, 3), {S1: 0.8})   # 새 목표 80% (자산 10만 → 800주) → 이미 800주라 매도 없음
    assert pf.shares[S1] == 800


# ---------------------------------------------------------------- is_terminated
def _pricebook(status: str | None, last_idx: int | None, T: int = 100):
    pb = bt.PriceBook.__new__(bt.PriceBook)
    pb.dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(T)]
    pb.last_idx = {} if last_idx is None else {S1: last_idx}
    pb.meta = {S1: {"status": status}} if status else {}
    return pb


@pytest.mark.parametrize("status", ["listed", "unknown", None])
def test_long_suspension_is_not_delisting(status):
    pb = _pricebook(status, last_idx=10)
    assert not pb.is_terminated(S1, 50)     # 40일 공백
    assert not pb.is_terminated(S1, 99)


@pytest.mark.parametrize("status", ["delisted", "merged"])
def test_confirmed_delisting_is_terminated_after_last_trade(status):
    pb = _pricebook(status, last_idx=10)
    assert not pb.is_terminated(S1, 10)
    assert pb.is_terminated(S1, 11)
    assert _pricebook(status, last_idx=None).is_terminated(S1, 0)
    assert not _pricebook("unknown", last_idx=None).is_terminated(S1, 0)


# ---------------------------------------------------------------- ml_risk_adj / buy_hold
def test_risk_adjusted_standardizes_rank_scores():
    from marketlab.research.strategies import risk_adjusted, _topn
    # 순위 점수 0~1: 'low' 는 점수가 하위권(0.3)인데 변동성이 매우 낮다. 그대로 나누면 6.0 으로 1위가 되어버림.
    g = pd.DataFrame({"security_id": ["hi", "mid", "low", "bad"], "ret": [0.95, 0.7, 0.3, 0.05],
                      "rv_20": [0.6, 0.3, 0.05, 0.05], "ret_is_rank": True})
    assert _topn(g.assign(ra=g["ret"] / g["rv_20"]), "ra", 1) == ["low"]   # 옛 방식 재현
    r = risk_adjusted(g)
    assert "low" not in _topn(r, "ra", 2) and "bad" not in _topn(r, "ra", 4)
    assert set(_topn(r, "ra", 2)) == {"hi", "mid"}
    # return 과제 예측(실제 기대수익률)은 표준화하지 않고 그대로 나눈다
    g2 = pd.DataFrame({"security_id": ["a", "b"], "ret": [0.02, 0.01], "vol": [0.4, 0.1], "ret_is_rank": False})
    assert risk_adjusted(g2)["ra"].tolist() == pytest.approx([0.05, 0.1])


def test_load_predictions_flags_rank_runs(isolated):
    from marketlab.research import strategies as S
    from marketlab.research.store import research_db
    d = date(2020, 3, 2)
    pred = pd.DataFrame({"run_id": ["r-rank", "r-ret"], "market": "KR", "horizon": 20, "algorithm": "lgbm",
                         "security_id": [S1, S1], "trade_date": [d, d], "pred_ret": [np.nan, 0.03], "pred_up": np.nan,
                         "pred_rank": [0.8, np.nan], "pred_vol": np.nan, "model_trained_until": date(2019, 12, 31),
                         "kind": "oos_expanding"})
    with research_db() as db:
        db.register("p", pred)
        db.execute("INSERT INTO predictions SELECT * FROM p")
    a = S.load_predictions("KR", {"ret": "r-rank"}, [d])
    b = S.load_predictions("KR", {"ret": "r-ret"}, [d])
    assert bool(a["ret_is_rank"].iloc[0]) and a["ret"].iloc[0] == pytest.approx(0.8)
    assert not bool(b["ret_is_rank"].iloc[0]) and b["ret"].iloc[0] == pytest.approx(0.03)


def test_buy_hold_us_without_market_cap_uses_traded_value(isolated):
    from marketlab.research import strategies as S
    from marketlab.research.features import features_dir
    days = list(pd.bdate_range("2021-01-04", "2021-03-31").date)
    sids = [f"US-T{i:02d}" for i in range(30)]
    rows = [{"security_id": s, "trade_date": d, "amount_20": float(i + 1) * 1e6, "market_cap": None,
             "rank_amount_20": (i + 1) / 30} for d in days for i, s in enumerate(sids)]
    df = pd.DataFrame(rows)
    for c in ["ret_1", "ret_5", "ret_60", "ret_120", "ma_20_60", "ma_gap_120", "rsi_14", "rv_20", "vol_ratio_5_60", "rank_ret_60"]:
        df[c] = 0.0
    df["market_cap"] = df["market_cap"].astype(float)
    p = features_dir("US") / "year=2021" / "part-0.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, index=False)
    tg = S.generate_targets("US", "buy_hold", days[0], days[-1], {"n": 5, "rebalance": 20})
    assert len(tg) == 1
    (w,) = tg.values()
    assert set(w) == set(sids[-5:]), "거래대금 상위 5종목이어야 함"
    assert sum(w.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------- experiments (up 과제)
# synthetic_up 픽스처는 conftest.py
def test_train_eval_up_all_models_share_eval_period(synthetic_up):
    from marketlab.research import experiments as E
    res = E.train_eval("KR", 20, "up", ["always_up", "momentum", "logistic"], sample_every=5)
    n_valid = {r["algorithm"]: r["n_valid"] for r in res}
    assert len(set(n_valid.values())) == 1, f"보정 모델과 기준 모델의 평가 표본이 다름: {n_valid}"


def test_refit_live_recalibrates_on_recent_holdout(synthetic_up):
    from marketlab.research import experiments as E
    from marketlab.research.models import Model
    src = E.train_eval("KR", 20, "up", ["logistic"], sample_every=5)[0]
    rec = E.refit_live("KR", 20, "up", source_exp_id=src["exp_id"])
    cal = rec["calibration"]
    assert "cal_start" in cal, cal
    days = synthetic_up
    assert date.fromisoformat(cal["fit_end"]) < date.fromisoformat(cal["cal_start"])   # 보정 구간은 학습에서 제외(+purge)
    assert date.fromisoformat(cal["cal_end"]) <= date.fromisoformat(rec["trained_until"])
    assert date.fromisoformat(cal["cal_start"]) > days[-400]                              # 최근 구간
    s = E.get_settings()
    new = Model.load(s.paths.data / s.get("research.models_dir", "research_models") / f"{rec['exp_id']}.pkl")
    old = Model.load(s.paths.data / s.get("research.models_dir", "research_models") / f"{src['exp_id']}.pkl")
    assert new.calibrator is not None and new.calibrator is not old.calibrator
    grid = np.linspace(0, 1, 11)
    assert not np.allclose(new.calibrator.predict(grid), old.calibrator.predict(grid)), "옛 보정기를 그대로 재사용함"


# ---------------------------------------------------------------- runner._interpret
def _strategy(name, cagr, sharpe, bench_total, years=2.0, x0=None, x2=None, turnover=5.0, sel=0.6):
    t = {"cagr": cagr, "sharpe": sharpe, "mdd": -0.2, "years": years, "turnover_per_year": turnover}
    return {"strategy": name, "selection_sharpe": sel, "test": t, "bootstrap": {"sharpe_p05_p50_p95": [0.1, 0.5, 0.9]},
            "benchmark_return": bench_total,
            "cost_sensitivity": {"cost_x0": {"cagr": x0 if x0 is not None else cagr}, "cost_x2": {"cagr": x2 if x2 is not None else cagr}}}


def _steps(valid_ic: dict):
    return {"fixed_rank_20": {"done": True, "result": [{"exp_id": k, "algorithm": k.split("-")[3], "metrics_valid": {"rank_ic": v}}
                                                       for k, v in valid_ic.items()]}}


def test_interpret_sentences_follow_numbers():
    from marketlab.research.runner import _interpret
    # 전략 CAGR 20% vs 벤치마크 2년 총수익 10%(CAGR≈4.9%) → 이김. Rank IC 검증 0.05 → 검증 0.04 → 유지.
    ft = {"strategies": [_strategy("ml_top_ret", 0.20, 1.2, {"KS11": 0.10}, x2=0.19)],
          "models": [{"exp_id": "KR-rank-20-lgbm-1", "metrics_test": {"rank_ic": 0.04}},
                     # risk 과제 ridge 는 순위 모델이 아니다 — and/or 우선순위 버그 때 섞여 들어갔다
                     {"exp_id": "KR-risk-20-ridge-1", "metrics_test": {"rank_ic": -0.5}}]}
    txt = "\n".join(_interpret(ft, _steps({"KR-rank-20-lgbm-1": 0.05})))
    assert "모든 벤치마크" in txt and "이기지 못했" not in txt
    assert "0.040~0.040" in txt and "-0.500" not in txt
    assert "절반 이상이 유지" in txt
    assert "크게 흔들리지 않았습니다" in txt

    # 벤치마크에 지고, IC 가 사라지고, 비용 2배에 민감
    ft2 = {"strategies": [_strategy("ml_top_ret", 0.02, 0.3, {"KS11": 0.30}, x2=-0.03)],
           "models": [{"exp_id": "KR-rank-20-lgbm-1", "metrics_test": {"rank_ic": -0.01}}]}
    txt2 = "\n".join(_interpret(ft2, _steps({"KR-rank-20-lgbm-1": 0.05})))
    assert "어느 벤치마크" in txt2 and "이기지 못했" in txt2
    assert "예측력이 관찰되지 않았습니다" in txt2 and "비슷하게 유지" not in txt2
    assert "2%p 넘게 줄어드는 전략: ml_top_ret" in txt2

    # 높은 회전율이지만 비용 영향이 작으면 '대부분 상쇄' 라고 쓰지 않는다
    ft3 = {"strategies": [_strategy("momentum", 0.10, 0.8, {"KS11": 0.0}, x0=0.11, turnover=40)], "models": []}
    txt3 = "\n".join(_interpret(ft3, {}))
    assert "연 회전율 40배" in txt3 and "절반 이상을 상쇄" not in txt3


def test_same_day_unfilled_sells_accumulate():
    pb = _book(2, [100, 100], [100_000, 1e9])        # 0일차 한도 1만원 = 100주
    pf = _pf(1000)
    pf.sell(pb, 0, date(2020, 1, 2), S1, 300, "주문1")   # 100주 체결, 200주 대기
    pf.sell(pb, 0, date(2020, 1, 2), S1, 300, "주문2")   # 다시 100주 체결, 200주 대기 추가 (덮어쓰지 않음)
    assert pf.shares[S1] == 800 and pf.pending_sells == {S1: 400}
    pf.rebalance(pb, 1, date(2020, 1, 3), None)
    assert pf.shares[S1] == 400
