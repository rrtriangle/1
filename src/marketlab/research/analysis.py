"""최신 시장 투자 분석 + 한국어 설명 생성 (유료 LLM 없이 규칙 기반 템플릿).

원칙
- 분석 기준일(데이터 마지막 거래일)과 모델 학습 마지막 시점을 항상 함께 표시한다.
- 데이터가 오래되었으면 '최신 의견 아님'으로 표시한다.
- 검증 성적(검증 구간 Rank IC 의 t값)이 약하면 추천 강도를 낮추거나 '판단 보류'로 표시한다.
- 변수 중요도는 원인이 아니다. 과거 유사 국면의 결과는 미래를 보장하지 않는다.
"""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd

from ..config import get_settings
from ..storage.catalog import catalog, memory_connection
from .features import FEATURES, feature_glob
from .models import Model
from .store import research_db

TASK_OF = {"y_ret": "return", "y_rank": "rank", "y_up": "up", "y_vol": "risk"}


def latest_feature_date(market: str) -> date | None:
    con = memory_connection()
    try:
        return con.execute(f"SELECT max(trade_date) FROM read_parquet('{feature_glob(market)}')").fetchone()[0]
    finally:
        con.close()


def freshness(market: str) -> dict:
    d = latest_feature_date(market)
    with catalog(read_only=True) as cat:
        behind = cat.execute("SELECT count(*) FROM trading_calendars WHERE market=? AND trade_date > ? AND trade_date < current_date",
                             [market, d]).fetchone()[0] if d else None
    return {"data_date": d, "sessions_behind": behind, "is_fresh": behind is not None and behind <= 2}


def pick_model(market: str, horizon: int, task: str, include_refit: bool = True) -> dict | None:
    """우선순위: 승인(approved) → 최신 운영용 재학습(refit) → 검증 성적 최고 후보(candidate)."""
    prefix = {"return": "y_ret", "rank": "y_rank", "up": "y_up", "risk": "y_vol"}[task]
    with research_db(read_only=True) as db:
        rows = db.execute("""
            SELECT r.model_id, r.status, r.trained_until, r.path, r.valid_metrics, r.algorithm, e.target, e.kind
            FROM model_registry r JOIN experiments e ON e.exp_id = r.exp_id
            WHERE r.market=? AND r.horizon=? AND e.target = ? AND r.status IN ('approved', 'candidate')
              AND r.algorithm NOT IN ('zero', 'hist_mean', 'always_up', 'hist_vol')
        """, [market, horizon, f"{prefix}_{horizon}"]).fetchall()
    if not rows:
        return None
    if not include_refit:
        rows = [r for r in rows if r[7] != "refit"]
    if not rows:
        return None
    def score(r):
        m = json.loads(r[4])
        key = m.get("auc") if task == "up" else (m.get("rank_ic") if task in ("rank", "return") else -(m.get("rmse") or 9))
        return (r[1] == "approved", r[7] == "refit", str(r[2]) if r[7] == "refit" else "", key if key is not None else -9)
    best = max(rows, key=score)
    return {"model_id": best[0], "status": best[1], "trained_until": best[2], "path": best[3],
            "valid_metrics": json.loads(best[4]), "algorithm": best[5]}


def confidence(task: str, met: dict) -> tuple[str, str]:
    if task == "up":
        auc = met.get("auc") or 0.5
        if auc >= 0.56:
            return "보통", f"검증 AUC {auc:.3f}"
        if auc >= 0.53:
            return "낮음", f"검증 AUC {auc:.3f} (무작위 0.5 대비 약간 나음)"
        return "판단 보류", f"검증 AUC {auc:.3f} — 무작위와 큰 차이 없음"
    t = met.get("rank_ic_t") or 0
    ic = met.get("rank_ic") or 0
    if ic >= 0.05 and t >= 3:
        return "보통", f"검증 Rank IC {ic:.3f} (t={t:.1f})"
    if ic > 0.02 and t >= 2:
        return "낮음", f"검증 Rank IC {ic:.3f} (t={t:.1f})"
    return "판단 보류", f"검증 Rank IC {ic:.3f} (t={t:.1f}) — 예측력이 통계적으로 약함"


