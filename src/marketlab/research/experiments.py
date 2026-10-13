"""학습·검증 실험 (고정 분할 + Walk-forward), 실험 기록, 최종 검증 접근 통제.

예측 과제(task)
- return : y_ret_k (t+1 시가 진입 → t+k 종가) 회귀
- rank   : y_rank_k (같은 날 투자 가능 종목 내 실현수익률 백분위) 회귀 → 종목 순위
- up     : y_up_k (y_ret_k > 0) 분류 + 검증 구간 등위회귀 확률 보정
- risk   : y_vol_k (t+1~t+k 실현 변동성) 회귀

시간 규칙
- 고정 분할: 학습(~train_end) / 모델 선택(valid) / 최종 독립 검증(test_start~). 경계마다 라벨 기간만큼 purge.
- 최종 검증 구간은 final_test() 로만 평가하며 매번 test_access_log 에 기록한다.
- Walk-forward: 연 단위로 과거만 학습 → 다음 해 예측. 확장형/고정길이 창 비교. 완료된 fold 는 저장되어 재개 가능.
"""
from __future__ import annotations

import subprocess
import time
import uuid
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from ..config import get_settings
from ..logging_setup import get_logger, log_event
from . import dataset
from .dataset import sample_every_for
from .evaluation import classification_metrics, regression_metrics
from .features import FEATURES, available_years
from .models import Model
from .resources import MemoryMonitor, estimate_training_memory, release_memory
from .store import jdump, research_db

log = get_logger("experiments")

TASKS = {
    "return": ("y_ret_{k}", "regression", ["zero", "hist_mean", "momentum", "reversal", "linear", "ridge", "lgbm"]),
    "rank": ("y_rank_{k}", "regression", ["momentum", "reversal", "ridge", "lgbm"]),
    "up": ("y_up_{k}", "classification", ["always_up", "momentum", "logistic", "lgbm"]),
    "risk": ("y_vol_{k}", "regression", ["hist_vol", "ridge", "lgbm"]),
}


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x))


def code_version() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=get_settings().paths.root, timeout=5).stdout.strip()
    except Exception:
        return "unknown"


def data_version(market: str) -> dict:
    import json
    from .features import features_dir
    p = features_dir(market) / "_manifest.json"
    man = json.loads(p.read_text()) if p.exists() else {}
    return {"feature_manifest": {k: v.get("eligible") for k, v in man.items() if not k.startswith("_")},
            "years": available_years(market)}


def periods(market: str) -> dict:
    s = get_settings().get("research.splits", {}) or {}
    ys = available_years(market)
    start = date(ys[0] + 1, 1, 1) if ys else date(2000, 1, 1)   # 첫 해는 특징 이력이 짧아 제외
    cfg_start = (get_settings().get("research.train_start", {}) or {}).get(market)
    if cfg_start:
        start = max(start, _d(cfg_start))
    return {"train_start": start, "train_end": _d(s.get("train_end", "2020-12-31")),
            "valid_start": _d(s.get("valid_start", "2021-01-01")), "valid_end": _d(s.get("valid_end", "2023-12-31")),
            "test_start": _d(s.get("test_start", "2024-01-01")), "test_end": date.today()}


def _target(task: str, horizon: int) -> tuple[str, str]:
    t, kind, _ = TASKS[task]
    return t.format(k=horizon), kind


def _make_model(algo: str, task: str, kind: str) -> Model:
    if algo == "hist_vol":
        m = Model("hist_mean", kind, FEATURES)
        m.algorithm = "hist_vol"
        return m
    return Model(algo, kind, FEATURES)


def _predict(m: Model, X: pd.DataFrame) -> np.ndarray:
    if m.algorithm == "hist_vol":
        return np.nan_to_num(X["rv_20"].to_numpy(dtype=float), nan=float(np.nanmedian(X["rv_20"])))
    return m.predict(X)


def _metrics(task: str, df: pd.DataFrame, horizon: int) -> dict:
    if task == "up":
        out = classification_metrics(df, "pred", "y")
        # 투자 관점: 상승 확률 상위 20% 의 실현 수익률
        if f"y_ret_{horizon}" in df:
            q = df.groupby("trade_date")["pred"].rank(pct=True)
            out["top20_mean_ret"] = float(df.loc[q >= 0.8, f"y_ret_{horizon}"].mean())
            out["all_mean_ret"] = float(df[f"y_ret_{horizon}"].mean())
        return out
    if task == "rank":
        # 순위 모델은 실현 수익률과의 순위 상관으로 평가
        out = regression_metrics(df.assign(y=df[f"y_ret_{horizon}"]), "pred", "y")
        for k in ("mae", "rmse", "direction_acc"):
            out.pop(k, None)
        return out
    return regression_metrics(df, "pred", "y")


