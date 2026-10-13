"""예측 성능 지표: 회귀(MAE, RMSE, 상관, IC, Rank IC), 분류(정확도, 정밀도, 재현율, AUC, Brier, 보정표)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def _per_date_ic(df: pd.DataFrame, pred: str, y: str, method: str) -> pd.Series:
    def f(g):
        if len(g) < 10 or g[pred].nunique() < 2:
            return np.nan
        return g[pred].corr(g[y], method=method)
    return df.groupby("trade_date")[[pred, y]].apply(f).dropna()


def regression_metrics(df: pd.DataFrame, pred: str = "pred", y: str = "y") -> dict:
    d = df[[pred, y, "trade_date"]].dropna()
    if d.empty:
        return {"n": 0}
    err = d[pred] - d[y]
    ic = _per_date_ic(d, pred, y, "pearson")
    ric = _per_date_ic(d, pred, y, "spearman")

    def tstat(s):
        return float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))) if len(s) > 2 and s.std(ddof=1) > 0 else None

    # 날짜별 예측 상위 10% − 하위 10% 실현 수익률 차이
    def spread(g):
        if len(g) < 20:
            return np.nan
        q = g[pred].rank(pct=True)
        return g.loc[q >= 0.9, y].mean() - g.loc[q <= 0.1, y].mean()
    sp = d.groupby("trade_date")[[pred, y]].apply(spread).dropna()
    return {
        "n": int(len(d)), "n_dates": int(d["trade_date"].nunique()),
        "mae": float(err.abs().mean()), "rmse": float(np.sqrt((err ** 2).mean())),
        "corr": float(d[pred].corr(d[y])) if d[pred].nunique() > 1 else None,
        "ic": float(ic.mean()) if len(ic) else None, "ic_t": tstat(ic),
        "rank_ic": float(ric.mean()) if len(ric) else None, "rank_ic_t": tstat(ric),
        "rank_ic_ir": float(ric.mean() / ric.std(ddof=1)) if len(ric) > 2 and ric.std(ddof=1) > 0 else None,
        "rank_ic_pos_ratio": float((ric > 0).mean()) if len(ric) else None,
        "top_bottom_spread": float(sp.mean()) if len(sp) else None,
        "direction_acc": float(((d[pred] > 0) == (d[y] > 0)).mean()),
    }


def classification_metrics(df: pd.DataFrame, pred: str = "pred", y: str = "y", bins: int = 10) -> dict:
    d = df[[pred, y]].dropna()
    if d.empty:
        return {"n": 0}
    p = d[pred].to_numpy(dtype=float)
    t = d[y].to_numpy(dtype=int)
    hat = (p >= 0.5).astype(int)
    tp = int(((hat == 1) & (t == 1)).sum())
    fp = int(((hat == 1) & (t == 0)).sum())
    fn = int(((hat == 0) & (t == 1)).sum())
    out = {"n": int(len(d)), "base_rate": float(t.mean()), "accuracy": float((hat == t).mean()),
           "precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None,
           "brier": float(np.mean((p - t) ** 2)),
           "brier_baseline": float(np.mean((t.mean() - t) ** 2))}
    try:
        from sklearn.metrics import roc_auc_score
        out["auc"] = float(roc_auc_score(t, p)) if len(np.unique(t)) == 2 and len(np.unique(p)) > 1 else None
    except Exception:
        out["auc"] = None
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    cal = []
    for b in range(bins):
        m = idx == b
        if m.sum() >= 30:
            cal.append({"bin": f"{edges[b]:.1f}-{edges[b + 1]:.1f}", "mean_pred": float(p[m].mean()),
                        "observed_up": float(t[m].mean()), "n": int(m.sum())})
    out["calibration"] = cal
    return out
