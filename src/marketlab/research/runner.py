"""연구 전체 실행 (재개 가능): 특징 → 고정분할 학습 → 국면 → walk-forward → 전략 탐색(선택구간) → 최종 검증 1회 → 보고서.

각 단계 완료 여부를 data/metadata/state/research_<market>.json 에 기록한다. 중단 후 다시 실행하면 완료된 단계는 건너뛴다.
최종 검증(test) 단계는 한 번만 실행된다. 다시 실행하려면 force_test=True 가 필요하며, 이 경우 보고서에 '독립성 훼손'이 표시된다.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

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


# 연구 로직 버전: 특징이 아니라 학습·평가·백테스트 규칙이 바뀌어 이전 결과를 재사용하면 안 될 때 올린다.
#  1: 초기  2: 2단계(미체결 매도·상장폐지·up 평가 기간·보정·ml_risk_adj)
#  3: 3단계(walk-forward 는 valid_end 까지, NW t값, deflated Sharpe, 거래대금 구간별 슬리피지 기본 적용)
RESEARCH_VERSION = 3


def versions() -> dict:
    return {"feature_version": F.FEATURE_VERSION, "research_version": RESEARCH_VERSION}


def is_stale(st: dict) -> bool:
    """이전 실행 결과가 있고, 그때의 특징·연구 로직 버전이 지금과 다르면 True (버전 기록이 없으면 v1 로 본다)."""
    return bool(st.get("steps")) and any(st.get(k, 1) != v for k, v in versions().items())


def prior_final_runs(market: str, test_start: date) -> tuple[int, str | None]:
    """같은 시장·같은 시작일의 최종 전략 검증을 예전에 몇 번 실행했는지 (test_access_log)."""
    with research_db(read_only=True) as db:
        n, first = db.execute("SELECT count(*), min(accessed_at) FROM test_access_log WHERE purpose='final strategy test' AND ref LIKE ?",
                              [f"{market}:{test_start}~%"]).fetchone()
    return int(n or 0), (str(first) if first else None)


def wf_run_id(market: str, task: str, h: int, window: str) -> str:
    """버전이 run_id 에 들어가야 무효화 뒤에 예전 fold 결과(fold_results·predictions)를 다시 쓰지 않는다."""
    return f"wf-{market}-{task}-{h}-{window}-f{F.FEATURE_VERSION}r{RESEARCH_VERSION}"


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
    if is_stale(st):
        st = invalidate_old_results(market, st)
    st.update(versions())
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
        rid = wf_runs.get(key) or wf_run_id(market, task, h, window)
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
    """특징 정의나 연구 로직이 바뀌면 이전 연구 결과는 재사용하지 않는다: 상태 파일·보고서 보관, 구버전 모델 retired 처리.
    walk-forward run_id 에 버전이 들어가므로 예전 fold 결과도 새 실행에서 다시 쓰이지 않는다."""
    old = {k: st.get(k, 1) for k in versions()}
    tag = f"featv{old['feature_version']}-resv{old['research_version']}"
    p = _state_path(market)
    p.with_name(f"research_{market}.{tag}.json").write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str))
    rep = get_settings().paths.reports / f"research_report_{market}.md"
    if rep.exists():
        rep.rename(rep.with_name(f"research_report_{market}.{tag}.md"))
    why = []
    if old["feature_version"] != F.FEATURE_VERSION:
        why.append(f"특징 v{old['feature_version']}→v{F.FEATURE_VERSION}")
    if old["research_version"] != RESEARCH_VERSION:
        why.append(f"연구 로직 v{old['research_version']}→v{RESEARCH_VERSION}")
    with research_db() as db:
        db.execute("UPDATE model_registry SET status='retired', note = note || ? WHERE market=? AND status IN ('candidate','approved')",
                   [f"; 이전 버전으로 학습({', '.join(why)}) — 사용 중지", market])
    log_event(log, "research version changed: old results archived", market=market, old=old, new=versions())
    return {"steps": {}, "previous_versions": old}


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


def forward_start(market: str) -> date | None:
    """설정(research.splits.forward_start_<시장> 또는 forward_start)이 있으면 그 날짜, 없으면 None."""
    sp = get_settings().get("research.splits", {}) or {}
    v = sp.get(f"forward_start_{market}") or sp.get("forward_start")
    return date.fromisoformat(str(v)) if v else None


def _sel_dsr(search: dict, strategy: str):
    for b in search.get("best") or []:
        if b.get("strategy") == strategy:
            v = b.get("dsr")
            return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)
    return None


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
    # 같은 시장·같은 시작일의 최종 검증을 예전에 돌린 적이 있으면(다른 버전 포함) 이번 결과는 재검증 — 독립성 훼손으로 표시
    n_prior, first_prior = prior_final_runs(market, t0)
    out = {"period": f"{t0}~{t1}", "forced_rerun": forced, "prior_test_runs": n_prior, "first_test_run_at": first_prior,
           "compromised": bool(forced or n_prior), "strategies": [], "models": [],
           # 전향 검증: 이번 최종 검증으로 연구 결정이 고정된 다음 날부터의 실운영 예측만 '진짜 미검증' 구간으로 본다
           "forward_start": str(forward_start(market) or (date.today() + timedelta(days=1)))}
    reg = _regime_by_date(market)
    capital = 100_000_000 if market == "KR" else 100_000
    bench = bt.benchmark_curves(market, t0, t1, capital)
    for r in best.itertuples():
        params = dict(r.params)
        cash_signal = None
        if r.strategy == "ml_confidence_cash":
            params["_recent_ic"] = S.recent_rank_ic(market, pred_runs["ret"], 20, S.rebalance_dates(market, t0, t1, int(params["rebalance"])))
            cash_signal = {"n_dates": len(params["_recent_ic"]), "n_cash": sum(v < 0 for v in params["_recent_ic"].values())}
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
                                  "bootstrap": boot, "benchmark_return": bench_ret, "cash_signal": cash_signal,
                                  "selection_dsr": _sel_dsr(search, r.strategy),
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
        L += ["## 6. 최종 독립 검증 (선택 후 1회 적용)", "", f"- 기간: {ft['period']}" + (" — ⚠️ 재실행됨: 독립성 훼손" if ft.get("forced_rerun") else ""),
              *_compromise_note(ft), "",
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
        L += ["## 8. 전향 검증 (연구 결정 고정 이후의 실운영 예측)", "", *_forward_section(market, ft), ""]
    L += ["## 9. 점검 요약 (자동)", "", *_checklist(st), ""]
    L += ["## 10. 자원 사용", ""]
    for k, v in steps.items():
        L.append(f"- {k}: {v.get('sec', 0):.0f}초, 프로세스 메모리 {v.get('rss_gb')}GB")
    p = s.paths.reports / f"research_report_{market}.md"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return p


def _compromise_note(ft: dict) -> list[str]:
    n = ft.get("prior_test_runs") or 0
    if not n:
        return []
    return [f"- ⚠️ **재검증**: 이 시장·구간의 최종 검증을 이전에 {n}번 실행했습니다(처음 {ft.get('first_test_run_at')}). "
            "그 결과를 본 뒤 코드·설정을 고쳤을 수 있으므로 이번 수치는 독립 검증이 아닙니다. 참고용으로만 보고, "
            "판단은 8장의 전향 검증 결과로 하세요."]


def _forward_section(market: str, ft: dict) -> list[str]:
    since = date.fromisoformat(ft["forward_start"]) if ft.get("forward_start") else None
    if not since:
        return ["- 전향 검증 시작일이 없습니다."]
    from .monitor import forward_ic
    try:
        f = forward_ic(market, since, 20)
    except Exception as e:   # 예측 테이블·특징 파일이 없는 환경
        return [f"- 전향 검증 계산 불가: {e}"]
    L = [f"- 시작: {since} (이 날 이후 매일 업데이트가 저장한 실운영 예측만 사용, 모델 refit 포함). "
         f"예측 {f['n_pred_dates']}일, 20일 결과가 확정된 날 {f['n_dates']}일."]
    if not f["n_dates"]:
        return L + ["- 아직 결과가 확정된 예측이 없습니다. `update` 를 매일 실행해 예측을 쌓으면 보고서를 다시 만들 때 채워집니다."]
    t = f["rank_ic_t_nw"]
    L.append(f"- Rank IC 평균 {f['rank_ic']:.3f} (NW t {_f(t, False, 1)})")
    if not f["enough"]:
        L.append("- 아직 표본이 부족합니다(60거래일 미만). 결론을 내리지 마세요.")
    elif f["rank_ic"] > 0 and t is not None and t >= 2:
        L.append("- 연구 결정 이후의 새 데이터에서도 순위 예측력이 통계적으로 유지되고 있습니다.")
    elif f["rank_ic"] > 0:
        L.append("- 방향은 양(+)이지만 아직 통계적으로 확실하지 않습니다.")
    else:
        L.append("- 연구 결정 이후 새 데이터에서는 순위 예측력이 관찰되지 않습니다. 전략 적용을 보류하세요.")
    L += [f"  - {m['month']}: {m['rank_ic']:.3f} ({m['n']}일)" for m in f["monthly"][-12:]]
    return L


def _checklist(st: dict) -> list[str]:
    """다시 돌린 뒤 확인할 항목을 수치로 판정 (NW 보정 후 유의성, DSR, 2단계 수정 전략의 동작)."""
    steps, L = st["steps"], []
    lost, kept = [], []
    for k, v in steps.items():
        if not k.startswith("fixed_") or not v.get("done"):
            continue
        for r in v.get("result") or []:
            m = r.get("metrics_valid") or {}
            t, tnw = m.get("rank_ic_t"), m.get("rank_ic_t_nw")
            if t is not None and tnw is not None and t >= 2:
                (kept if tnw >= 2 else lost).append(f"{k[6:]}/{r['algorithm']}(t {t:.1f}→{tnw:.1f})")
    if kept or lost:
        L.append(f"- NW 보정: 보통 t≥2 였던 모델 {len(kept) + len(lost)}개 중 보정 후에도 t≥2 유지 {len(kept)}개"
                 + (f", 유의성 사라짐 {len(lost)}개: {', '.join(lost)}" if lost else "."))
    else:
        L.append("- NW 보정: 비교할 모델 기록이 없습니다(이전 버전 기록에는 NW t값이 없음).")
    sr = (steps.get("strategy_search") or {}).get("result") or {}
    best = sr.get("best") or []
    dsr_ok = [b["strategy"] for b in best if (b.get("dsr") or 0) >= 0.95]
    if best:
        ds = sr.get("deflated_sharpe") or {}
        L.append(f"- DSR: 누적 시도 {ds.get('n_trials', '?')}개 기준, 0.95 이상인 선택 전략 {len(dsr_ok)}/{len(best)}개"
                 + (f" ({', '.join(dsr_ok)})" if dsr_ok else " — 다중 시도를 감안하면 우연과 구분되는 전략이 없습니다."))
    ft = (steps.get("final_test") or {}).get("result") or {}
    by = {x["strategy"]: x for x in ft.get("strategies") or []}
    cc = by.get("ml_confidence_cash")
    if cc and cc.get("cash_signal"):
        c = cc["cash_signal"]
        L.append(f"- ml_confidence_cash: 리밸런싱 {c['n_dates']}번 중 현금 전환 {c['n_cash']}번"
                 + (" — 현금 전환 조건이 실제로 작동했습니다." if c["n_cash"] else " — 최종 검증 구간에서는 순위상관이 음수인 때가 없었습니다."))
    ra, top = by.get("ml_risk_adj"), by.get("ml_top_ret")
    if ra and top and ra["test"].get("sharpe") is not None and top["test"].get("sharpe") is not None:
        d = ra["test"]["sharpe"] - top["test"]["sharpe"]
        L.append(f"- ml_risk_adj(순위 점수 표준화 후) 샤프 {ra['test']['sharpe']:.2f} vs ml_top_ret {top['test']['sharpe']:.2f}: "
                 + ("변동성 조정이 도움이 됐습니다." if d > 0.05 else ("변동성 조정이 오히려 낮췄습니다." if d < -0.05 else "차이가 거의 없습니다.")))
    if ft.get("compromised"):
        L.append("- 최종 검증은 재검증(독립성 훼손) 상태입니다 → 판단은 전향 검증(8장)으로.")
    return L or ["- 점검할 결과가 없습니다."]


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
