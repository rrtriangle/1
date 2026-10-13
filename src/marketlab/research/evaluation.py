"""예측 성능 지표: 회귀(MAE, RMSE, 상관, IC, Rank IC), 분류(정확도, 정밀도, 재현율, AUC, Brier, 보정표),
그리고 시계열 의존성·다중 시도를 반영한 보정 통계(Newey-West t값, deflated Sharpe)."""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

_N = NormalDist()
EULER_GAMMA = 0.5772156649015329


def nw_lag(horizon: int | None, sample_every: int | None) -> int:
    """라벨 기간이 겹치는 만큼의 Newey-West 라그 = ceil(horizon / 표본 간격). horizon 을 모르면 0(보정 없음)."""
    if not horizon or horizon <= 0:
        return 0
    return int(math.ceil(horizon / max(1, int(sample_every or 1))))


def newey_west_tstat(x, lag: int) -> float | None:
    """평균이 0 인지에 대한 t값 — Bartlett 가중 Newey-West 분산 (자기상관이 있는 날짜별 IC 용).
    lag=0 이면 보통 t값과 같다(분모 n 사용)."""
    a = np.asarray(x, dtype=float)
    a = a[np.isfinite(a)]
    n = len(a)
    if n <= 2:
        return None
    e = a - a.mean()
    lag = max(0, min(int(lag), n - 1))
    var = float(e @ e) / n
    for k in range(1, lag + 1):
        var += 2 * (1 - k / (lag + 1)) * float(e[k:] @ e[:-k]) / n
    if var <= 0:
        return None
    return float(a.mean() / math.sqrt(var / n))


def expected_max_sharpe(sr_trials, n_trials: int | None = None) -> float:
    """독립 시도 N 개의 샤프가 모두 0 이 참값일 때 기대되는 최대 샤프 (Bailey & López de Prado 2014).
    sr_trials: 각 시도의 (같은 단위) 샤프. 분산은 시도 간 분산을 쓴다."""
    sr = np.asarray([v for v in sr_trials if v is not None and np.isfinite(v)], dtype=float)
    n = int(n_trials or len(sr))
    if n <= 1 or len(sr) < 2:
        return 0.0
    sd = float(sr.std(ddof=1))
    return sd * ((1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n) + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n * math.e)))


def deflated_sharpe(sr: float, n_obs: int, skew: float, kurt: float, sr_trials, n_trials: int | None = None) -> float | None:
    """Deflated Sharpe Ratio: 시도 횟수만큼 '우연히 나올 최대 샤프'(SR0)를 넘었을 확률.
    sr 은 기간 단위(일간) 샤프, kurt 는 보통 첨도(정규분포 = 3). 0.95 이상이면 다중 시도를 감안해도 유의하다고 본다."""
    if sr is None or not np.isfinite(sr) or n_obs is None or n_obs < 3:
        return None
    sr0 = expected_max_sharpe(sr_trials, n_trials)
    skew = 0.0 if skew is None or not np.isfinite(skew) else float(skew)
    kurt = 3.0 if kurt is None or not np.isfinite(kurt) else float(kurt)
    den = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if den <= 0:
        return None
    return float(_N.cdf((sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(den)))


def return_moments(returns) -> dict:
    """일간 수익률 → 일간 샤프·왜도·첨도(보통)·관측 수. deflated_sharpe 입력용."""
    r = pd.Series(returns, dtype=float).dropna()
    if len(r) < 3 or r.std(ddof=1) == 0:
        return {"sr": None, "skew": None, "kurt": None, "n_obs": int(len(r))}
    return {"sr": float(r.mean() / r.std(ddof=1)), "skew": float(r.skew()), "kurt": float(r.kurt() + 3), "n_obs": int(len(r))}


def _per_date_ic(df: pd.DataFrame, pred: str, y: str, method: str) -> pd.Series:
    def f(g):
        if len(g) < 10 or g[pred].nunique() < 2:
            return np.nan
        return g[pred].corr(g[y], method=method)
    return df.groupby("trade_date")[[pred, y]].apply(f).dropna()


def regression_metrics(df: pd.DataFrame, pred: str = "pred", y: str = "y", horizon: int | None = None,
                       sample_every: int | None = None) -> dict:
    """horizon·sample_every 를 주면 날짜별 IC 의 t값에 Newey-West 보정(라그 = ceil(horizon/표본간격))을 추가한다.
    라벨 기간(t+1~t+horizon)이 겹치는 날짜들의 IC 는 서로 상관돼 있어 보통 t값은 유의성을 부풀린다."""
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
    lag = nw_lag(horizon, sample_every)
    return {
        "n": int(len(d)), "n_dates": int(d["trade_date"].nunique()),
        "mae": float(err.abs().mean()), "rmse": float(np.sqrt((err ** 2).mean())),
        "corr": float(d[pred].corr(d[y])) if d[pred].nunique() > 1 else None,
        "ic": float(ic.mean()) if len(ic) else None, "ic_t": tstat(ic),
        "rank_ic": float(ric.mean()) if len(ric) else None, "rank_ic_t": tstat(ric),
        "ic_t_nw": newey_west_tstat(ic, lag) if len(ic) else None,
        "rank_ic_t_nw": newey_west_tstat(ric, lag) if len(ric) else None, "nw_lag": lag,
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
