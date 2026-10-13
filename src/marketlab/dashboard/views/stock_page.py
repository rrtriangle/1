from datetime import date, timedelta

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from marketlab.api import MarketData
from marketlab.dashboard.common import disclaimer, pct
from marketlab.research import analysis

st.title("개별 종목 분석")
market = st.radio("시장", ["KR", "US"], horizontal=True, format_func=lambda x: {"KR": "한국", "US": "미국"}[x])
tab1, tab2 = st.tabs(["종목 분석", "추천 후보 순위"])


@st.cache_resource(ttl=600)
def md():
    return MarketData()


with tab1:
    q = st.text_input("종목명·코드·티커", "삼성전자" if market == "KR" else "AAPL")
    res = md().search(q, 30) if q else None
    if res is not None and not res.empty:
        res = res[res["country"] == market]
    if res is None or res.empty:
        st.info("검색 결과 없음")
    else:
        sid = st.selectbox("종목", res["security_id"], format_func=lambda x: f"{res.set_index('security_id').loc[x, 'name']} ({x})")
        px = md().prices(sid, date.today() - timedelta(days=730), date.today(), adjust="all_corporate")
        if not px.empty:
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25], vertical_spacing=0.03)
            fig.add_candlestick(x=px["trade_date"], open=px["open"], high=px["high"], low=px["low"], close=px["close"], name="가격(수정)", row=1, col=1)
            fig.add_bar(x=px["trade_date"], y=px["volume"], name="거래량", row=2, col=1)
            fig.update_layout(height=480, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=10, b=10), showlegend=False)
            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"데이터 마지막 거래일: {px['trade_date'].max()} · 수정주가(조회일까지 알려진 기업행위 반영)")
        with st.spinner("최신 예측 계산 중…"):
            rep = analysis.stock_report(market, sid)
        st.markdown(analysis.describe_stock(rep))
        if rep.get("available"):
            f = rep["features"]
            c = st.columns(5)
            c[0].metric("20일 수익률", pct(f.get("ret_20")))
            c[1].metric("시장 대비 20일", pct(f.get("xret_20")))
            c[2].metric("20일 변동성", pct(f.get("rv_20"), 0))
            c[3].metric("RSI(14)", f"{f.get('rsi_14', 0):.0f}" if f.get("rsi_14") is not None else "-")
            c[4].metric("52주 고점 대비", pct(f.get("pos_high_252")))
            st.subheader("모델 검증 성적")
            st.dataframe([{"예측": k, "모델": v.get("algorithm"), "상태": v.get("status"), "학습 마지막": v.get("trained_until"),
                           "신뢰도": v.get("confidence"), "근거": v.get("why")} for k, v in rep["info"].items()], hide_index=True)
            st.caption("상태 'candidate' 는 사용자가 아직 승인하지 않은 모델입니다(머신러닝 연구소에서 승인).")

with tab2:
    c = st.columns(4)
    h = c[0].selectbox("예측 기간(거래일)", [5, 20, 60], index=1)
    mc = c[1].number_input("최소 시가총액(억원)" if market == "KR" else "최소 시가총액(백만$, 미국은 미제공)", 0, value=0)
    mv = c[2].slider("최대 20일 변동성(연)", 0.1, 2.0, 1.0)
    top = c[3].number_input("표시 개수", 10, 200, 50)
    with st.spinner("후보 계산 중…"):
        df, info = analysis.candidate_ranking(market, h, min_mcap=mc * 1e8 if market == "KR" and mc else None, max_vol=mv, top=int(top))
    i = info.get(f"rank_{h}") or {}
    if df.empty:
        st.info("예측 모델이 없습니다. 머신러닝 연구소에서 학습을 먼저 실행하세요.")
    else:
        st.write(f"모델: {i.get('model_id')} · 학습 마지막 {i.get('trained_until')} · 신뢰도 **{i.get('confidence')}** ({i.get('why')})")
        if i.get("confidence") == "판단 보류":
            st.warning("검증 구간 예측력이 약해 추천 강도를 '판단 보류'로 표시합니다.")
        st.dataframe(df, hide_index=True, use_container_width=True)
disclaimer()
