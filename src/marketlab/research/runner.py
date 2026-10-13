"""연구 전체 실행 (재개 가능): 특징 → 고정분할 학습 → 국면 → walk-forward → 전략 탐색(선택구간) → 최종 검증 1회 → 보고서.

각 단계 완료 여부를 data/metadata/state/research_<market>.json 에 기록한다. 중단 후 다시 실행하면 완료된 단계는 건너뛴다.
최종 검증(test) 단계는 한 번만 실행된다. 다시 실행하려면 force_test=True 가 필요하며, 이 경우 보고서에 '독립성 훼손'이 표시된다.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime

import numpy as np
import pandas as pd

from ..config import get_settings
from ..logging_setup import get_logger, log_event
from . import backtest as bt
from . import experiments as E
from . import features as F
from . import strategies as S
from .regime import analyze as regime_analyze
from .regime import market_env, rule_regime
from .resources import rss_gb
from .store import jdump, research_db

log = get_logger("research_runner")

FIXED = [("rank", h, None) for h in (5, 20, 60)] + [("return", h, None) for h in (5, 20, 60)] + \
        [("up", h, None) for h in (5, 20, 60)] + [("risk", 20, None)]
WF = [("rank", 20, ["ridge", "lgbm"], "expanding"), ("rank", 20, ["lgbm"], "rolling"),
      ("up", 20, ["lgbm"], "expanding"), ("risk", 20, ["lgbm"], "expanding"),
      ("rank", 5, ["lgbm"], "expanding"), ("rank", 60, ["lgbm"], "expanding")]


def _state_path(market: str):
    p = get_settings().paths.state_dir / f"research_{market}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_state(market: str) -> dict:
    p = _state_path(market)
    return json.loads(p.read_text()) if p.exists() else {"steps": {}}


def save_state(market: str, st: dict) -> None:
    _state_path(market).write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str))


def run_all(market: str, force_test: bool = False, progress=None) -> dict:
    st = load_state(market)
    if st.get("steps") and st.get("feature_version", 1) != F.FEATURE_VERSION:
        st = invalidate_old_results(market, st)
    st["feature_version"] = F.FEATURE_VERSION
    steps = st["steps"]
    t_all = time.time()
    preflight(market)

    def step(name, fn):
        if name in steps and steps[name].get("done"):
            return steps[name].get("result")
        if progress:
            progress(name)
        t0 = time.time()
        res = fn()
        steps[name] = {"done": True, "result": res, "sec": round(time.time() - t0, 1), "rss_gb": round(rss_gb(), 2),
                       "at": datetime.now().isoformat(timespec="seconds")}
        save_state(market, st)
        log_event(log, "research step", market=market, step=name, sec=steps[name]["sec"])
        return res

    def _features():
        stale = F.stale_years(market)
        if not stale and any(F.features_dir(market).glob("year=*/*.parquet")):
            return "up-to-date"
        F.build(market, stale or None)
        return f"built {stale or 'all'}"
    step("features", _features)
    for task, h, algos in FIXED:
        step(f"fixed_{task}_{h}", lambda task=task, h=h, algos=algos: [
            {k: r[k] for k in ("exp_id", "algorithm", "metrics_valid", "n_train", "n_valid", "train_seconds", "peak_rss_gb")}
            for r in E.train_eval(market, h, task, algos)])
    step("regime", lambda: _regime(market))
    wf_runs = st.setdefault("wf_runs", {})
    for task, h, algos, window in WF:
        key = f"wf_{task}_{h}_{window}"
        rid = wf_runs.get(key) or f"wf-{market}-{task}-{h}-{window}"
        wf_runs[key] = rid
        save_state(market, st)
        step(key, lambda task=task, h=h, algos=algos, window=window, rid=rid:
             E.walk_forward(market, h, task, algos, window=window, run_id=rid)["run_id"])
    # walk-forward 는 기본으로 valid_end 연도까지만 예측한다. 전략이 쓰는 run 의 최종 검증 구간 예측은 _final 에서 이어서 계산.
    pred_runs = {"ret": wf_runs.get("wf_rank_20_expanding"), "up": wf_runs.get("wf_up_20_expanding"),
                 "vol": wf_runs.get("wf_risk_20_expanding")}
    wf_spec = {wf_runs[f"wf_{t}_{h}_{w}"]: (t, h, a, w) for t, h, a, w in WF if f"wf_{t}_{h}_{w}" in wf_runs}
    step("strategy_search", lambda: _search(market, pred_runs))
    if force_test or not steps.get("final_test", {}).get("done"):
        steps.pop("final_test", None)
        step("final_test", lambda: _final(market, st, pred_runs, force_test, wf_spec))
    rep = write_report(market, st)
    st["report"] = str(rep)
    st["total_sec"] = round(time.time() - t_all, 1)
    save_state(market, st)
    return st


def invalidate_old_results(market: str, st: dict) -> dict:
    """특징 정의가 바뀌면 이전 연구 결과는 재사용하지 않는다: 상태 파일 보관, 구버전 모델 retired 처리."""
    old_v = st.get("feature_version", 1)
    p = _state_path(market)
    p.with_name(f"research_{market}.featv{old_v}.json").write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str))
    rep = get_settings().paths.reports / f"research_report_{market}.md"
    if rep.exists():
        rep.rename(rep.with_name(f"research_report_{market}.featv{old_v}.md"))
    with research_db() as db:
        db.execute("UPDATE model_registry SET status='retired', note = note || ? WHERE market=? AND status IN ('candidate','approved')",
                   [f"; 구버전 특징(v{old_v})으로 학습 — 특징 버그 수정 후 사용 중지", market])
    log_event(log, "feature version changed: old research results archived", market=market, old=old_v, new=F.FEATURE_VERSION)
    return {"steps": {}, "previous_feature_version": old_v}


def preflight(market: str) -> dict:
    """학습 전 저장공간 점검 (수집+연구 합산 예산). 위험하면 StorageRisk 로 중단하고 storage_advice.md 작성."""
    from ..storage.diskguard import GB, DiskGuard, dir_size
    feat_exist = dir_size(F.features_dir(market))
    n_years = len(F.available_years(market))
    feat_need = 0 if feat_exist else int(n_years * 60e6)          # 실측: 연 40~95MB
    planned = feat_need + int(0.3 * GB) + int(0.8 * GB) + int(0.3 * GB)  # 모델 + 예측 DB + 백테스트
    a = DiskGuard(f"머신러닝 연구({market})", planned, peak_temp_bytes=int(2 * GB)).preflight()
    return {"planned_gb": planned / GB, "free_gb": a.disk_free_gb}


def _regime(market: str) -> dict:
    r = regime_analyze(market)
    out = get_settings().paths.reports / f"regime_{market}.json"
    out.write_text(json.dumps({"current": r["current"], "cluster_profile": r["cluster_profile"].to_dict("records"),
                               "rule_stats": r["rule_stats"].to_dict("records"), "crosstab": r["crosstab"].to_dict()},
                              ensure_ascii=False, default=str, indent=1))
    return {"current": r["current"], "rule_stats": r["rule_stats"].to_dict("records")}


def _regime_by_date(market: str) -> dict:
    env = market_env(market)
    env["regime"] = rule_regime(env)
    return dict(zip(env["d"], env["regime"]))


def _search(market: str, pred_runs: dict) -> dict:
    res = S.search(market, S.RULES + S.ML, grid={"n": [10, 20], "rebalance": [5, 20]}, budget=60,
                   pred_runs=pred_runs, horizon=20, regime_by_date=_regime_by_date(market))
    best = res["best_per_strategy"]
    # 선택 구간 자산곡선 저장(국면별 전략 비교용)
    from .experiments import periods
    for r in best.itertuples():
        params = dict(r.params)
        if r.strategy == "ml_confidence_cash":
            params["_recent_ic"] = S.recent_rank_ic(market, pred_runs["ret"], 20,
                                                   S.rebalance_dates(market, pd.to_datetime(res["selection_period"].split("~")[0]).date(),
                                                                     periods(market)["valid_end"], int(params["rebalance"])))
        a, b = res["selection_period"].split("~")
        tg = S.generate_targets(market, r.strategy, date.fromisoformat(a), date.fromisoformat(b), params, pred_runs, _regime_by_date(market))
        out = bt.run(market, tg, date.fromisoformat(a), date.fromisoformat(b), name=r.strategy)
        bt.save(out, r.strategy, {k: v for k, v in params.items() if not k.startswith("_")}, res["selection_period"], note="selection best")
    return {"search_id": res["search_id"], "n_trials": res["n_trials"], "selection_period": res["selection_period"],
            "trials": res["trials"].to_dict("records"), "best": best.to_dict("records"),
            "deflated_sharpe": res.get("deflated_sharpe")}


def _final(market: str, st: dict, pred_runs: dict, forced: bool, wf_spec: dict | None = None) -> dict:
    """최종 독립 검증: 선택 구간에서 고른 전략·모델을 test 구간에 한 번 적용.
    전략이 쓰는 walk-forward run 은 여기서 처음으로 최종 검증 구간까지 이어서 예측한다(앞 연도 fold 는 재사용)."""
    from .experiments import final_test, periods
    P = periods(market)
    for rid in {r for r in pred_runs.values() if r}:
        if wf_spec and rid in wf_spec:
            task, h, algos, window = wf_spec[rid]
            E.walk_forward(market, h, task, algos, window=window, run_id=rid, through="test")
    search = st["steps"]["strategy_search"]["result"]
    best = pd.DataFrame(search["best"])
    sel_sharpe = best.set_index("strategy")["sharpe"]
    t0, t1 = P["test_start"], P["test_end"]
    out = {"period": f"{t0}~{t1}", "forced_rerun": forced, "strategies": [], "models": []}
    reg = _regime_by_date(market)
    capital = 100_000_000 if market == "KR" else 100_000
    bench = bt.benchmark_curves(market, t0, t1, capital)
    for r in best.itertuples():
        params = dict(r.params)
        if r.strategy == "ml_confidence_cash":
            params["_recent_ic"] = S.recent_rank_ic(market, pred_runs["ret"], 20, S.rebalance_dates(market, t0, t1, int(params["rebalance"])))
        tg = S.generate_targets(market, r.strategy, t0, t1, params, pred_runs, reg)
        if not tg:
            continue
        sids = sorted({s for w in tg.values() for s in w})
        pb = bt.PriceBook(market, sids, t0, t1)
        rows = {}
        for mult in (0.0, 1.0, 2.0):
            res = bt.run(market, tg, t0, t1, capital=capital, costs=bt.Costs.for_market(market, mult), prices=pb, name=r.strategy)
            rows[f"cost_x{mult:g}"] = res["metrics"]
            if mult == 1.0:
                main = res
        cons = bt.Constraints.default()
        cons.delisting_scenario = "conservative"
        cons_res = bt.run(market, tg, t0, t1, capital=capital, cons=cons, prices=pb, name=r.strategy)
        boot = bt.block_bootstrap(main["equity"])
        bench_ret = {c: float(bench[c].dropna().iloc[-1] / capital - 1) for c in bench.columns if bench[c].notna().any()}
        main["benchmark"] = bench_ret
        krw = bt.to_krw(main["equity"], main["trades"]) if market == "US" else None
        bt_id = bt.save(main, r.strategy, {k: v for k, v in params.items() if not k.startswith("_")}, search["selection_period"],
                        note="final_test" + (" (재실행: 독립성 훼손)" if forced else ""))
        out["strategies"].append({"strategy": r.strategy, "params": {k: v for k, v in params.items() if not k.startswith("_")},
                                  "selection_sharpe": float(sel_sharpe.get(r.strategy, np.nan)),
                                  "test": main["metrics"], "cost_sensitivity": rows, "conservative_delisting": cons_res["metrics"],
                                  "bootstrap": boot, "benchmark_return": bench_ret,
                                  "yearly": bt.yearly_returns(main["equity"]).to_dict("records"), "bt_id": bt_id,
                                  "krw_view": krw})
    with research_db() as db:
        db.execute("INSERT INTO test_access_log VALUES (now(), 'runner', 'final strategy test', ?)", [f"{market}:{t0}~{t1}"])
    # 고정 분할 모델의 최종 검증 (과제·기간별 검증 성적 1위 비기준 모델)
    chosen = []
    for k, v in st["steps"].items():
        if k.startswith("fixed_") and v.get("done"):
            task = k.split("_")[1]
            cands = [x for x in v["result"] if x["algorithm"] not in ("zero", "hist_mean", "always_up", "hist_vol", "momentum", "reversal")]
            key = (lambda x: x["metrics_valid"].get("auc") or 0) if task == "up" else \
                  ((lambda x: -(x["metrics_valid"].get("rmse") or 9)) if task == "risk" else (lambda x: x["metrics_valid"].get("rank_ic") or -9))
            if cands:
                chosen.append(max(cands, key=key)["exp_id"])
            base = [x for x in v["result"] if x["algorithm"] in ("hist_mean", "always_up", "hist_vol", "momentum")]
            chosen += [b["exp_id"] for b in base[:1]]
    out["models"] = final_test(chosen, purpose="runner final test")
    return out


# ---------------------------------------------------------------------------- 보고서
def _f(x, pct=True, nd=1):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "-"
    return f"{x * 100:.{nd}f}%" if pct else f"{x:.{nd if nd else 2}f}"


def write_report(market: str, st: dict):
    s = get_settings()
    steps = st["steps"]
    P = E.periods(market)
    L = [f"# 머신러닝 투자 연구 보고서 — {'한국' if market == 'KR' else '미국'}", "",
         f"생성: {datetime.now():%Y-%m-%d %H:%M} · 코드 {E.code_version()}", "",
         "## 1. 데이터와 연구 범위", ""]
    dv = E.data_version(market)
    man = dv["feature_manifest"]
    L += [f"- 특징 파일 연도: {min(int(y) for y in man)}~{max(int(y) for y in man)}, 투자 가능(학습 대상) 행 {sum(v or 0 for v in man.values()):,}",
          f"- 학습 {P['train_start']}~{P['train_end']} / 모델 선택(검증) {P['valid_start']}~{P['valid_end']} / 최종 독립 검증 {P['test_start']}~",
          f"- 학습 대상 기준: {s.get('research.universe')}",
          f"- 학습 표본: {E.sample_every_for(market)}거래일 간격 추출 (메모리 절약·라벨 중첩 완화). 경계마다 라벨 기간만큼 purge.",
          "- 제외: 우선주·신주·SPAC·ETF 등 보통주 외, 품질 등급 X, 가격/거래대금 기준 미달, 이력 120거래일 미만 종목-일."]
    if market == "KR":
        L += ["- 한계: 배당 데이터 없음(총수익률 과소), 업종 분류의 시점 이력 없음(업종 특징 미사용), 재무정보 없음(OpenDART 키 필요)."]
    else:
        L += ["- 한계: 상장폐지 종목 대부분 누락 → 생존편향. 결과는 '현재까지 살아남은 종목' 기준이며 실제보다 좋게 나올 수 있음."]
    L += ["", "## 2. 예측 모델 비교 (검증 구간)", "",
          "| 과제 | 기간 | 모델 | Rank IC | t값 | NW t값 | 상위-하위 10% 차이 | AUC | Brier(기준) | RMSE |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for k, v in steps.items():
        if not k.startswith("fixed_") or not v.get("done"):
            continue
        _, task, h = k.split("_")
        for r in v["result"]:
            m = r["metrics_valid"]
            L.append(f"| {task} | {h}일 | {r['algorithm']} | {_f(m.get('rank_ic'), False, 3)} | {_f(m.get('rank_ic_t'), False, 1)} | {_f(m.get('rank_ic_t_nw'), False, 1)} | "
                     f"{_f(m.get('top_bottom_spread'))} | {_f(m.get('auc'), False, 3)} | {_f(m.get('brier'), False, 4)}({_f(m.get('brier_baseline'), False, 4)}) | {_f(m.get('rmse'), False, 4)} |")
    L += ["", "※ Rank IC: 같은 날 종목들의 예측 순위와 실제 수익률 순위의 상관(날짜 평균). 0 이면 예측력 없음. "
          "NW t값은 라벨 기간이 겹쳐 생기는 날짜 간 상관을 Newey-West(라그 = 기간 ÷ 표본 간격)로 보정한 값으로, 보통 t값보다 보수적이다. "
          "Brier 는 확률 예측 오차(낮을수록 좋음), 괄호는 '과거 평균 상승비율'만 쓴 기준값.", ""]
    L += ["## 3. Walk-forward (매년 과거만으로 재학습 → 다음 해 예측, 모델 선택 구간까지)", ""]
    for key, rid in (st.get("wf_runs") or {}).items():
        df = E.wf_summary(rid)
        if not df.empty:   # 최종 검증 단계에서 이어 계산한 연도는 여기서 빼고 선택 구간까지만 요약
            df = df[pd.to_datetime(df["test_start"]).dt.date <= P["valid_end"]]
        if df.empty:
            continue
        col = "auc" if "_up_" in key else "rank_ic"
        if col not in df:
            continue
        g = df.groupby("algorithm")[col].agg(["mean", "std", "count", lambda x: (x > (0.5 if col == "auc" else 0)).mean()])
        for algo, r in g.iterrows():
            L.append(f"- {key} / {algo}: 연도별 {col} 평균 {r['mean']:.3f} (표준편차 {r['std']:.3f}, {int(r['count'])}개 연도, "
                     f"양(+) 비율 {r.iloc[3]:.0%})")
    L += [""]
    if "regime" in steps:
        cur = steps["regime"]["result"]["current"]
        L += ["## 4. 시장 국면", "", f"- 현재({cur['date']}) 규칙 기준 국면: **{cur['regime_rule']}**, 통계 군집 #{cur['cluster']}",
              "- 국면별 이후 20거래일 시장 수익률(과거 통계):"]
        for r in steps["regime"]["result"]["rule_stats"]:
            L.append(f"  - {r['regime_rule']}: {r['n']:,}일, 평균 {_f(r['fwd_ret_20'], True, 2)}, 상승 비율 {_f(r['fwd_ret_20_pos'], True, 0)}")
        L += [""]
    if "strategy_search" in steps:
        sr = steps["strategy_search"]["result"]
        L += ["## 5. 전략 탐색 (선택 구간에서만 비교)", "", f"- 선택 구간: {sr['selection_period']}, 시도한 전략·파라미터 조합: **{sr['n_trials']}개** "
              "(많이 시도할수록 우연히 좋은 결과가 나올 확률이 커짐)", "",
              "| 전략 | 파라미터 | CAGR | 샤프 | DSR | MDD | 거래수 |", "|---|---|---:|---:|---:|---:|---:|"]
        for r in sorted(sr["best"], key=lambda x: -(x.get("sharpe") or -9)):
            L.append(f"| {r['strategy']} | {r['params']} | {_f(r['cagr'])} | {_f(r['sharpe'], False, 2)} | {_f(r.get('dsr'), False, 2)} | "
                     f"{_f(r['mdd'])} | {r['n_trades']} |")
        ds = sr.get("deflated_sharpe")
        if ds:
            L += ["", f"- Deflated Sharpe: 같은 선택 구간 누적 시도 {ds['n_trials']}개를 감안하면, 아무 예측력이 없어도 최고 샤프가 "
                  f"연 {ds['sr0_annual']:.2f} 정도 나올 수 있습니다. DSR 은 그 값을 넘었을 확률(0.95 이상이면 유의)이며, "
                  f"최고 샤프 전략 {ds['best_strategy']} 의 DSR 은 {_f(ds['best_dsr'], False, 2)} 입니다."]
        L += [""]
    if "final_test" in steps:
        ft = steps["final_test"]["result"]
        L += ["## 6. 최종 독립 검증 (선택 후 1회 적용)", "", f"- 기간: {ft['period']}" + (" — ⚠️ 재실행됨: 독립성 훼손" if ft.get("forced_rerun") else ""), "",
              "| 전략 | 선택구간 샤프 | 검증 CAGR | 검증 샤프 | MDD | 비용0 CAGR | 비용2배 CAGR | 보수적 상폐 CAGR | 샤프 90% 구간 | 벤치마크 CAGR(같은 기간) |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---|---|"]
        for r in sorted(ft["strategies"], key=lambda x: -(x["test"].get("sharpe") or -9)):
            t = r["test"]
            b = r["bootstrap"].get("sharpe_p05_p50_p95") if isinstance(r["bootstrap"], dict) else None
            L.append(f"| {r['strategy']} | {_f(r['selection_sharpe'], False, 2)} | {_f(t.get('cagr'))} | {_f(t.get('sharpe'), False, 2)} | {_f(t.get('mdd'))} | "
                     f"{_f(r['cost_sensitivity']['cost_x0'].get('cagr'))} | {_f(r['cost_sensitivity']['cost_x2'].get('cagr'))} | "
                     f"{_f(r['conservative_delisting'].get('cagr'))} | {('%.2f~%.2f' % (b[0], b[2])) if b else '-'} | "
                     f"{', '.join(f'{k} {_f((1 + v) ** (1 / max(t.get('years') or 1, 1e-9)) - 1)}' for k, v in r['benchmark_return'].items())} |")
        L += ["", "### 모델 최종 검증", ""]
        for m in ft["models"]:
            mt = m["metrics_test"]
            L.append(f"- {m['exp_id']}: Rank IC {_f(mt.get('rank_ic'), False, 3)} (t {_f(mt.get('rank_ic_t'), False, 1)}, NW t {_f(mt.get('rank_ic_t_nw'), False, 1)}), AUC {_f(mt.get('auc'), False, 3)}, "
                     f"n={m['n_test']:,}" + (f" ⚠️ {m['warning']}" if m.get("warning") else ""))
        L += ["", "## 7. 해석과 주의", "", *_interpret(ft, steps), ""]
    L += ["## 8. 자원 사용", ""]
    for k, v in steps.items():
        L.append(f"- {k}: {v.get('sec', 0):.0f}초, 프로세스 메모리 {v.get('rss_gb')}GB")
    p = s.paths.reports / f"research_report_{market}.md"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return p


def _cagr_from_total(total: float | None, years: float | None) -> float | None:
    if total is None or not years or years <= 0 or 1 + total <= 0:
        return None
    return (1 + total) ** (1 / years) - 1


def _exp_task_algo(exp_id: str) -> tuple[str, str]:
    """exp_id = '{market}-{task}-{horizon}-{algo}-...' → (task, algo)."""
    parts = exp_id.split("-")
    return (parts[1], parts[3]) if len(parts) >= 4 else ("", "")


def _interpret(ft: dict, steps: dict) -> list[str]:
    """최종 검증 수치를 비교해 결론 문장을 고른다 (고정 문구 없음)."""
    out = []
    strat = [x for x in ft["strategies"] if x["test"].get("sharpe") is not None]
    if not strat:
        return ["- 최종 검증 결과가 없습니다."]
    best = max(strat, key=lambda x: x["test"]["sharpe"])
    b = best["bootstrap"].get("sharpe_p05_p50_p95") if isinstance(best["bootstrap"], dict) else None
    out.append(f"- 최종 검증에서 샤프지수가 가장 높은 전략은 **{best['strategy']}** (CAGR {_f(best['test']['cagr'])}, 샤프 {best['test']['sharpe']:.2f}, MDD {_f(best['test']['mdd'])}).")
    if b and b[0] <= 0:
        out.append(f"- 그러나 블록 부트스트랩 90% 구간({b[0]:.2f}~{b[2]:.2f})이 0 을 포함 → 검증 기간이 짧아 통계적으로 확실하다고 볼 수 없습니다.")
    elif b:
        out.append(f"- 블록 부트스트랩 90% 구간({b[0]:.2f}~{b[2]:.2f})이 0 보다 커서, 이 기간 안에서는 우연만으로 설명하기 어려운 성과입니다.")

    # 벤치마크 비교 (같은 기간 CAGR)
    years = best["test"].get("years")
    bench = {k: _cagr_from_total(v, years) for k, v in (best.get("benchmark_return") or {}).items()}
    bench = {k: v for k, v in bench.items() if v is not None}
    cagr = best["test"].get("cagr")
    beat_bench = None
    if bench and cagr is not None:
        won = [k for k, v in bench.items() if cagr > v]
        lost = [k for k, v in bench.items() if cagr <= v]
        beat_bench = len(won) == len(bench)
        detail = ", ".join(f"{k} {_f(v)}" for k, v in bench.items())
        if beat_bench:
            out.append(f"- {best['strategy']} 의 CAGR {_f(cagr)} 는 모든 벤치마크({detail})보다 높았습니다.")
        elif won:
            out.append(f"- {best['strategy']} 의 CAGR {_f(cagr)} 는 {', '.join(won)} 보다는 높았지만 {', '.join(lost)} 보다는 낮았습니다 ({detail}).")
        else:
            out.append(f"- {best['strategy']} 의 CAGR {_f(cagr)} 는 어느 벤치마크({detail})도 이기지 못했습니다.")

    held = [x for x in strat if (x["selection_sharpe"] or 0) > 0.5 and (x["test"]["sharpe"] or 0) > 0.5]
    dropped = [x["strategy"] for x in strat if (x["selection_sharpe"] or 0) > 0.5 and (x["test"]["sharpe"] or 0) <= 0.3]
    if held:
        out.append("- 선택 구간과 최종 검증 모두 샤프 0.5 초과로 유지된 전략: " + ", ".join(x["strategy"] for x in held))
    else:
        out.append("- 선택 구간과 최종 검증 모두에서 일관되게 좋은 전략은 발견되지 않았습니다.")
    if dropped:
        out.append("- 선택 구간에서는 좋았으나 최종 검증에서 성과가 사라진 전략(탈락): " + ", ".join(dropped))
    ml = [x for x in strat if x["strategy"].startswith("ml_")]
    rule = [x for x in strat if not x["strategy"].startswith("ml_")]
    if ml and rule:
        a, r = np.mean([x["test"]["sharpe"] for x in ml]), np.mean([x["test"]["sharpe"] for x in rule])
        cmp = "높았습니다" if a > r + 0.05 else ("낮았습니다" if a < r - 0.05 else "비슷했습니다")
        out.append(f"- ML 전략 평균 샤프 {a:.2f} vs 규칙 전략 평균 {r:.2f} — ML 쪽이 {cmp}.")

    # 회전율이 높은 전략: 비용 전후 CAGR 을 실제로 비교
    for x in [x for x in strat if (x["test"].get("turnover_per_year") or 0) > 30]:
        c0, c1 = x["cost_sensitivity"]["cost_x0"].get("cagr"), x["test"].get("cagr")
        msg = f"- {x['strategy']}: 연 회전율 {x['test']['turnover_per_year']:.0f}배 — 비용 전 CAGR {_f(c0)} → 비용 후 {_f(c1)}."
        if c0 is not None and c1 is not None:
            if c0 > 0 and c1 <= c0 * 0.5:
                msg += " 잦은 매매 비용이 비용 전 수익의 절반 이상을 상쇄했습니다."
            elif c1 < c0:
                msg += f" 비용으로 연 {_f(c0 - c1)}p 줄었습니다."
        out.append(msg)

    # 모델 순위상관: 검증 구간(고정 분할) 값과 최종 검증 값을 비교
    valid_ic = {r["exp_id"]: (r.get("metrics_valid") or {}).get("rank_ic")
                for k, v in steps.items() if k.startswith("fixed_") and v.get("done") for r in v.get("result") or []}
    pairs = []
    for m in ft.get("models") or []:
        task, algo = _exp_task_algo(m["exp_id"])
        if task == "rank" and algo in ("lgbm", "ridge"):
            t_ic = m["metrics_test"].get("rank_ic")
            if t_ic is not None:
                pairs.append((m["exp_id"], valid_ic.get(m["exp_id"]), t_ic))
    if pairs:
        t_ics = [p[2] for p in pairs]
        v_ics = [p[1] for p in pairs if p[1] is not None]
        rng = f"{min(t_ics):.3f}~{max(t_ics):.3f}"
        vtxt = f" (검증 구간 {min(v_ics):.3f}~{max(v_ics):.3f})" if v_ics else ""
        held_ic = [p for p in pairs if p[1] is not None and p[1] > 0 and p[2] >= 0.5 * p[1]]
        if max(t_ics) <= 0:
            sig = "ic_gone"
            out.append(f"- 순위 모델의 최종 검증 Rank IC 는 {rng}{vtxt}로, 미래 구간에서는 종목 순위 예측력이 관찰되지 않았습니다.")
        elif v_ics and len(held_ic) == len(pairs):
            sig = "ic_held"
            out.append(f"- 순위 모델의 최종 검증 Rank IC 는 {rng}{vtxt}로 검증 구간 값의 절반 이상이 유지되었습니다.")
        else:
            sig = "ic_weak"
            out.append(f"- 순위 모델의 최종 검증 Rank IC 는 {rng}{vtxt}로 양(+)이지만 검증 구간보다 약해졌습니다.")
        if sig != "ic_gone" and beat_bench is not None:
            if beat_bench:
                out.append("- 통계적 신호가 미래 구간에서도 남아 있었고, 가장 좋은 전략은 같은 기간 벤치마크도 넘었습니다.")
            else:
                out.append("- 통계적 신호는 미래 구간에서도 남아 있었지만, 이를 실제 수익으로 바꾸는 전략 설계(회전율·비용·종목 쏠림)에서는 "
                           "벤치마크를 모두 이기지는 못했습니다.")

    # 비용 2배 민감도: 실제로 크게 줄어든 전략만 이름을 든다
    sens = []
    for x in strat:
        c1, c2 = x["test"].get("cagr"), x["cost_sensitivity"].get("cost_x2", {}).get("cagr")
        if c1 is not None and c2 is not None and c1 - c2 > 0.02:
            sens.append(f"{x['strategy']}({_f(c1)}→{_f(c2)})")
    if sens:
        out.append("- 거래비용을 2배로 가정하면 CAGR 이 2%p 넘게 줄어드는 전략: " + ", ".join(sens) + " — 실제 적용 시 성과가 더 낮을 수 있습니다.")
    else:
        out.append("- 거래비용을 2배로 가정해도 CAGR 감소가 2%p 이하로, 비용 가정에 크게 흔들리지 않았습니다.")
    out.append("- 최종 검증 결과를 본 뒤 전략을 고치면 그 구간은 더 이상 독립 검증이 아닙니다. 개선 아이디어(낮은 회전율, 비용 반영 목표 등)는 "
               "새로운 미래 구간에서 다시 검증해야 합니다.")
    out.append("- 변수 중요도는 통계적 연관성일 뿐 인과관계가 아닙니다. 과거에 통했던 패턴이 미래에도 유지된다는 보장은 없습니다.")
    return out