def latest_frame(market: str, d: date | None = None) -> pd.DataFrame:
    d = d or latest_feature_date(market)
    con = memory_connection()
    try:
        df = con.execute(f"SELECT * FROM read_parquet('{feature_glob(market)}') WHERE trade_date = ?", [d]).df()
    finally:
        con.close()
    with catalog(read_only=True) as cat:
        names = cat.execute("SELECT security_id, name, market AS exchange FROM securities WHERE country=?", [market]).df()
    return df.merge(names, on="security_id", how="left")


def predict_latest(market: str, horizons=(5, 20, 60), d: date | None = None) -> tuple[pd.DataFrame, dict]:
    df = latest_frame(market, d)
    info = {}
    if df.empty:
        return df, info
    for k in horizons:
        for task, col in (("rank", f"rank_{k}"), ("return", f"ret_{k}"), ("up", f"up_{k}"), ("risk", f"vol_{k}")):
            m = pick_model(market, k, task)
            if not m:
                continue
            model = Model.load(m["path"])
            p = model.predict(df)
            df[f"pred_{col}"] = p
            conf, why = confidence(task, m["valid_metrics"]) if task != "risk" else ("-", "")
            info[f"{task}_{k}"] = {**{x: m[x] for x in ("model_id", "status", "trained_until", "algorithm")},
                                   "confidence": conf, "why": why, "resid_q": m["valid_metrics"].get("resid_q")}
    return df, info


def market_summary(market: str) -> dict:
    from .regime import analyze
    fr = freshness(market)
    reg = analyze(market)
    env = reg["env"]
    last = env.iloc[-1]
    rv_pct = float((env["mkt_rv_20"] <= last["mkt_rv_20"]).mean())
    base = get_settings().paths.standardized / "market_indicators"
    idx = ["KRX:KS11", "KRX:KQ11", "MARCAP:KOSPI_CW_RETURN"] if market == "KR" else ["YAHOO:^GSPC", "YAHOO:^IXIC", "YAHOO:^VIX"]
    con = memory_connection()
    try:
        ind = con.execute(f"""SELECT indicator_id, obs_date, value FROM read_parquet('{(base / '*.parquet').as_posix()}', union_by_name=true)
                              WHERE indicator_id IN (SELECT unnest(?::VARCHAR[])) QUALIFY row_number() OVER (PARTITION BY indicator_id ORDER BY obs_date DESC) <= 61""", [idx]).df()
    finally:
        con.close()
    trends = {}
    for iid, g in ind.groupby("indicator_id"):
        g = g.sort_values("obs_date")
        if iid.startswith("MARCAP"):
            continue
        trends[iid] = {"last_date": g["obs_date"].iloc[-1], "value": float(g["value"].iloc[-1]),
                       "ret_20": float(g["value"].iloc[-1] / g["value"].iloc[-21] - 1) if len(g) > 21 else None,
                       "ret_60": float(g["value"].iloc[-1] / g["value"].iloc[0] - 1) if len(g) > 60 else None}
    return {"market": market, "freshness": fr, "regime": reg["current"], "rv_percentile": rv_pct,
            "adv_ratio_5": float(last.get("mkt_adv_ratio_5")) if pd.notna(last.get("mkt_adv_ratio_5")) else None,
            "vix": float(last["vix"]) if pd.notna(last.get("vix")) else None, "index_trends": trends,
            "regime_stats": reg["rule_stats"], "cluster_profile": reg["cluster_profile"], "env": env}


def describe_market(sm: dict) -> str:
    r = sm["regime"]
    lines = [f"**분석 기준일 {sm['freshness']['data_date']}**" + ("" if sm["freshness"]["is_fresh"] else
             f" — ⚠️ 데이터가 {sm['freshness']['sessions_behind']} 거래일 지연되어 최신 의견이 아닙니다.")]
    lines.append(f"현재 시장은 규칙 기준으로 **'{r['regime_rule']}'** 국면과 가장 비슷합니다 "
                 f"(최근 60거래일 시장 수익률 {r['mkt_ret_60']:+.1%}, 200일 추세 대비 {r['mkt_ma200_gap']:+.1%}).")
    lines.append(f"최근 20거래일 변동성은 연 {r['mkt_rv_20']:.0%}로, 과거 전체 기간 중 하위 {sm['rv_percentile']:.0%} 수준입니다.")
    if sm.get("adv_ratio_5") is not None:
        lines.append(f"최근 5일 평균 상승 종목 비율은 {sm['adv_ratio_5']:.0%} 입니다.")
    if sm.get("vix") is not None:
        lines.append(f"미국 VIX(공포지수)는 {sm['vix']:.1f} 입니다.")
    st = sm["regime_stats"]
    row = st[st["regime_rule"] == r["regime_rule"]]
    if not row.empty:
        lines.append(f"과거 같은 국면이었던 {int(row['n'].iloc[0]):,}거래일 이후 20거래일 시장 수익률은 평균 {row['fwd_ret_20'].iloc[0]:+.2%}, "
                     f"상승 비율 {row['fwd_ret_20_pos'].iloc[0]:.0%} 였습니다. 이는 과거 통계일 뿐 미래를 보장하지 않습니다.")
    return "\n\n".join(lines)


