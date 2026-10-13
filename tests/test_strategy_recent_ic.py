"""recent_rank_ic 재현 테스트.

버그: rank 과제 walk-forward run 에는 pred_rank 만 있고 pred_ret 은 전부 NULL 인데 pred_ret 을 읽어
IC 가 NaN → `ic < 0` 이 항상 거짓 → ml_confidence_cash 가 절대 현금으로 전환하지 않았다.
"""
import logging
from datetime import date

import numpy as np
import pandas as pd
import pytest

from marketlab.research import strategies as S
from marketlab.research.features import features_dir
from marketlab.research.store import research_db


def _setup(isolated, pred_rank: bool = True):
    rng = np.random.default_rng(1)
    days = pd.bdate_range("2020-01-02", "2020-08-31").date
    sids = [f"KR-T{i:03d}-20000101" for i in range(30)]
    rows = [(s, d, float(rng.normal())) for d in days for s in sids]
    lab = pd.DataFrame(rows, columns=["security_id", "trade_date", "y_ret_20"])
    p = features_dir("KR") / "year=2020" / "part-0.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    lab.to_parquet(p, index=False)
    pred = pd.DataFrame({
        "run_id": "wf-test-rank", "market": "KR", "horizon": 20, "algorithm": "lgbm",
        "security_id": lab["security_id"], "trade_date": lab["trade_date"],
        "pred_ret": np.nan,                                       # rank 과제: pred_ret 은 비어 있음
        "pred_up": np.nan,
        "pred_rank": -lab["y_ret_20"] if pred_rank else np.nan,     # 실제와 정반대 순위 → IC = -1
        "pred_vol": np.nan, "model_trained_until": date(2019, 12, 31), "kind": "oos_expanding"})
    with research_db() as db:
        db.register("p", pred)
        db.execute("INSERT INTO predictions SELECT * FROM p")
    return list(days)


def test_recent_rank_ic_reads_pred_rank_and_turns_cash_on(isolated):
    days = _setup(isolated)
    ic = S.recent_rank_ic("KR", "wf-test-rank", 20, days[-10:])
    assert ic, "IC 를 계산하지 못함 (pred_rank 를 읽지 않음)"
    assert all(np.isfinite(v) for v in ic.values())
    assert all(v < -0.9 for v in ic.values())       # 정반대 예측 → 음수 → 현금 전환 조건 충족
    # 이미 결과가 확정된 과거 예측만 사용: 마지막 사용 날짜 + horizon < d
    d = days[-1]
    assert d in ic


def test_recent_rank_ic_warns_when_all_predictions_nan(isolated, caplog):
    days = _setup(isolated, pred_rank=False)
    with caplog.at_level(logging.WARNING, logger="marketlab"):
        ic = S.recent_rank_ic("KR", "wf-test-rank", 20, days[-10:])
    assert ic == {}
    assert any("예측값이 모두 비어" in r.getMessage() for r in caplog.records)
