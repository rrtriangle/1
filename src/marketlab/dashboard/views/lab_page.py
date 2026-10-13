import json
import subprocess
import sys

import pandas as pd
import plotly.express as px
import streamlit as st

from marketlab.config import get_settings
from marketlab.dashboard.common import research_df
from marketlab.research import experiments as E
from marketlab.research import runner
from marketlab.research.features import FEATURES
from marketlab.research.resources import estimate_training_memory

S = get_settings()
st.title("머신러닝 연구소")
market = st.radio("연구 시장", ["KR", "US"], horizontal=True, format_func=lambda x: {"KR": "한국", "US": "미국"}[x])
P = E.periods(market)
st.caption(f"학습 {P['train_start']} – {P['train_end']} · 모델 선택 {P['valid_start']} – {P['valid_end']} · 최종 독립 검증 {P['test_start']} 이후 "
           "(최종 검증 구간은 모델·전략 선택에 쓰지 않습니다)")


def launch(args, label):
    log = S.paths.logs / "research_jobs.log"
    with open(log, "a") as fh:
        subprocess.Popen([sys.executable, "-m", "marketlab.cli.main", "research", *args], cwd=S.paths.root,
                         stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    st.success(f"{label} 시작 — 진행 기록: logs/research_jobs.log (한 번에 하나만 실행하세요)")


t1, t2, t3, t4 = st.tabs(["학습 실행", "모델 비교", "Walk-forward", "학습 이력·승인"])
with t1:
    c = st.columns(4)
    task = c[0].selectbox("예측 과제", ["rank", "return", "up", "risk"],
                          format_func=lambda x: {"rank": "종목 상대순위", "return": "향후 수익률", "up": "상승 확률", "risk": "위험(변동성)"}[x])
    h = c[1].selectbox("예측 기간", [5, 20, 60], index=1)
    algos = c[2].multiselect("모델", E.TASKS[task][2], default=E.TASKS[task][2])
    est = estimate_training_memory(1_100_000, len(FEATURES))
    c[3].metric("예상 학습 메모리", f"{est['estimated_peak_gb']:.1f}GB", help="KR 기준 약 110만 행 × 특징 수, float32. 한도 초과 시 자동 중단")
    if st.button("고정 분할 학습 시작"):
        launch(["train", "--market", market, "--task", task, "--horizon", str(h), "--algos", *algos], "학습")
    w = st.selectbox("Walk-forward 창", ["expanding", "rolling"], format_func=lambda x: {"expanding": "확장형(처음부터 누적)", "rolling": "고정 길이(최근 8년)"}[x])
    if st.button("Walk-forward 시작"):
        launch(["walkforward", "--market", market, "--task", task, "--horizon", str(h), "--window", w, "--algos", *(algos or ["lgbm"])], "Walk-forward")
    st.divider()
    st.write("**연구 전체 실행**: 특징 → 모든 과제 학습 → 국면 → walk-forward → 전략 탐색 → 최종 검증 1회 → 보고서 (수 시간, 중단 후 재개 가능)")
    if st.button("연구 전체 실행/재개", type="primary"):
        launch(["run-all", "--market", market], "연구 전체 실행")
    stt = runner.load_state(market)
    if stt.get("steps"):
        st.dataframe(pd.DataFrame([{"단계": k, "완료": v.get("done"), "초": v.get("sec"), "단계 종료 시 메모리GB": v.get("rss_gb"), "시각": v.get("at")}
                                   for k, v in stt["steps"].items()]), hide_index=True)
    logf = S.paths.logs / "research_jobs.log"
    if logf.exists():
        with st.expander("최근 작업 로그"):
            st.code("\n".join(logf.read_text(errors="replace").splitlines()[-30:]))

with t2:
    ex = research_df("SELECT exp_id, created_at, target, horizon, algorithm, n_train, n_valid, train_seconds, peak_rss_gb, metrics FROM experiments WHERE market=? ORDER BY created_at DESC", (market,))
    if ex.empty:
        st.info("실험 기록 없음")
    else:
        m = pd.json_normalize(ex["metrics"].map(json.loads))
        mv = pd.json_normalize(m["metrics_valid"]) if "metrics_valid" in m else pd.DataFrame()
        tbl = pd.concat([ex.drop(columns="metrics"), mv[[c for c in ("rank_ic", "rank_ic_t", "top_bottom_spread", "auc", "brier", "brier_baseline", "rmse", "mae") if c in mv]]], axis=1)
        tgt = st.selectbox("예측 대상", sorted(tbl["target"].unique()))
        sub = tbl[tbl["target"] == tgt].drop_duplicates("algorithm")
        st.dataframe(sub, hide_index=True, use_container_width=True)
        metric = "auc" if tgt.startswith("y_up") else ("rmse" if tgt.startswith("y_vol") else "rank_ic")
        if metric in sub:
            st.plotly_chart(px.bar(sub, x="algorithm", y=metric, title=f"검증 구간 {metric}"), use_container_width=True)
        pick = st.selectbox("특징 중요도 볼 실험", sub["exp_id"])
        imp = json.loads(ex.set_index("exp_id").loc[pick, "metrics"]).get("importance_top")
        if imp:
            from marketlab.research.analysis import ko
            di = pd.DataFrame({"특징": [ko(k) for k in imp], "중요도": list(imp.values())})
            st.plotly_chart(px.bar(di, x="중요도", y="특징", orientation="h", height=420), use_container_width=True)
            st.caption("중요도는 모델이 예측에 많이 사용한 정도이며, 주가 변동의 원인이라는 뜻이 아닙니다.")
        cal = json.loads(ex.set_index("exp_id").loc[pick, "metrics"]).get("metrics_valid", {}).get("calibration")
        if cal:
            dc = pd.DataFrame(cal)
            fig = px.line(dc, x="mean_pred", y="observed_up", markers=True, title="확률 보정: 예측 확률 vs 실제 상승 비율")
            fig.add_shape(type="line", x0=0, y0=0, x1=1, y1=1, line=dict(dash="dot"))
            st.plotly_chart(fig, use_container_width=True)

with t3:
    runs = research_df("SELECT DISTINCT run_id FROM fold_results WHERE market=? ORDER BY run_id", (market,))
    if runs.empty:
        st.info("walk-forward 결과 없음")
    else:
        rid = st.selectbox("실행", runs["run_id"])
        wf = E.wf_summary(rid)
        col = "auc" if "-up-" in rid else "rank_ic"
        if col in wf:
            wf["year"] = pd.to_datetime(wf["test_start"]).dt.year
            st.plotly_chart(px.bar(wf, x="year", y=col, color="algorithm", barmode="group", title=f"연도별 {col} (매년 과거만으로 재학습)"),
                            use_container_width=True)
            st.dataframe(wf.groupby("algorithm")[col].describe(), use_container_width=True)
        st.dataframe(wf, hide_index=True, use_container_width=True)

with t4:
    reg = research_df("SELECT model_id, created_at, horizon, algorithm, trained_until, status, note, valid_metrics->>'rank_ic' AS rank_ic, valid_metrics->>'auc' AS auc FROM model_registry WHERE market=? ORDER BY created_at DESC", (market,))
    st.dataframe(reg, hide_index=True, use_container_width=True)
    st.caption("새로 학습한 모델은 'candidate' 로 등록됩니다. 검증 성적을 확인한 뒤 직접 승인해야 분석 화면의 기본 모델이 됩니다(자동 교체 없음).")
    if not reg.empty:
        mid = st.selectbox("승인할 모델", reg[reg["status"] == "candidate"]["model_id"] if (reg["status"] == "candidate").any() else [])
        if mid and st.button("이 모델 승인"):
            from marketlab.research.monitor import promote
            st.success(promote(mid))
    acc = research_df("SELECT * FROM test_access_log ORDER BY accessed_at DESC")
    with st.expander(f"최종 검증 구간 사용 기록 ({len(acc)}회)"):
        st.dataframe(acc, hide_index=True)
    rep = S.paths.reports / f"research_report_{market}.md"
    if rep.exists():
        with st.expander("연구 보고서", expanded=False):
            st.markdown(rep.read_text(encoding="utf-8"))
