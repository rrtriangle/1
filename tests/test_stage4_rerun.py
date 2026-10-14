"""재실행 준비: 연구 결과 버전 무효화, 재검증 표시, 전향 검증, 점검 요약, 기본 슬리피지 구간."""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from marketlab.research import runner as R
from marketlab.research.store import research_db


# ---------------------------------------------------------------- 버전 무효화
def test_is_stale_on_version_change():
    cur = R.versions()
    assert not R.is_stale({"steps": {}})                                    # 처음 실행
    assert not R.is_stale({"steps": {"features": {}}, **cur})
    assert R.is_stale({"steps": {"features": {}}, "feature_version": cur["feature_version"]})   # 2단계 이전 상태(연구 버전 없음)
    assert R.is_stale({"steps": {"features": {}}, **cur, "research_version": cur["research_version"] - 1})


def test_wf_run_id_carries_versions():
    rid = R.wf_run_id("KR", "rank", 20, "expanding")
    v = R.versions()
    assert rid == f"wf-KR-rank-20-expanding-f{v['feature_version']}r{v['research_version']}"
    assert rid != "wf-KR-rank-20-expanding"   # 예전 이름과 달라야 예전 fold 결과를 재사용하지 않는다


def test_invalidate_archives_state_and_retires_models(isolated):
    old = {"steps": {"fixed_rank_20": {"done": True}}, "feature_version": R.F.FEATURE_VERSION, "research_version": 2,
           "wf_runs": {"wf_rank_20_expanding": "wf-KR-rank-20-expanding"}}
    R.save_state("KR", old)
    rep = isolated.paths.reports / "research_report_KR.md"
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text("old")
    with research_db() as db:
        db.execute("INSERT INTO model_registry VALUES ('m1', now(), 'KR', 20, 'lgbm', DATE '2020-01-01', 'e1', 'p', 'candidate', '{}', 'x')")
    new = R.invalidate_old_results("KR", R.load_state("KR"))
    assert new["steps"] == {} and "wf_runs" not in new
    tag = f"featv{R.F.FEATURE_VERSION}-resv2"
    assert json.loads((R._state_path("KR").with_name(f"research_KR.{tag}.json")).read_text())["research_version"] == 2
    assert rep.with_name(f"research_report_KR.{tag}.md").exists() and not rep.exists()
    with research_db(read_only=True) as db:
        st, note = db.execute("SELECT status, note FROM model_registry WHERE model_id='m1'").fetchone()
    assert st == "retired" and "연구 로직 v2→v" in note


# ---------------------------------------------------------------- 재검증 표시
def test_prior_final_runs_counts_same_market_and_start(isolated):
    with research_db() as db:
        db.execute("INSERT INTO test_access_log VALUES (TIMESTAMP '2026-05-01 10:00:00', 'runner', 'final strategy test', 'KR:2024-01-01~2026-05-01')")
        db.execute("INSERT INTO test_access_log VALUES (now(), 'runner', 'final strategy test', 'US:2024-01-01~2026-05-01')")
        db.execute("INSERT INTO test_access_log VALUES (now(), 'walk_forward', 'walk-forward 예측', 'wf-KR')")
    n, first = R.prior_final_runs("KR", date(2024, 1, 1))
    assert n == 1 and first.startswith("2026-05-01")
    assert R.prior_final_runs("KR", date(2025, 1, 1))[0] == 0
    note = R._compromise_note({"prior_test_runs": n, "first_test_run_at": first})
    assert note and "재검증" in note[0] and "1번" in note[0]
    assert R._compromise_note({"prior_test_runs": 0}) == []


# ---------------------------------------------------------------- 전향 검증
def _live(isolated, days, sign=1.0, n=30):
    from marketlab.research.features import features_dir
    rng = np.random.default_rng(0)
    sids = [f"KR-T{i:03d}" for i in range(n)]
    lab = pd.DataFrame([(s, d, float(rng.normal())) for d in days for s in sids], columns=["security_id", "trade_date", "y_ret_20"])
    p = features_dir("KR") / f"year={days[0].year}" / "part-0.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    lab.to_parquet(p, index=False)
    half = len(days) // 2   # refit 로 run_id 가 바뀐 경우: 두 run 을 합쳐 써야 한다
    pred = pd.DataFrame({"run_id": np.where(lab["trade_date"].isin(days[:half]), "live-m1", "live-m2"), "market": "KR",
                         "horizon": 20, "algorithm": "lgbm", "security_id": lab["security_id"], "trade_date": lab["trade_date"],
                         "pred_ret": np.nan, "pred_up": np.nan, "pred_rank": sign * lab["y_ret_20"] + rng.normal(scale=2, size=len(lab)),
                         "pred_vol": np.nan, "model_trained_until": date(2025, 12, 31), "kind": "live"})
    with research_db() as db:
        db.register("p", pred)
        db.execute("INSERT INTO predictions SELECT * FROM p")