def train_eval(market: str, horizon: int, task: str, algorithms: list[str] | None = None,
               sample_every: int | None = None, progress=None, winsorize: tuple | None = None) -> list[dict]:
    """winsorize=(0.01, 0.99): 수익률 라벨의 극단값을 '학습 구간 분위수'로 잘라 학습 (평가는 원래 값). 꼬리가 두꺼운 수익률에서
    트리 모델이 상수 예측으로 무너지는 것을 막는다."""
    """고정 분할 학습 → 검증 구간 평가. 모델·실험 기록 저장. 최종 검증 구간은 건드리지 않는다."""
    s = get_settings()
    target, kind = _target(task, horizon)
    algos = algorithms or TASKS[task][2]
    P = periods(market)
    tr_end = min(P["train_end"], dataset.purge_cutoff(market, P["valid_start"], horizon))
    va_end = min(P["valid_end"], dataset.purge_cutoff(market, P["test_start"], horizon))
    labels = list(dict.fromkeys([target, f"y_ret_{horizon}"]))
    n_est = dataset.count_rows(market, P["train_start"], tr_end, target, sample_every or sample_every_for(market))
    mem = estimate_training_memory(n_est, len(FEATURES))
    if not mem["fits"]:
        raise MemoryError(f"예상 메모리 {mem['estimated_peak_gb']:.1f}GB > 목표. 표본 간격을 늘리거나(sample_every) 기간을 나누세요. "
                          f"허용 행 수 ≈ {mem['max_rows_within_soft']:,}")
    train = dataset.load(market, P["train_start"], tr_end, labels=labels, sample_every=sample_every, require_label=target)
    valid = dataset.load(market, P["valid_start"], va_end, labels=labels, sample_every=sample_every, require_label=target)
    results = []
    seed = int(s.get("research.seed", 42))
    for i, algo in enumerate(algos):
        if progress:
            progress(i / len(algos), f"{algo} 학습 중")
        mon = MemoryMonitor().start()
        t0 = time.time()
        try:
            m = _make_model(algo, task, kind)
            y_tr = train[target].to_numpy()
            if winsorize and kind == "regression":
                lo, hi = np.nanquantile(y_tr, winsorize[0]), np.nanquantile(y_tr, winsorize[1])
                y_tr = np.clip(y_tr, lo, hi)
                m.params["winsorize"] = [float(winsorize[0]), float(winsorize[1]), float(lo), float(hi)]
            if algo in ("lgbm",):
                # 조기 종료는 학습 구간 마지막 15% 날짜(+purge)로만 판단 → 검증 구간은 순수 평가용
                dates = np.sort(train["trade_date"].unique())
                cut = dates[int(len(dates) * 0.85)]
                inner_end = dataset.purge_cutoff(market, cut, horizon)
                fit_mask = train["trade_date"] <= inner_end
                es_mask = train["trade_date"] >= cut
                m.fit(train[fit_mask], y_tr[fit_mask.to_numpy()], train[es_mask], y_tr[es_mask.to_numpy()], seed=seed)
            elif algo != "hist_vol":
                m.fit(train, y_tr, seed=seed)
            mon.check()
            if kind == "classification" and algo in ("logistic", "lgbm"):
                # 확률 보정: 검증 구간 앞 절반으로 보정, 뒤 절반으로 평가 (보정 데이터와 평가 데이터 분리)
                vd = np.sort(valid["trade_date"].unique())
                half = vd[len(vd) // 2]
                cal_mask = valid["trade_date"] < dataset.purge_cutoff(market, half, horizon)
                raw = m.predict(valid[cal_mask])
                m.calibrate(raw, valid.loc[cal_mask, target].to_numpy())
                eval_df = valid[valid["trade_date"] >= half]
            else:
                eval_df = valid
            ev = eval_df[["security_id", "trade_date", f"y_ret_{horizon}"]].copy() if f"y_ret_{horizon}" != target else eval_df[["security_id", "trade_date"]].copy()
            ev["pred"] = _predict(m, eval_df)
            ev["y"] = eval_df[target].to_numpy()
            if f"y_ret_{horizon}" not in ev:
                ev[f"y_ret_{horizon}"] = eval_df[f"y_ret_{horizon}"].to_numpy()
            met = _metrics(task, ev, horizon)
            if kind == "regression" and task != "rank":
                res = (ev["y"] - ev["pred"]).dropna()
                met["resid_q"] = [float(res.quantile(q)) for q in (0.1, 0.5, 0.9)]
            secs = time.time() - t0
        finally:
            mon.stop()
        exp_id = f"{market}-{task}-{horizon}-{algo}-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4]}"
        path = s.paths.data / s.get("research.models_dir", "research_models") / f"{exp_id}.pkl"
        size = m.save(path)
        imp = m.importance()
        rec = {"exp_id": exp_id, "market": market, "task": task, "horizon": horizon, "algorithm": algo,
               "backend": m.backend, "metrics_valid": met, "n_train": len(train), "n_valid": len(eval_df),
               "train_seconds": round(secs, 1), "peak_rss_gb": round(mon.peak_rss_gb, 2), "model_bytes": size,
               "importance_top": imp.head(15).round(4).to_dict() if imp is not None else None}
        with research_db() as db:
            db.execute("INSERT INTO experiments VALUES (?, now(), 'fixed_split', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, 'done', ?)",
                       [exp_id, market, target, horizon, algo, jdump({**m.params, "backend": m.backend,
                        "best_iteration": m.best_iteration}), jdump(FEATURES),
                        jdump(get_settings().get("research.universe")), jdump({k: str(v) for k, v in P.items()} | {"train_end_purged": str(tr_end), "valid_end_purged": str(va_end)}),
                        jdump(data_version(market)), seed, len(train), len(eval_df), secs, mon.peak_rss_gb,
                        jdump(rec), str(path), code_version(), jdump({"sample_every": sample_every or sample_every_for(market)})])
            db.execute("INSERT OR REPLACE INTO model_registry VALUES (?, now(), ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)",
                       [exp_id, market, horizon, algo, tr_end, exp_id, str(path), jdump(met), f"task={task}"])
        log_event(log, "experiment done", exp_id=exp_id, rank_ic=met.get("rank_ic"), auc=met.get("auc"))
        results.append(rec)
    if progress:
        progress(1.0, "완료")
    return results


def final_test(exp_ids: list[str], purpose: str = "final evaluation") -> list[dict]:
    """최종 독립 검증. 호출 기록이 남으며, 같은 실험을 반복 평가하면 경고한다."""
    out = []
    with research_db() as db:
        rows = db.execute("SELECT exp_id, market, target, horizon, algorithm, model_path FROM experiments WHERE exp_id IN (SELECT unnest(?::VARCHAR[]))",
                          [exp_ids]).fetchall()
        prev = dict(db.execute("SELECT ref, count(*) FROM test_access_log GROUP BY 1").fetchall())
    for exp_id, market, target, horizon, algo, path in rows:
        task = {"y_ret": "return", "y_rank": "rank", "y_up": "up", "y_vol": "risk"}[target.rsplit("_", 1)[0]]
        P = periods(market)
        m = Model.load(path)
        test = dataset.load(market, P["test_start"], P["test_end"], labels=list(dict.fromkeys([target, f"y_ret_{horizon}"])),
                            require_label=target)
        ev = test[["security_id", "trade_date", f"y_ret_{horizon}"]].copy()
        ev["pred"] = _predict(m, test)
        ev["y"] = test[target].to_numpy()
        met = _metrics(task, ev, horizon)
        n_prev = prev.get(exp_id, 0)
        rec = {"exp_id": exp_id, "metrics_test": met, "n_test": len(test), "test_period": f"{P['test_start']}~{test['trade_date'].max()}",
               "previous_accesses": n_prev,
               "warning": "이 실험의 최종 검증 결과를 이미 본 적이 있습니다. 이후 수정한 모델은 독립 검증이 아닙니다." if n_prev else None}
        with research_db() as db:
            db.execute("INSERT INTO test_access_log VALUES (now(), 'user', ?, ?)", [purpose, exp_id])
            db.execute("UPDATE experiments SET n_test = ?, metrics = json_merge_patch(metrics, ?) WHERE exp_id = ?",
                       [len(test), jdump({"metrics_test": met}), exp_id])
        out.append(rec)
    return out


# ---------------------------------------------------------------------------- walk-forward
def walk_forward(market: str, horizon: int, task: str, algorithms: list[str] | None = None,
                 window: str = "expanding", run_id: str | None = None, sample_every: int | None = None,
                 save_predictions: bool = True, progress=None) -> dict:
    """연 단위 walk-forward. 각 fold 모델은 test 시작 전(라벨 purge 반영) 데이터만으로 학습한다."""
    s = get_settings()
    wf = s.get("research.walkforward", {}) or {}
    target, kind = _target(task, horizon)
    algos = algorithms or (["ridge", "lgbm"] if kind == "regression" else ["logistic", "lgbm"])
    ys = available_years(market)
    t0 = periods(market)["train_start"]
    first = max(t0.year + int(wf.get("first_train_years", 6)), int((wf.get("min_test_year") or {}).get(market, 0)))
    test_years = [y for y in ys if y >= first]
    run_id = run_id or f"wf-{market}-{task}-{horizon}-{window}-{datetime.now():%Y%m%d%H%M%S}"
    with research_db() as db:
        done = {(r[0], r[1]) for r in db.execute("SELECT fold, algorithm FROM fold_results WHERE run_id=? AND wf_window=?",
                                                  [run_id, window]).fetchall()}
    seed = int(s.get("research.seed", 42))
    summary = []
    for fi, ty in enumerate(test_years):
        tstart, tend = date(ty, 1, 1), date(ty, 12, 31)
        tr_end = dataset.purge_cutoff(market, tstart, horizon)
        tr_start = t0 if window == "expanding" else max(t0, date(ty - int(wf.get("rolling_years", 8)), 1, 1))
        if all((fi, a) in done for a in algos):
            continue
        labels = list(dict.fromkeys([target, f"y_ret_{horizon}"]))
        train = dataset.load(market, tr_start, tr_end, labels=labels, sample_every=sample_every, require_label=target)
        test = dataset.load(market, tstart, tend, labels=labels, sample_every=1)
        if train.empty or test.empty:
            continue
        for algo in algos:
            if (fi, algo) in done:
                continue
            if progress:
                progress(fi / len(test_years), f"{ty}년 {algo}")
            mon = MemoryMonitor().start()
            try:
                m = _make_model(algo, task, kind)
                y = train[target].to_numpy()
                if algo == "lgbm":
                    dates = np.sort(train["trade_date"].unique())
                    cut = dates[int(len(dates) * 0.85)]
                    fit_mask = (train["trade_date"] <= dataset.purge_cutoff(market, cut, horizon)).to_numpy()
                    es_mask = (train["trade_date"] >= cut).to_numpy()
                    m.fit(train[fit_mask], y[fit_mask], train[es_mask], y[es_mask], seed=seed)
                elif algo != "hist_vol":
                    m.fit(train, y, seed=seed)
                mon.check()
                pred = _predict(m, test)
            finally:
                mon.stop()
            ev = test[["security_id", "trade_date", f"y_ret_{horizon}"]].copy()
            ev["pred"] = pred
            ev["y"] = test[target].to_numpy()
            met = _metrics(task, ev.dropna(subset=["y"]), horizon)
            with research_db() as db:
                db.execute("INSERT OR REPLACE INTO fold_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, now())",
                           [run_id, fi, market, horizon, algo, window, tr_start, tr_end, tstart, tend, len(train), len(test), jdump(met)])
                if save_predictions:
                    pdf = pd.DataFrame({"run_id": run_id, "market": market, "horizon": horizon, "algorithm": algo,
                                        "security_id": test["security_id"].to_numpy(), "trade_date": test["trade_date"].to_numpy(),
                                        "pred_ret": pred if task in ("return",) else np.nan,
                                        "pred_up": pred if task == "up" else np.nan,
                                        "pred_rank": pred if task == "rank" else np.nan,
                                        "pred_vol": pred if task == "risk" else np.nan,
                                        "model_trained_until": tr_end, "kind": f"oos_{window}"})
                    db.execute("DELETE FROM predictions WHERE run_id=? AND algorithm=? AND trade_date BETWEEN ? AND ?",
                               [run_id, algo, tstart, tend])
                    db.register("pdf", pdf)
                    db.execute("INSERT INTO predictions SELECT * FROM pdf")
            summary.append({"fold": fi, "test_year": ty, "algorithm": algo, "n_train": len(train), "n_test": len(test),
                            "rank_ic": met.get("rank_ic"), "auc": met.get("auc"), "peak_rss_gb": round(mon.peak_rss_gb, 2)})
            log_event(log, "wf fold", run_id=run_id, year=ty, algo=algo, rank_ic=met.get("rank_ic"))
        del train, test
        release_memory()
    if progress:
        progress(1.0, "완료")
    return {"run_id": run_id, "folds": summary}


def wf_summary(run_id: str) -> pd.DataFrame:
    with research_db(read_only=True) as db:
        df = db.execute("SELECT fold, algorithm, wf_window, test_start, n_train, n_test, metrics FROM fold_results WHERE run_id=? ORDER BY fold",
                        [run_id]).df()
    import json
    if df.empty:
        return df
    m = pd.json_normalize(df["metrics"].map(json.loads))
    return pd.concat([df.drop(columns="metrics"), m], axis=1)


def refit_live(market: str, horizon: int, task: str, source_exp_id: str | None = None) -> dict:
    """운영용 재학습: 검증으로 선택된 실험과 같은 알고리즘·설정을, 라벨이 확정된 최신 날짜까지의 데이터로 다시 학습.

    독립 검증 성적이 없으므로 원 실험의 검증 지표를 함께 기록하고 note 에 'refit' 을 남긴다.
    분석 화면은 승인 모델 → 최신 refit → 검증 최고 후보 순으로 사용한다(자동 승인 없음).
    """
    from .analysis import pick_model
    s = get_settings()
    target, kind = _target(task, horizon)
    src = None
    with research_db(read_only=True) as db:
        if source_exp_id:
            src = db.execute("SELECT exp_id, algorithm, params, metrics FROM experiments WHERE exp_id=?", [source_exp_id]).fetchone()
    if src is None:
        m = pick_model(market, horizon, task, include_refit=False)
        if not m:
            raise RuntimeError("먼저 고정 분할 학습으로 검증된 모델이 필요합니다.")
        with research_db(read_only=True) as db:
            src = db.execute("SELECT exp_id, algorithm, params, metrics FROM experiments WHERE exp_id=?", [m["model_id"]]).fetchone()
    exp_src, algo, params_json, metrics_json = src
    import json as _json
    valid_metrics = _json.loads(metrics_json).get("metrics_valid", {})
    P = periods(market)
    last = max(d for d in dataset.trading_days(market) if d <= date.today())
    tr_end = dataset.purge_cutoff(market, last, horizon)
    labels = list(dict.fromkeys([target, f"y_ret_{horizon}"]))
    train = dataset.load(market, P["train_start"], tr_end, labels=labels, require_label=target)
    mon = MemoryMonitor().start()
    t0 = time.time()
    try:
        m = _make_model(algo, task, kind)
        y = train[target].to_numpy()
        if algo == "lgbm":
            dates = np.sort(train["trade_date"].unique())
            cut = dates[int(len(dates) * 0.9)]
            fm = (train["trade_date"] <= dataset.purge_cutoff(market, cut, horizon)).to_numpy()
            em = (train["trade_date"] >= cut).to_numpy()
            m.fit(train[fm], y[fm], train[em], y[em], seed=int(s.get("research.seed", 42)))
        elif algo != "hist_vol":
            m.fit(train, y, seed=int(s.get("research.seed", 42)))
        if kind == "classification" and algo in ("logistic", "lgbm"):
            # 보정: 최근 2년(학습에 포함되지만 별도 적합 불가) 대신 원 실험의 보정기를 재사용
            src_model = Model.load(s.paths.data / s.get("research.models_dir", "research_models") / f"{exp_src}.pkl")
            m.calibrator = src_model.calibrator
        mon.check()
    finally:
        mon.stop()
    exp_id = f"{market}-{task}-{horizon}-{algo}-refit-{datetime.now():%Y%m%d%H%M%S}"
    path = s.paths.data / s.get("research.models_dir", "research_models") / f"{exp_id}.pkl"
    m.save(path)
    rec = {"exp_id": exp_id, "source_exp": exp_src, "metrics_valid": valid_metrics, "n_train": len(train),
           "train_seconds": round(time.time() - t0, 1), "peak_rss_gb": round(mon.peak_rss_gb, 2), "trained_until": str(tr_end)}
    with research_db() as db:
        db.execute("INSERT INTO experiments VALUES (?, now(), 'refit', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?, ?, 'done', ?)",
                   [exp_id, market, target, horizon, algo, params_json, jdump(FEATURES), jdump(s.get("research.universe")),
                    jdump({"train_start": str(P["train_start"]), "train_end_purged": str(tr_end)}), jdump(data_version(market)),
                    int(s.get("research.seed", 42)), len(train), rec["train_seconds"], mon.peak_rss_gb, jdump(rec), str(path),
                    code_version(), f"refit of {exp_src}"])
        db.execute("INSERT OR REPLACE INTO model_registry VALUES (?, now(), ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)",
                   [exp_id, market, horizon, algo, tr_end, exp_id, str(path), jdump(valid_metrics), f"task={task}; refit of {exp_src}"])
    return rec
