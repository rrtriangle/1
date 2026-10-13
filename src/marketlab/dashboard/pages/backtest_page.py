from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from marketlab.dashboard.common import disclaimer, pct, research_df
from marketlab.research import backtest as bt
from marketlab.research import experiments as E
from marketlab.research import runner
from marketlab.research import strategies as S
from marketlab.research.store import research_db

st.title("전략 백테스트")
market = st.radio("시장", ["KR", "US"], horizontal=True, format_func=lambda x: {"KR": "한국", "US": "미국"}[x])
P = E.periods(market)
t1, t2 = st.tabs(["직접 실행", "저장된 결과"])

with t1:
    c = st.columns(3)
    strat = c[0].selectbox("전략", S.RULES + S.ML, format_func=lambda x: f"{x} — {S.DESCRIPTIONS[x]}")
    n = c[1].slider("보유 종목 수", 5, 50, 20)
    reb = c[2].selectbox("리밸런싱 주기(거래일)", [5, 20, 60], index=1)
    c = st.columns(4)
    period = c[0].selectbox("기간", ["선택 구간(학습~검증)", "최종 검증 구간", "직접 지정"])
    if period.startswith("선택"):
        start, end = date(max(2003, P["train_start"].year), 1, 1), P["valid_end"]
    elif period.startswith("최종"):
        start, end = P["test_start"], P["test_end"]
        st.warning("최종 검증 구간을 반복해서 보고 전략을 고치면 그 구간은 더 이상 '독립 검증'이 아닙니다. 사용 기록이 남습니다.")
    else:
        start = c[1].date_input("시작", date(2015, 1, 1))
        end = c[2].date_input("종료", P["valid_end"])
    cost_mult = c[3].slider("거래비용 배수", 0.0, 3.0, 1.0, 0.5, help="1.0 = 기본 가정(수수료+슬리피지+매도세)")
    c = st.columns(3)
    maxw = c[0].slider("단일 종목 최대 비중", 0.02, 0.5, 0.10)
    part = c[1].slider("거래대금 대비 최대 주문 비율", 0.01, 0.2, 0.05)
    scen = c[2].selectbox("상장폐지 처리", ["last_close", "conservative"], format_func=lambda x: {"last_close": "마지막 종가 청산", "conservative": "보수적(보상 근거 없으면 0원)"}[x])
    capital = st.number_input("초기 자금", value=100_000_000 if market == "KR" else 100_000, step=1_000_000 if market == "KR" else 10_000)
    st.caption(f"기본 비용({market}): {bt.Costs.for_market(market).__dict__}")
    if st.button("백테스트 실행", type="primary"):
        wf = runner.load_state(market).get("wf_runs", {})
        pred = {"ret": wf.get("wf_rank_20_expanding"), "up": wf.get("wf_up_20_expanding"), "vol": wf.get("wf_risk_20_expanding")}
        if strat.startswith("ml_") and not pred["ret"]:
            st.error("ML 전략에는 walk-forward 예측이 필요합니다. 머신러닝 연구소에서 연구 전체 실행을 먼저 하세요.")
            st.stop()
        with st.spinner("신호 생성·시뮬레이션 중…"):
            params = {"n": n, "rebalance": reb}
            if strat == "ml_confidence_cash":
                params["_recent_ic"] = S.recent_rank_ic(market, pred["ret"], 20, S.rebalance_dates(market, start, end, reb))
            reg = runner._regime_by_date(market) if strat == "ml_regime_switch" else None
            tg = S.generate_targets(market, strat, start, end, params, pred, reg)
            cons = bt.Constraints(max_weight=maxw, max_participation=part, delisting_scenario=scen)
            res = bt.run(market, tg, start, end, capital=capital, costs=bt.Costs.for_market(market, cost_mult), cons=cons, name=strat)
            bench = bt.benchmark_curves(market, start, end, capital)
        if period.startswith("최종"):
            with research_db() as db:
                db.execute("INSERT INTO test_access_log VALUES (now(), 'dashboard', 'manual backtest', ?)", [f"{strat}:{start}~{end}"])
        st.session_state["bt_res"] = (res, bench, strat, params, f"{start}~{end}")
    if "bt_res" in st.session_state:
        res, bench, strat_, params, per = st.session_state["bt_res"]
        m = res["metrics"]
        c = st.columns(6)
        c[0].metric("누적 수익률", pct(m.get("total_return")))
        c[1].metric("CAGR", pct(m.get("cagr")))
        c[2].metric("샤프", f"{m.get('sharpe') or 0:.2f}")
        c[3].metric("MDD", pct(m.get("mdd")))
        c[4].metric("거래 수", m.get("n_trades"))
        c[5].metric("총 비용", f"{m.get('total_cost', 0):,.0f}")
        eq = res["equity"]
        fig = go.Figure()
        fig.add_scatter(x=eq["date"], y=eq["equity"], name=strat_)
        for col in bench.columns:
            fig.add_scatter(x=bench.index, y=bench[col], name=col, line=dict(dash="dot"))
        fig.update_layout(height=380, margin=dict(l=10, r=10, t=30, b=10), title="자산곡선 (지수는 배당 미포함 가격지수)", legend=dict(orientation="h"))
        st.plotly_chart(fig, use_container_width=True)
        dd = eq["equity"] / eq["equity"].cummax() - 1
        st.plotly_chart(go.Figure(go.Scatter(x=eq["date"], y=dd, fill="tozeroy", name="낙폭")).update_layout(height=220, title="최대 낙폭(MDD) 추이", yaxis_tickformat=".0%", margin=dict(l=10, r=10, t=30, b=10)), use_container_width=True)
        c1, c2 = st.columns(2)
        c1.write("연도별 수익률")
        c1.dataframe(bt.yearly_returns(eq).assign(**{"return": lambda x: x["return"].map(pct)}), hide_index=True)
        boot = bt.block_bootstrap(eq)
        c2.write("통계적 불확실성 (20거래일 블록 부트스트랩)")
        c2.json(boot)
        st.write("세부 지표")
        st.json(m)
        st.write("거래 내역")
        st.dataframe(res["trades"], hide_index=True, use_container_width=True)
        if st.button("결과 저장"):
            st.success(bt.save(res, strat_, {k: v for k, v in params.items() if not k.startswith("_")}, per, note="manual"))

with t2:
    df = research_df("SELECT bt_id, created_at, strategy, params, start_date, end_date, note, metrics->>'cagr' AS cagr, metrics->>'sharpe' AS sharpe, metrics->>'mdd' AS mdd FROM backtests WHERE market=? ORDER BY created_at DESC", (market,))
    st.dataframe(df, hide_index=True, use_container_width=True)
    log = research_df("SELECT * FROM strategy_search_log WHERE market=? ORDER BY created_at DESC LIMIT 500", (market,))
    with st.expander(f"전략 탐색 기록 ({len(log)}건) — 시도한 모든 조합"):
        st.dataframe(log, hide_index=True, use_container_width=True)
disclaimer()