def test_forward_ic_combines_refit_runs_and_respects_start(isolated):
    from marketlab.research.monitor import forward_ic
    days = list(pd.bdate_range("2026-01-05", periods=100).date)
    _live(isolated, days)
    f = forward_ic("KR", days[0], 20)
    assert f["n_dates"] == 100 and f["enough"] and f["rank_ic"] > 0.1
    assert f["rank_ic_t_nw"] is not None and f["rank_ic_t_nw"] > 2
    assert sum(m["n"] for m in f["monthly"]) == 100
    g = forward_ic("KR", days[70], 20)
    assert g["n_dates"] == 30 and not g["enough"]
    assert forward_ic("KR", date(2027, 1, 1), 20)["n_dates"] == 0


def test_forward_section_text(isolated):
    days = list(pd.bdate_range("2026-01-05", periods=100).date)
    _live(isolated, days, sign=-1.0)
    txt = "\n".join(R._forward_section("KR", {"forward_start": str(days[0])}))
    assert "예측력이 관찰되지 않습니다" in txt
    empty = "\n".join(R._forward_section("KR", {"forward_start": "2027-01-01"}))
    assert "아직 결과가 확정된 예측이 없습니다" in empty


def test_forward_start_config_override(isolated):
    assert R.forward_start("KR") is None
    isolated.raw.setdefault("research", {}).setdefault("splits", {})["forward_start_KR"] = "2026-11-01"
    assert R.forward_start("KR") == date(2026, 11, 1) and R.forward_start("US") is None


# ---------------------------------------------------------------- 점검 요약
def test_checklist_judges_numbers():
    st = {"steps": {
        "fixed_rank_20": {"done": True, "result": [
            {"algorithm": "lgbm", "metrics_valid": {"rank_ic_t": 4.0, "rank_ic_t_nw": 2.5}},
            {"algorithm": "ridge", "metrics_valid": {"rank_ic_t": 3.0, "rank_ic_t_nw": 1.2}}]},
        "strategy_search": {"done": True, "result": {"best": [{"strategy": "momentum", "dsr": 0.97}, {"strategy": "ml_top_ret", "dsr": 0.4}],
                                                     "deflated_sharpe": {"n_trials": 120}}},
        "final_test": {"done": True, "result": {"compromised": True, "strategies": [
            {"strategy": "ml_confidence_cash", "cash_signal": {"n_dates": 12, "n_cash": 3}, "test": {"sharpe": 0.5}},
            {"strategy": "ml_risk_adj", "test": {"sharpe": 0.9}}, {"strategy": "ml_top_ret", "test": {"sharpe": 0.6}}]}}}}
    txt = "\n".join(R._checklist(st))
    assert "2개 중 보정 후에도 t≥2 유지 1개" in txt and "rank_20/ridge(t 3.0→1.2)" in txt
    assert "누적 시도 120개" in txt and "1/2개 (momentum)" in txt
    assert "12번 중 현금 전환 3번" in txt and "작동했습니다" in txt
    assert "변동성 조정이 도움이 됐습니다" in txt
    assert "재검증" in txt
    assert R._checklist({"steps": {}}) and "비교할 모델 기록이 없습니다" in R._checklist({"steps": {}})[0]


# ---------------------------------------------------------------- 기본 슬리피지 구간
def test_default_slippage_tiers_loaded():
    from marketlab.research import backtest as bt
    kr, us = bt.Costs.for_market("KR"), bt.Costs.for_market("US")
    assert kr.slip_for(1e9) == pytest.approx(0.004) and kr.slip_for(5e9) == pytest.approx(0.002) and kr.slip_for(5e10) == pytest.approx(0.001)
    assert us.slip_for(1e6) == pytest.approx(0.0025) and us.slip_for(1e8) == pytest.approx(0.0005)
    assert kr.slip_for(5e10) == pytest.approx(kr.slip)   # 대형주는 기존 고정값과 같다
