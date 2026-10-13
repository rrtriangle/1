"""예측력(Rank IC) 점검: 규모·유동성 구간별 IC, 단일 특징 IC, 미래정보 누출 진단.

모두 walk-forward OOS 예측(각 예측은 그 날짜 이전 데이터로만 학습된 모델이 만든 값)을 사용한다.

누출 진단 항목
- 위약(placebo): 같은 날 안에서 예측을 무작위로 섞으면 IC ≈ 0 이어야 한다 (지표 계산 자체 점검).
- 시간 이동: 예측(t)을 과거 수익률(t−20→t)과 비교한 IC, 그리고 겹치지 않는 먼 미래(t+21→t+40) 수익률과 비교한 IC.
  라벨(t+1 시가→t+20 종가)에서만 유난히 높고 바로 다음 구간에서 0 으로 떨어지는 등 비정상 패턴을 본다.
- 체결 불가 진입 제외: t+1 시가가 상한가·하한가 부근인 행(실제 매수 어려움)을 빼고 재계산.
- 상장폐지 절단 라벨 제외: y_trunc=1 행을 빼고 재계산.
- 저가주 제외, 연도별 안정성.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from ..config import get_settings
from ..storage.catalog import memory_connection
from .features import FEATURES, feature_glob
from .store import research_db


def _ic_by_date(df: pd.DataFrame, pred: str, y: str, min_n: int = 10) -> pd.Series:
    def f(g):
        g = g[[pred, y]].dropna()
        if len(g) < min_n or g[pred].nunique() < 2:
            return np.nan
        return g[pred].corr(g[y], method="spearman")
    return df.groupby("trade_date")[[pred, y]].apply(f).dropna()


def _summ(s: pd.Series) -> dict:
    if s.empty:
        return {"rank_ic": None, "t": None, "pos": None, "n_dates": 0}
    t = s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if len(s) > 2 and s.std(ddof=1) > 0 else None
    return {"rank_ic": float(s.mean()), "t": float(t) if t is not None else None, "pos": float((s > 0).mean()), "n_dates": int(len(s))}


def load_panel(market: str, run_id: str, horizon: int = 20, every: int = 5) -> pd.DataFrame:
    """OOS 예측 + 라벨 + 진단용 컬럼 (메모리 절약: every 거래일마다)."""
    with research_db(read_only=True) as db:
        p = db.execute("""SELECT security_id, trade_date, coalesce(pred_rank, pred_ret) AS pred, model_trained_until
                          FROM predictions WHERE run_id=? AND algorithm='lgbm'""", [run_id]).df()
    if p.empty:
        raise RuntimeError(f"예측 없음: {run_id}")
    assert (pd.to_datetime(p["model_trained_until"]) < pd.to_datetime(p["trade_date"])).all()
    con = memory_connection()
    try:
        ds = sorted(p["trade_date"].unique())[::every]
        con.execute("CREATE TEMP TABLE dd AS SELECT unnest(?::DATE[]) AS trade_date", [ds])
        con.register("p", p)
        feats = ", ".join(f"f.{c}" for c in FEATURES)
        df = con.execute(f"""
            WITH f AS (SELECT * FROM read_parquet('{feature_glob(market)}') JOIN dd USING (trade_date))
            SELECT f.security_id, f.trade_date, p.pred, f.close, f.open_next, f.amount_20, f.market_cap,
                   f.y_ret_{horizon} AS y, f.y_rank_{horizon} AS y_rank, f.y_trunc_{horizon} AS y_trunc, {feats}
            FROM f JOIN p USING (security_id, trade_date)
            WHERE f.y_ret_{horizon} IS NOT NULL AND f.entry_ok
        """).df()
        # 시간 이동 진단용: 같은 종목의 20거래일 뒤 특징 파일의 ret_20 = (t+20 → t+40) 수익률 근사
        fut = con.execute(f"""
            SELECT security_id, trade_date,
                   lead(ret_20, 40) OVER (PARTITION BY security_id ORDER BY trade_date) AS ret_t20_t40
            FROM read_parquet('{feature_glob(market)}')
        """).df()
    finally:
        con.close()
    for x in (df, fut, p):
        x["trade_date"] = pd.to_datetime(x["trade_date"]).dt.date
    df = df.merge(fut, on=["security_id", "trade_date"], how="left")
    # 지연 진단: 같은 종목의 1·5거래일 전 예측을 오늘 라벨에 적용
    p = p.sort_values(["security_id", "trade_date"])
    for k in (1, 5):
        p[f"pred_lag{k}"] = p.groupby("security_id")["pred"].shift(k)
    df = df.merge(p[["security_id", "trade_date", "pred_lag1", "pred_lag5"]], on=["security_id", "trade_date"], how="left")
    return df


def audit(market: str, run_id: str, horizon: int = 20, every: int = 5, seed: int = 42) -> dict:
    df = load_panel(market, run_id, horizon, every)
    out: dict = {"market": market, "run_id": run_id, "horizon": horizon, "rows": len(df), "dates": int(df["trade_date"].nunique()),
                 "period": f"{df['trade_date'].min()}~{df['trade_date'].max()}"}
    out["overall"] = _summ(_ic_by_date(df, "pred", "y"))
    # 1) 규모·유동성·가격 구간별 (같은 날 5분위)
    buckets = {}
    for col, label in (("market_cap", "시가총액"), ("amount_20", "20일 평균 거래대금"), ("close", "주가")):
        if df[col].notna().mean() < 0.5:
            continue
        q = df.groupby("trade_date")[col].transform(lambda x: pd.qcut(x.rank(method="first"), 5, labels=False) + 1 if len(x) >= 25 else np.nan)
        rows = []
        for b in range(1, 6):
            sub = df[q == b]
            s = _summ(_ic_by_date(sub, "pred", "y", min_n=8))
            # 구간 안에서 예측 상위 20% − 하위 20% 실현 수익률 차이
            def spread(g):
                if len(g) < 10:
                    return np.nan
                r = g["pred"].rank(pct=True)
                return g.loc[r >= 0.8, "y"].mean() - g.loc[r <= 0.2, "y"].mean()
            sp = sub.groupby("trade_date")[["pred", "y"]].apply(spread).dropna()
            rows.append({"bucket": b, **s, "median_value": float(sub[col].median()), "rows": int(len(sub)),
                         "top_minus_bottom_20pct": float(sp.mean()) if len(sp) else None})
        buckets[label] = rows
    out["buckets"] = buckets
    # 2) 단일 특징 IC (부호 포함) — 단순 이상현상이 예측력의 얼마를 설명하는지
    uni = []
    for c in FEATURES:
        if df[c].notna().mean() < 0.5 or df[c].nunique() < 50:
            continue
        s = _summ(_ic_by_date(df, c, "y"))
        uni.append({"feature": c, **s})
    out["univariate_top"] = sorted(uni, key=lambda r: -abs(r["rank_ic"] or 0))[:12]
    # 3) 누출 진단
    rng = np.random.default_rng(seed)
    sh = df.copy()
    sh["pred"] = sh.groupby("trade_date")["pred"].transform(lambda x: rng.permutation(x.to_numpy()))
    diag = {"placebo_shuffled": _summ(_ic_by_date(sh, "pred", "y"))}
    diag["vs_past_ret_20"] = _summ(_ic_by_date(df, "pred", "ret_20"))
    diag["pred_lag1_day"] = _summ(_ic_by_date(df, "pred_lag1", "y"))
    diag["pred_lag5_days"] = _summ(_ic_by_date(df, "pred_lag5", "y"))
    diag["vs_future_t20_t40"] = _summ(_ic_by_date(df, "pred", "ret_t20_t40"))
    gap = df["open_next"] / df["close"] - 1
    limit = np.where(pd.to_datetime(df["trade_date"]) < pd.Timestamp("2015-06-15"), 0.145, 0.295) if market == "KR" else np.full(len(df), 9.0)
    near_limit = (gap.abs() >= limit * 0.97)
    diag["limit_open_share"] = float(near_limit.mean())
    diag["excl_limit_open"] = _summ(_ic_by_date(df[~near_limit], "pred", "y"))
    diag["truncated_label_share"] = float((df["y_trunc"] == 1).mean())
    diag["excl_truncated"] = _summ(_ic_by_date(df[df["y_trunc"] != 1], "pred", "y"))
    lowp = 2000 if market == "KR" else 5
    diag[f"excl_price_below_{lowp}"] = _summ(_ic_by_date(df[df["close"] >= lowp], "pred", "y"))
    out["diagnostics"] = diag
    yr = _ic_by_date(df, "pred", "y")
    out["by_year"] = {int(y): float(v.mean()) for y, v in yr.groupby(pd.to_datetime(yr.index).year)}
    return out


def write_report(res: dict) -> str:
    def f(x, nd=3):
        return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.{nd}f}"
    m = res["market"]
    L = [f"# Rank IC 점검 보고서 — {m} {res['horizon']}거래일", "",
         f"- 예측: walk-forward OOS (`{res['run_id']}`), 기간 {res['period']}, {res['dates']}개 날짜(5거래일 간격 표본), {res['rows']:,}행",
         f"- 전체 Rank IC {f(res['overall']['rank_ic'])} (t={f(res['overall']['t'], 1)}, 양(+) 날짜 비율 {f(res['overall']['pos'], 2)})", "",
         "## 1. 규모·유동성 구간별 (같은 날 투자 가능 종목을 5분위로 나눔, 1=가장 작음)", ""]
    for label, rows in res["buckets"].items():
        L += [f"### {label}", "", "| 구간 | 중앙값 | Rank IC | t | 양(+) 비율 | 상위20%−하위20% 수익 | 행 |", "|---|---:|---:|---:|---:|---:|---:|"]
        for r in rows:
            L.append(f"| {r['bucket']} | {r['median_value']:,.0f} | {f(r['rank_ic'])} | {f(r['t'], 1)} | {f(r['pos'], 2)} | "
                     f"{f(r['top_minus_bottom_20pct'] * 100 if r['top_minus_bottom_20pct'] is not None else None, 2)}% | {r['rows']:,} |")
        L.append("")
    L += ["## 2. 단일 특징 Rank IC (부호 포함, 절댓값 상위)", "", "| 특징 | Rank IC | t |", "|---|---:|---:|"]
    L += [f"| {r['feature']} | {f(r['rank_ic'])} | {f(r['t'], 1)} |" for r in res["univariate_top"]]
    d = res["diagnostics"]
    L += ["", "## 3. 미래정보 누출 진단", "",
          f"- 위약(같은 날 예측 무작위 섞기): Rank IC {f(d['placebo_shuffled']['rank_ic'])} → 0 근처면 지표 계산 정상",
          f"- 예측 vs 과거 20일 수익률(t−20→t): {f(d['vs_past_ret_20']['rank_ic'])}",
          f"- 1거래일 전 예측을 오늘 라벨에 적용: {f(d['pred_lag1_day']['rank_ic'])} / 5거래일 전: {f(d['pred_lag5_days']['rank_ic'])} "
          "(완만히 줄면 느린 특성 신호, 급락하면 당일 정보 의존 의심)",
          f"- 예측 vs 겹치지 않는 먼 미래(t+20→t+40): {f(d['vs_future_t20_t40']['rank_ic'])}",
          f"- t+1 시가 상·하한가 부근 진입 비중 {f(d['limit_open_share'] * 100, 2)}% → 제외 시 Rank IC {f(d['excl_limit_open']['rank_ic'])}",
          f"- 상장폐지로 잘린 라벨 비중 {f(d['truncated_label_share'] * 100, 2)}% → 제외 시 Rank IC {f(d['excl_truncated']['rank_ic'])}",
          *[f"- {k} → Rank IC {f(v['rank_ic'])}" for k, v in d.items() if k.startswith("excl_price")],
          "", "## 4. 연도별", "", " · ".join(f"{y}: {v:.3f}" for y, v in res["by_year"].items()), ""]
    p = get_settings().paths.reports / f"ic_audit_{m}.md"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return str(p)