def stock_report(market: str, security_id: str) -> dict:
    df, info = predict_latest(market)
    row = df[df["security_id"] == security_id]
    if row.empty:
        return {"available": False, "reason": "최신 거래일 기준 투자 가능 종목(유동성·가격·이력 기준)이 아니어서 모델 예측 대상에서 제외됨"}
    r = row.iloc[0]
    out = {"available": True, "date": r["trade_date"], "name": r.get("name"), "features": {c: r[c] for c in FEATURES if c in r},
           "predictions": {}, "info": info}
    for k in (5, 20, 60):
        p = {}
        if f"pred_ret_{k}" in r:
            p["ret"] = float(r[f"pred_ret_{k}"])
            q = (info.get(f"return_{k}") or {}).get("resid_q")
            if q:
                p["ret_range_80"] = [p["ret"] + q[0], p["ret"] + q[2]]
        if f"pred_up_{k}" in r:
            p["up_prob"] = float(r[f"pred_up_{k}"])
        if f"pred_rank_{k}" in r:
            p["rank_pct"] = float((df[f"pred_rank_{k}"] <= r[f"pred_rank_{k}"]).mean())
        if f"pred_vol_{k}" in r:
            p["vol"] = float(r[f"pred_vol_{k}"])
        out["predictions"][k] = p
    # 개별 예측 근거 (LightGBM SHAP)
    m = pick_model(market, 20, "rank")
    if m and m["algorithm"] == "lgbm":
        model = Model.load(m["path"])
        c = model.contributions(row)
        if c is not None:
            s = c.iloc[0].sort_values()
            out["contrib_neg"] = s.head(5).to_dict()
            out["contrib_pos"] = s.tail(5)[::-1].to_dict()
    return out


FEATURE_KO = {
    "ret_5": "최근 5일 수익률", "ret_20": "최근 20일 수익률", "ret_60": "최근 60일 수익률", "ret_120": "최근 120일 수익률",
    "turnover_20": "회전율(거래대금/시가총액)", "rank_rv_20": "변동성 순위", "rv_20": "20일 변동성", "rv_60": "60일 변동성",
    "us10y": "미국 10년 금리", "t10y2y": "미국 장단기 금리차", "mkt_ma200_gap": "시장 200일 추세", "amount_chg_5_60": "거래대금 증가율",
    "days_since_high_252": "52주 고점 이후 경과일", "mkt_rv_20": "시장 변동성", "pos_high_252": "52주 고점 대비 위치",
    "rank_amount_20": "거래대금 순위", "mkt_ret_60": "시장 60일 수익률", "log_mcap": "시가총액(로그)", "rank_ret_20": "20일 수익률 순위",
    "fx_ret_20": "원/달러 20일 변화", "rsi_14": "RSI(14)", "ma_gap_20": "20일선 이격도", "ma_gap_60": "60일선 이격도",
    "vol_ratio_5_60": "거래량 증가율", "beta_120": "시장 베타", "amihud_20": "비유동성", "hl_range_20": "일중 변동폭",
    "vix": "VIX", "dd_60": "60일 고점 대비 낙폭", "jump_freq_60": "급등락 빈도", "xret_20": "시장 대비 20일 초과수익",
}


def ko(f: str) -> str:
    return FEATURE_KO.get(f, f)


