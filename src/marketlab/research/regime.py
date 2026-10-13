"""시장 국면 분석.

두 가지를 구분해서 제공한다.
1) 통계적 군집: 시장 수익률·변동성 특징을 학습 구간 데이터로만 적합한 가우시안 혼합모형(GMM)으로 묶은 결과.
   군집 번호 자체에는 의미가 없다.
2) 설명용 국면: 사람이 이해하기 쉬운 규칙으로 정한 이름 (상승 추세/하락 추세/횡보/변동성 확대/변동성 축소).
   규칙의 기준값(변동성 분위수)은 그 날짜까지의 과거 데이터만으로 계산한다(확장 분위수 → 미래정보 없음).
국면을 '좋은 시장/나쁜 시장'으로 단정하지 않는다. 국면별 이후 수익률은 과거 통계로만 보여준다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get_settings
from ..storage.catalog import memory_connection
from .features import build_market_env

REGIME_FEATURES = ["mkt_ret_20", "mkt_ret_60", "mkt_ma200_gap", "mkt_rv_20", "mkt_rv_chg"]
LABELS_KO = ["상승 추세", "하락 추세", "횡보", "변동성 확대", "변동성 축소"]


def market_env(market: str) -> pd.DataFrame:
    con = memory_connection()
    try:
        build_market_env(con, market)
        df = con.execute("SELECT * FROM env2 ORDER BY d").df()
    finally:
        con.close()
    df["d"] = pd.to_datetime(df["d"]).dt.date
    return df


def rule_regime(env: pd.DataFrame) -> pd.Series:
    rv = env["mkt_rv_20"]
    hi = rv.expanding(min_periods=250).quantile(0.8)
    lo = rv.expanding(min_periods=250).quantile(0.3)
    out = pd.Series("횡보", index=env.index)
    up = (env["mkt_ma200_gap"] > 0) & (env["mkt_ret_60"] > 0.05)
    down = (env["mkt_ma200_gap"] < 0) & (env["mkt_ret_60"] < -0.05)
    out[up] = "상승 추세"
    out[down] = "하락 추세"
    out[(rv < lo) & ~up & ~down] = "변동성 축소"
    out[(rv > hi) & (env["mkt_rv_chg"] > 0.2)] = "변동성 확대"
    out[rv.isna()] = None
    return out


def analyze(market: str, n_clusters: int = 5) -> dict:
    from sklearn.mixture import GaussianMixture
    from sklearn.preprocessing import StandardScaler

    s = get_settings()
    train_end = pd.to_datetime(s.get("research.splits.train_end", "2020-12-31")).date()
    env = market_env(market).dropna(subset=REGIME_FEATURES).reset_index(drop=True)
    env["regime_rule"] = rule_regime(env)
    tr = env[env["d"] <= train_end]
    sc = StandardScaler().fit(tr[REGIME_FEATURES])
    gm = GaussianMixture(n_components=n_clusters, covariance_type="full", random_state=int(s.get("research.seed", 42)))
    gm.fit(sc.transform(tr[REGIME_FEATURES]))
    env["cluster"] = gm.predict(sc.transform(env[REGIME_FEATURES]))
    # 이후 20거래일 시장 수익률 (서술 통계용 — 예측 특징으로 쓰지 않음)
    lr = np.log1p(env["mkt_ret_1"].fillna(0))
    env["fwd_ret_20"] = np.exp(lr[::-1].rolling(20, min_periods=20).sum()[::-1].shift(-1)) - 1
    prof = env.groupby("cluster").agg(n=("d", "size"), mkt_ret_60=("mkt_ret_60", "mean"), mkt_rv_20=("mkt_rv_20", "mean"),
                                      ma200_gap=("mkt_ma200_gap", "mean"), fwd_ret_20=("fwd_ret_20", "mean"),
                                      fwd_ret_20_std=("fwd_ret_20", "std")).reset_index()
    rule_stats = env.groupby("regime_rule").agg(n=("d", "size"), fwd_ret_20=("fwd_ret_20", "mean"),
                                                fwd_ret_20_pos=("fwd_ret_20", lambda x: (x > 0).mean())).reset_index()
    cross = pd.crosstab(env["cluster"], env["regime_rule"])
    last = env.iloc[-1]
    return {"env": env, "cluster_profile": prof, "rule_stats": rule_stats, "crosstab": cross,
            "current": {"date": last["d"], "regime_rule": last["regime_rule"], "cluster": int(last["cluster"]),
                        **{k: float(last[k]) for k in REGIME_FEATURES}},
            "fit_period": f"~{train_end}"}
