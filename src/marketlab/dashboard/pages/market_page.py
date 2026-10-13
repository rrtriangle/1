import plotly.graph_objects as go
import streamlit as st

from marketlab.dashboard.common import disclaimer, pct
from marketlab.research import analysis

st.title("시장 분석")
market = st.radio("시장", ["KR", "US"], horizontal=True, format_func=lambda x: {"KR": "한국", "US": "미국"}[x])


@st.cache_data(ttl=600, show_spinner="시장 지표 계산 중…")
def summary(m):
    sm = analysis.market_summary(m)
    return sm, analysis.describe_market(sm)


try:
    sm, text = summary(market)
except Exception as e:
    st.error(f"시장 분석을 만들 수 없습니다: {e}")
    st.stop()

fr = sm["freshness"]
if not fr["is_fresh"]:
    st.warning(f"데이터 기준일 {fr['data_date']} — {fr['sessions_behind']} 거래일 지연. 최신 시장 의견으로 보지 마세요.")
st.markdown(text)

c = st.columns(4)
r = sm["regime"]
c[0].metric("현재 국면(규칙 기준)", r["regime_rule"] or "-", help="통계 군집 번호: #%d" % r["cluster"])
c[1].metric("시장 60일 수익률", pct(r["mkt_ret_60"]))
c[2].metric("시장 변동성(연)", pct(r["mkt_rv_20"], 0), help=f"과거 대비 하위 {sm['rv_percentile']:.0%}")
c[3].metric("5일 상승 종목 비율", pct(sm.get("adv_ratio_5"), 0))

st.subheader("주요 지수")
st.dataframe([{"지수": k.split(":")[1], "기준일": v["last_date"], "값": round(v["value"], 2), "20일": pct(v["ret_20"]), "60일": pct(v["ret_60"])}
              for k, v in sm["index_trends"].items()], hide_index=True)

env = sm["env"].tail(750)
fig = go.Figure()
fig.add_scatter(x=env["d"], y=env["mkt_rv_20"], name="시장 변동성(20일, 연환산)")
fig.add_scatter(x=env["d"], y=env["mkt_ret_60"], name="시장 60일 수익률", yaxis="y2")
fig.update_layout(height=340, margin=dict(l=10, r=10, t=30, b=10), yaxis=dict(tickformat=".0%"),
                  yaxis2=dict(overlaying="y", side="right", tickformat=".0%"), legend=dict(orientation="h"))
st.plotly_chart(fig, use_container_width=True)

st.subheader("국면별 과거 통계 (이후 20거래일 시장 수익률)")
st.caption("국면 이름은 이해를 돕기 위한 규칙 기반 설명이며, '좋은/나쁜 시장'이라는 판정이 아닙니다. 과거 통계는 미래를 보장하지 않습니다.")
st.dataframe(sm["regime_stats"].rename(columns={"regime_rule": "국면", "n": "거래일 수", "fwd_ret_20": "평균 수익률", "fwd_ret_20_pos": "상승 비율"}),
             hide_index=True)
with st.expander("통계 군집(GMM) 특성 — 학습 구간으로만 적합"):
    st.dataframe(sm["cluster_profile"], hide_index=True)

st.subheader("국면별로 유리했던 전략 (선택 구간 백테스트 기준)")
adv = analysis.strategy_advice(market)
if adv.empty:
    st.info("아직 전략 탐색 결과가 없습니다. 머신러닝 연구소에서 '연구 전체 실행'을 먼저 하세요.")
else:
    cur = adv[adv["regime"] == r["regime_rule"]].sort_values("sharpe", ascending=False)
    st.dataframe(cur, hide_index=True, use_container_width=True)
    st.caption("현재 국면과 같은 과거 기간에서의 성과입니다. 국면이 비슷하다고 같은 결과가 반복된다는 보장은 없습니다.")
disclaimer()
