"""모델 갱신·모니터링.

- record_live: 최신 데이터로 예측을 실행하고 predictions(kind='live')에 저장 → 나중에 실제 결과와 비교.
- realized_vs_predicted: 과거 live/OOS 예측과 실현 수익률 비교, 월별 Rank IC 추이.
- drift: 학습 구간 대비 최근 60거래일 특징 분포 변화(PSI). 0.25 초과 = 큰 변화.
- check: 최근 성과 약화·분포 변화·확률 보정 악화 시 경고(pending_alerts 와 결과 dict).
- promote: 새 모델은 'candidate' 로만 등록되며, 사용자가 명시적으로 승인해야 'approved' 가 된다(자동 교체 금지).
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..storage.catalog import memory_connection
from ..storage.diskguard import raise_alert
from .analysis import pick_model, predict_latest
from .features import FEATURES, feature_glob
from .store import jdump, research_db


def record_live(market: str, horizons=(5, 20, 60)) -> dict:
    df, info = predict_latest(market, horizons)
    if df.empty:
        return {"rows": 0}
    rows = []
    for k in horizons:
        m = info.get(f"rank_{k}") or info.get(f"return_{k}")
        if not m:
            continue
        rows.append(pd.DataFrame({
            "run_id": f"live-{m['model_id']}", "market": market, "horizon": k, "algorithm": m["algorithm"],
            "security_id": df["security_id"], "trade_date": df["trade_date"],
            "pred_ret": df.get(f"pred_ret_{k}"), "pred_up": df.get(f"pred_up_{k}"), "pred_rank": df.get(f"pred_rank_{k}"),
            "pred_vol": df.get(f"pred_vol_{k}"), "model_trained_until": m["trained_until"], "kind": "live"}))
    if not rows:
        return {"rows": 0}
    out = pd.concat(rows)
    with research_db() as db:
        d = out["trade_date"].iloc[0]
        db.execute("DELETE FROM predictions WHERE kind='live' AND market=? AND trade_date=?", [market, d])
        db.register("o", out)
        db.execute("INSERT INTO predictions SELECT * FROM o")
    return {"rows": len(out), "date": str(out["trade_date"].iloc[0])}


def realized_vs_predicted(market: str, horizon: int = 20, kind_like: str = "live%") -> pd.DataFrame:
    with research_db(read_only=True) as db:
        runs = db.execute("""SELECT run_id FROM predictions WHERE market=? AND horizon=? AND kind LIKE ? AND algorithm='lgbm'
                             AND (pred_rank IS NOT NULL OR pred_ret IS NOT NULL) GROUP BY 1 ORDER BY (run_id LIKE '%rank%') DESC, max(trade_date) DESC LIMIT 1""",
                          [market, horizon, kind_like]).fetchall()
        if not runs:
            return pd.DataFrame()
        p = db.execute("""SELECT security_id, trade_date, coalesce(pred_rank, pred_ret) AS pred FROM predictions
                          WHERE run_id=? AND algorithm='lgbm' AND horizon=?""", [runs[0][0], horizon]).df()
    if p.empty:
        return pd.DataFrame()
    con = memory_connection()
    try:
        con.register("p", p)
        df = con.execute(f"""SELECT p.*, f.y_ret_{horizon} AS y FROM p JOIN read_parquet('{feature_glob(market)}') f
                             USING (security_id, trade_date) WHERE f.y_ret_{horizon} IS NOT NULL""").df()
    finally:
        con.close()
    if df.empty:
        return df
    df["month"] = pd.to_datetime(df["trade_date"]).dt.to_period("M").astype(str)
    return df.groupby("month").apply(lambda g: pd.Series({"rank_ic": g.groupby("trade_date")[["pred", "y"]].apply(
        lambda x: x["pred"].corr(x["y"], method="spearman")).mean(), "n": len(g)})).reset_index()


def psi(a: np.ndarray, b: np.ndarray, bins: int = 10) -> float:
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    if len(a) < 100 or len(b) < 100:
        return float("nan")
    edges = np.unique(np.quantile(a, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return float("nan")
    pa = np.histogram(np.clip(a, edges[0], edges[-1]), edges)[0] / len(a) + 1e-6
    pb = np.histogram(np.clip(b, edges[0], edges[-1]), edges)[0] / len(b) + 1e-6
    return float(np.sum((pb - pa) * np.log(pb / pa)))


def drift(market: str, train_start="2010-01-01", train_end="2020-12-31", recent_days: int = 60) -> pd.DataFrame:
    con = memory_connection()
    try:
        last = con.execute(f"SELECT max(trade_date) FROM read_parquet('{feature_glob(market)}')").fetchone()[0]
        cols = ", ".join(FEATURES)
        tr = con.execute(f"""SELECT {cols} FROM read_parquet('{feature_glob(market)}') WHERE trade_date BETWEEN ? AND ?
                             USING SAMPLE 200000 ROWS""", [train_start, train_end]).df()
        rc = con.execute(f"SELECT {cols} FROM read_parquet('{feature_glob(market)}') WHERE trade_date > ?",
                         [last - timedelta(days=int(recent_days * 1.5))]).df()
    finally:
        con.close()
    env_cols = [c for c in FEATURES if c.startswith(("mkt_", "vix", "fx_", "us10y", "t10y2y"))]
    rows = []
    for c in FEATURES:
        a, b = tr[c].to_numpy(float), rc[c].to_numpy(float)
        if c in env_cols:
            # 시장 환경 특징은 날짜당 값이 하나라 PSI 가 무의미 → 현재 값이 학습 기간 분포의 몇 분위인지
            cur = np.nanmedian(b[-1000:]) if len(b) else np.nan
            pctl = float(np.mean(a[~np.isnan(a)] <= cur)) if np.isfinite(cur) else np.nan
            rows.append({"feature": c, "kind": "market", "psi": np.nan, "current_percentile": pctl})
        else:
            rows.append({"feature": c, "kind": "stock", "psi": psi(a, b), "current_percentile": np.nan})
    return pd.DataFrame(rows).sort_values("psi", ascending=False, na_position="last")


def check(market: str, horizon: int = 20) -> dict:
    warnings = []
    m = pick_model(market, horizon, "rank")
    rv = realized_vs_predicted(market, horizon, "live%")
    if rv.empty:
        rv = realized_vs_predicted(market, horizon, "oos_expanding")
        src = "walk-forward OOS"
    else:
        src = "live"
    recent = rv.tail(6) if not rv.empty else rv
    if not recent.empty and recent["rank_ic"].mean() < 0:
        warnings.append(f"최근 6개월 예측 순위상관 평균 {recent['rank_ic'].mean():.3f} < 0 — 모델 성과 약화 ({src})")
    dr = drift(market)
    imp = []
    if m:
        from .models import Model
        mod = Model.load(m["path"])
        im = mod.importance()
        imp = list(im.head(10).index) if im is not None else []
    big = dr[(dr["kind"] == "stock") & (dr["psi"] > 0.25) & dr["feature"].isin(imp or FEATURES)]
    if len(big):
        warnings.append("주요 종목 특징 분포가 학습 기간과 크게 달라짐(PSI>0.25): " + ", ".join(big["feature"].head(5)))
    ext = dr[(dr["kind"] == "market") & ((dr["current_percentile"] < 0.01) | (dr["current_percentile"] > 0.99))]
    if len(ext):
        warnings.append("현재 시장 환경이 학습 기간에 거의 없던 수준(1% 미만/99% 초과 분위): " + ", ".join(ext["feature"].head(5)))
    for w in warnings:
        raise_alert("warn", "model", f"[{market} {horizon}일 모델] {w}")
    return {"model": m, "recent_rank_ic": recent.to_dict("records") if not recent.empty else [], "source": src,
            "drift_top": dr.head(15).to_dict("records"), "warnings": warnings}


def promote(model_id: str, note: str = "user approved") -> dict:
    """사용자 승인으로만 approved 전환. 같은 시장·기간·과제의 기존 approved 는 retired 로."""
    with research_db() as db:
        r = db.execute("SELECT market, horizon, note FROM model_registry WHERE model_id=?", [model_id]).fetchone()
        if not r:
            raise KeyError(model_id)
        task_tag = str(r[2]).split(";")[0]
        db.execute("UPDATE model_registry SET status='retired' WHERE market=? AND horizon=? AND note LIKE ? AND status='approved'",
                   [r[0], r[1], task_tag + "%"])
        db.execute("UPDATE model_registry SET status='approved', note = note || ? WHERE model_id=?", [f"; {note}", model_id])
    return {"approved": model_id}