def describe_stock(rep: dict) -> str:
    if not rep.get("available"):
        return f"예측 불가: {rep.get('reason')}"
    L = [f"**{rep.get('name')}** — 분석 기준일 {rep['date']}"]
    info = rep["info"]
    for k, p in rep["predictions"].items():
        seg = [f"**{k}거래일**"]
        if "rank_pct" in p:
            conf = (info.get(f"rank_{k}") or {}).get("confidence", "-")
            seg.append(f"예상 상대순위 상위 {1 - p['rank_pct']:.0%} (신뢰도: {conf})")
        if "ret" in p:
            rng = p.get("ret_range_80")
            seg.append(f"예상 수익률 {p['ret']:+.1%}" + (f" (80% 범위 {rng[0]:+.1%} ~ {rng[1]:+.1%})" if rng else ""))
        if "up_prob" in p:
            seg.append(f"상승 확률(보정) {p['up_prob']:.0%}")
        if "vol" in p:
            seg.append(f"예상 변동성 연 {p['vol']:.0%}")
        L.append(" · ".join(seg))
    if rep.get("contrib_pos"):
        L.append("예측을 높인 요인: " + ", ".join(ko(k) for k in rep["contrib_pos"]))
        L.append("예측을 낮춘 요인: " + ", ".join(ko(k) for k in rep["contrib_neg"]))
        L.append("※ 요인 기여도는 모델이 예측에 쓴 통계적 연관성이며 주가 변동의 원인이라는 뜻이 아닙니다.")
    tu = {v.get("trained_until") for v in info.values() if v.get("trained_until")}
    if tu:
        L.append(f"모델 학습 데이터 마지막 시점: {', '.join(str(x) for x in sorted(tu))} (분석 기준일과 다름)")
    return "\n\n".join(L)


def candidate_ranking(market: str, horizon: int = 20, min_mcap: float | None = None, max_vol: float | None = None,
                      exchanges: list[str] | None = None, top: int = 50) -> tuple[pd.DataFrame, dict]:
    df, info = predict_latest(market, (horizon,))
    if df.empty or f"pred_rank_{horizon}" not in df:
        return pd.DataFrame(), info
    if min_mcap and "market_cap" in df:
        df = df[df["market_cap"].fillna(0) >= min_mcap]
    if max_vol:
        df = df[df["rv_20"] <= max_vol]
    if exchanges:
        df = df[df["exchange"].isin(exchanges)]
    df = df.sort_values(f"pred_rank_{horizon}", ascending=False).head(top)
    conf = (info.get(f"rank_{horizon}") or {}).get("confidence", "판단 보류")
    df["추천강도"] = {"보통": "후보", "낮음": "약한 후보", "판단 보류": "판단 보류"}.get(conf, "판단 보류")
    cols = ["security_id", "name", "exchange", "close", "market_cap", "amount_20", "rv_20", "ret_20", f"pred_rank_{horizon}"] + \
           [c for c in (f"pred_ret_{horizon}", f"pred_up_{horizon}", f"pred_vol_{horizon}") if c in df] + ["추천강도"]
    return df[cols], info


def strategy_advice(market: str) -> pd.DataFrame:
    """선택 구간 백테스트 결과를 시장 국면별로 나눠 비교 (과거 통계)."""
    from .regime import market_env, rule_regime
    with research_db(read_only=True) as db:
        bts = db.execute("SELECT bt_id, strategy, params, equity_path, selection_period, note FROM backtests WHERE market=? AND note LIKE 'selection%'",
                         [market]).df()
    if bts.empty:
        return pd.DataFrame()
    env = market_env(market)
    env["regime"] = rule_regime(env)
    reg = env.set_index("d")["regime"]
    rows = []
    for b in bts.itertuples():
        eq = pd.read_parquet(b.equity_path)
        eq["date"] = pd.to_datetime(eq["date"]).dt.date
        r = eq.set_index("date")["equity"].pct_change().dropna()
        g = pd.DataFrame({"r": r, "regime": reg.reindex(r.index).to_numpy()}).dropna()
        for rg, x in g.groupby("regime"):
            rows.append({"strategy": b.strategy, "params": b.params, "regime": rg, "days": len(x),
                         "ann_return": float(x["r"].mean() * 252), "sharpe": float(x["r"].mean() / x["r"].std() * np.sqrt(252)) if x["r"].std() > 0 else None})
    return pd.DataFrame(rows)
