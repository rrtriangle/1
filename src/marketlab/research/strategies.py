"""투자 전략: 신호일 t 의 정보(특징 파일 = t 장 마감까지 관측)로 목표 비중을 만든다. 체결은 백테스트 엔진이 t+1 시가에.

규칙 기반: buy_hold, ma_cross, momentum, mean_reversion, rsi, vol_breakout, volume_spike, rel_strength
ML 기반(walk-forward OOS 예측 사용 — 각 예측은 그 날짜 이전 데이터로만 학습된 모델이 만든 값):
  ml_top_ret, ml_top_up, ml_risk_adj, ml_regime_switch, ml_confidence_cash, ml_plus_trend

전략 탐색: 파라미터 격자를 예산 내에서 선택 구간(≤ valid_end)으로만 비교하고, 모든 시도를 strategy_search_log 에 남긴다.
최종 검증 구간 성과는 선택 후에 한 번만 계산한다.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import date, datetime

import numpy as np
import pandas as pd

from ..config import get_settings
from ..storage.catalog import memory_connection
from . import backtest as bt
from .features import feature_glob
from .store import jdump, research_db

RULES = ["buy_hold", "ma_cross", "momentum", "mean_reversion", "rsi", "vol_breakout", "volume_spike", "rel_strength"]
ML = ["ml_top_ret", "ml_top_up", "ml_risk_adj", "ml_regime_switch", "ml_confidence_cash", "ml_plus_trend"]
DESCRIPTIONS = {
    "buy_hold": "시작일 시가총액 상위 N종목을 같은 비중으로 사서 끝까지 보유",
    "ma_cross": "20일 이동평균이 60일 이동평균 위인 종목 중 거래대금 상위 N종목",
    "momentum": "최근 120거래일 수익률 상위 N종목 (최근 5일 제외 효과 없음, 단순 모멘텀)",
    "mean_reversion": "장기 상승 추세(120일선 위)에서 최근 5일 낙폭이 큰 N종목",
    "rsi": "RSI(14) 30 미만 과매도 종목 중 거래대금 상위 N종목",
    "vol_breakout": "당일 수익률이 평소 변동폭의 1.5배를 넘고 거래량이 늘어난 종목",
    "volume_spike": "5일 평균 거래량이 60일 평균의 2배 이상이며 5일 수익률이 양수인 종목",
    "rel_strength": "60일 수익률의 시장 내 순위 상위 N종목",
    "ml_top_ret": "머신러닝 예상 수익률 상위 N종목",
    "ml_top_up": "머신러닝 상승 확률 상위 N종목",
    "ml_risk_adj": "예상 수익률 ÷ 예상 변동성 상위 N종목",
    "ml_regime_switch": "ML 상위 N종목, 단 하락 추세·변동성 확대 국면에서는 비중 50%",
    "ml_confidence_cash": "최근(이미 결과가 확인된) 예측 순위상관이 음수면 현금 보유",
    "ml_plus_trend": "20일선>60일선 종목 중 ML 예상 수익률 상위 N종목",
}


def rebalance_dates(market: str, start: date, end: date, every: int) -> list[date]:
    con = memory_connection()
    try:
        ds = [r[0] for r in con.execute(f"SELECT DISTINCT trade_date FROM read_parquet('{feature_glob(market)}') "
                                        f"WHERE trade_date BETWEEN ? AND ? ORDER BY 1", [start, end]).fetchall()]
    finally:
        con.close()
    return ds[::every]


def load_signal_frame(market: str, dates: list[date], cols: list[str]) -> pd.DataFrame:
    con = memory_connection()
    try:
        con.execute("CREATE TEMP TABLE dd AS SELECT unnest(?::DATE[]) AS trade_date", [dates])
        df = con.execute(f"""SELECT security_id, trade_date, {", ".join(sorted(set(cols)))} FROM read_parquet('{feature_glob(market)}')
                             JOIN dd USING (trade_date)""").df()
    finally:
        con.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def load_predictions(market: str, run_ids: dict[str, str], dates: list[date]) -> pd.DataFrame:
    """run_ids: {'ret': run_id, 'up': run_id, 'vol': run_id} → OOS 예측 (model_trained_until < trade_date 보장)."""
    parts = []
    with research_db(read_only=True) as db:
        for key, rid in run_ids.items():
            if not rid:
                continue
            col = {"ret": "coalesce(pred_ret, pred_rank)", "up": "pred_up", "vol": "pred_vol", "rank": "pred_rank"}[key]
            p = db.execute(f"""SELECT security_id, trade_date, algorithm, {col} AS {key}, model_trained_until FROM predictions
                               WHERE run_id = ? AND trade_date IN (SELECT unnest(?::DATE[]))""", [rid, dates]).df()
            if not p.empty:
                assert (pd.to_datetime(p["model_trained_until"]) < pd.to_datetime(p["trade_date"])).all(), "미래 학습 모델 예측 혼입"
                algo = "lgbm" if (p["algorithm"] == "lgbm").any() else p["algorithm"].iloc[0]
                parts.append(p[p["algorithm"] == algo][["security_id", "trade_date", key]].set_index(["security_id", "trade_date"]))
    if not parts:
        return pd.DataFrame(columns=["security_id", "trade_date"])
    out = pd.concat(parts, axis=1).reset_index()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
    return out


def _topn(g: pd.DataFrame, col: str, n: int, ascending=False) -> list[str]:
    g = g.dropna(subset=[col])
    return g.sort_values(col, ascending=ascending).head(n)["security_id"].tolist()


def generate_targets(market: str, strategy: str, start: date, end: date, params: dict,
                     pred_runs: dict[str, str] | None = None, regime_by_date: dict | None = None) -> dict[date, dict[str, float]]:
    n = int(params.get("n", 20))
    every = int(params.get("rebalance", 20))
    min_liq = float(params.get("min_liquidity_rank", 0.3))
    dates = rebalance_dates(market, start, end, every)
    cols = ["ret_1", "ret_5", "ret_60", "ret_120", "ma_20_60", "ma_gap_120", "rsi_14", "rv_20", "vol_ratio_5_60",
            "rank_ret_60", "rank_amount_20", "amount_20", "market_cap"]
    sig = load_signal_frame(market, dates, cols)
    sig = sig[sig["rank_amount_20"] >= min_liq]
    if strategy.startswith("ml_"):
        pr = load_predictions(market, pred_runs or {}, dates)
        sig = sig.merge(pr, on=["security_id", "trade_date"], how="inner")
    out: dict[date, dict[str, float]] = {}
    hold: list[str] | None = None
    recent_ic: dict[date, float] = params.get("_recent_ic", {})
    for d, g in sig.groupby("trade_date"):
        if strategy == "buy_hold":
            if hold is None:
                hold = _topn(g, "market_cap", n)
                out[d] = {s: 1 / len(hold) for s in hold} if hold else {}
            continue
        if strategy == "ma_cross":
            pick = _topn(g[g["ma_20_60"] > 0], "amount_20", n)
        elif strategy == "momentum":
            pick = _topn(g, "ret_120", n)
        elif strategy == "mean_reversion":
            pick = _topn(g[g["ma_gap_120"] > 0], "ret_5", n, ascending=True)
        elif strategy == "rsi":
            pick = _topn(g[g["rsi_14"] < float(params.get("rsi_level", 30))], "amount_20", n)
        elif strategy == "vol_breakout":
            th = float(params.get("k", 1.5)) * g["rv_20"] / np.sqrt(252)
            pick = _topn(g[(g["ret_1"] > th) & (g["vol_ratio_5_60"] > 1.2)], "amount_20", n)
        elif strategy == "volume_spike":
            pick = _topn(g[(g["vol_ratio_5_60"] > float(params.get("vol_mult", 2.0))) & (g["ret_5"] > 0)], "amount_20", n)
        elif strategy == "rel_strength":
            pick = _topn(g, "rank_ret_60", n)
        elif strategy == "ml_top_ret":
            pick = _topn(g, "ret_pred" if "ret_pred" in g else "ret", n)
        elif strategy == "ml_top_up":
            pick = _topn(g, "up", n)
        elif strategy == "ml_risk_adj":
            g = g.assign(ra=g["ret"] / g["vol"].clip(lower=0.05)) if "vol" in g else g.assign(ra=g["ret"] / g["rv_20"].clip(lower=0.05))
            pick = _topn(g, "ra", n)
        elif strategy in ("ml_regime_switch",):
            pick = _topn(g, "ret", n)
        elif strategy == "ml_confidence_cash":
            ic = recent_ic.get(d)
            pick = [] if (ic is not None and ic < 0) else _topn(g, "ret", n)
        elif strategy == "ml_plus_trend":
            pick = _topn(g[g["ma_20_60"] > 0], "ret", n)
        else:
            raise ValueError(strategy)
        scale = 1.0
        if strategy == "ml_regime_switch" and regime_by_date and regime_by_date.get(d) in ("하락 추세", "변동성 확대"):
            scale = 0.5
        out[d] = {s: scale / max(len(pick), n) for s in pick}
    return out


def recent_rank_ic(market: str, run_id: str, horizon: int, dates: list[date]) -> dict[date, float]:
    """각 날짜 d 에서 '이미 결과가 확정된' 과거 예측(신호일 + horizon 거래일 < d)의 최근 60일 순위상관 평균."""
    con = memory_connection()
    try:
        lab = con.execute(f"SELECT security_id, trade_date, y_ret_{horizon} AS y FROM read_parquet('{feature_glob(market)}') "
                          f"WHERE y_ret_{horizon} IS NOT NULL").df()
    finally:
        con.close()
    with research_db(read_only=True) as db:
        p = db.execute("SELECT security_id, trade_date, pred_ret FROM predictions WHERE run_id=? AND algorithm='lgbm'", [run_id]).df()
    if p.empty:
        return {}
    for x in (lab, p):
        x["trade_date"] = pd.to_datetime(x["trade_date"]).dt.date
    m = p.merge(lab, on=["security_id", "trade_date"])
    daily = m.groupby("trade_date")[["pred_ret", "y"]].apply(lambda g: g["pred_ret"].corr(g["y"], method="spearman")).sort_index()
    all_days = sorted(daily.index)
    out = {}
    for d in dates:
        known = [x for x in all_days if x < d][: max(0, len([x for x in all_days if x < d]) - (horizon + 1))]
        if len(known) >= 20:
            out[d] = float(daily.loc[known[-60:]].mean())
    return out


def search(market: str, strategies: list[str], grid: dict | None = None, budget: int = 40,
           pred_runs: dict | None = None, horizon: int = 20, regime_by_date: dict | None = None,
           metric: str = "sharpe", progress=None) -> dict:
    """선택 구간(학습 시작 ~ valid_end)에서만 전략·파라미터 비교. 최종 검증 구간은 사용하지 않는다."""
    from .experiments import periods
    P = periods(market)
    grid = grid or {"n": [10, 20], "rebalance": [5, 20]}
    combos = [dict(zip(grid, v)) for v in itertools.product(*grid.values())]
    trials = [(s, c) for s in strategies for c in (combos if s != "buy_hold" else [{"n": 20, "rebalance": 20}])]
    trials = trials[:budget]
    search_id = f"search-{market}-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4]}"
    sel_start = date(max(P["train_start"].year, 2003 if market == "KR" else P["train_start"].year), 1, 1)
    if any(s.startswith("ml_") for s in strategies):
        sel_start = max(sel_start, _first_pred_date(pred_runs))
    sel_end = P["valid_end"]
    rows = []
    costs = bt.Costs.for_market(market)
    for i, (s, c) in enumerate(trials):
        if progress:
            progress(i / len(trials), f"{s} {c}")
        params = dict(c)
        if s == "ml_confidence_cash" and pred_runs and pred_runs.get("ret"):
            params["_recent_ic"] = recent_rank_ic(market, pred_runs["ret"], horizon, rebalance_dates(market, sel_start, sel_end, int(c["rebalance"])))
        tg = generate_targets(market, s, sel_start, sel_end, params, pred_runs, regime_by_date)
        if not tg:
            continue
        res = bt.run(market, tg, sel_start, sel_end, costs=costs, name=s)
        m = res["metrics"]
        rows.append({"strategy": s, "params": {k: v for k, v in c.items()}, **{k: m.get(k) for k in ("cagr", "sharpe", "mdd", "n_trades", "turnover_per_year", "total_cost")}})
        with research_db() as db:
            db.execute("INSERT INTO strategy_search_log VALUES (?,?,?,?,?,?,?,?,?,?,FALSE, now())",
                       [search_id, s, market, jdump(c), f"{sel_start}~{sel_end}", metric, m.get(metric),
                        m.get("n_trades"), costs.slippage_bps + costs.commission_bps, jdump({"mdd": m.get("mdd"), "vol": m.get("volatility")})])
    df = pd.DataFrame(rows)
    if df.empty:
        return {"search_id": search_id, "trials": df}
    best = df.sort_values(metric, ascending=False).groupby("strategy").head(1)
    with research_db() as db:
        for r in best.itertuples():
            db.execute("UPDATE strategy_search_log SET selected = TRUE WHERE search_id=? AND strategy_id=? AND params=?",
                       [search_id, r.strategy, jdump(r.params)])
    return {"search_id": search_id, "trials": df, "best_per_strategy": best, "n_trials": len(df),
            "selection_period": f"{sel_start}~{sel_end}"}


def _first_pred_date(pred_runs: dict | None) -> date:
    if not pred_runs:
        return date(2000, 1, 1)
    with research_db(read_only=True) as db:
        r = db.execute("SELECT min(trade_date) FROM predictions WHERE run_id IN (SELECT unnest(?::VARCHAR[]))",
                       [[v for v in pred_runs.values() if v]]).fetchone()[0]
    return r or date(2000, 1, 1)
