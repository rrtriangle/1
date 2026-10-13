from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from marketlab.dashboard.common import pct
from marketlab.research import game as G

st.title("과거 시장 투자 게임")
st.caption("과거 날짜로 돌아가 그날까지 공개된 정보만 보고 투자합니다. 주문은 다음 거래일 시가에 체결되며 거래비용이 반영됩니다.")

with st.sidebar:
    st.subheader("게임")
    games = G.list_games()
    if not games.empty:
        gid = st.selectbox("저장된 게임 불러오기", ["(선택)"] + games["game_id"].tolist(),
                           format_func=lambda x: x if x == "(선택)" else f"{games.set_index('game_id').loc[x, 'name']}")
        if gid != "(선택)" and st.button("불러오기"):
            st.session_state["game"] = G.load(gid)
    with st.form("new"):
        mk = st.selectbox("시장", ["KR", "US"])
        sd = st.date_input("시작일", date(2008, 9, 1), min_value=date(1996, 1, 1), max_value=date.today())
        cap = st.number_input("초기 투자금", value=10_000_000 if mk == "KR" else 10_000)
        ai = st.checkbox("AI 투자자와 대결", True)
        if st.form_submit_button("새 게임 시작"):
            st.session_state["game"] = G.new_game(mk, sd, float(cap), with_ai=ai)

g = st.session_state.get("game")
if not g:
    st.info("왼쪽에서 새 게임을 시작하거나 저장된 게임을 불러오세요.")
    st.stop()

for lim in g.limits:
    st.caption("⚠️ " + lim)
st.caption("🤖 " + g.ai_note)
summ = G.summary(g)
c = st.columns(5)
c[0].metric("오늘(게임 날짜)", g.current_date)
c[1].metric("내 자산", f"{summ['user']['equity']:,.0f}", pct(summ["user"]["return"]))
c[2].metric("현금", f"{g.user.cash:,.0f}")
if "ai" in summ:
    c[3].metric("AI 자산", f"{summ['ai']['equity']:,.0f}", pct(summ["ai"]["return"]))
if "benchmark_return" in summ:
    c[4].metric("시장지수", pct(summ["benchmark_return"]))

b = st.columns(4)
if b[0].button("하루 진행 ▶"):
    st.session_state["game"] = G.advance(g, 1); st.rerun()
if b[1].button("일주일 진행 ⏩"):
    st.session_state["game"] = G.advance(g, 5); st.rerun()
if b[2].button("한 달 진행 ⏭"):
    st.session_state["game"] = G.advance(g, 21); st.rerun()
if g.orders:
    b[3].write(f"대기 주문 {len(g.orders)}건 (다음 거래일 시가 체결)")

left, right = st.columns([3, 2])
with left:
    q = st.text_input("종목 검색 (게임 날짜 기준 상장 종목만)", "")
    u = G.visible_universe(g, q).head(200)
    if u.empty:
        st.info("검색 결과 없음")
    else:
        sid = st.selectbox("종목", u["security_id"], format_func=lambda x: f"{u.set_index('security_id').loc[x, 'name']} ({u.set_index('security_id').loc[x, 'symbol']})")
        adj = st.toggle("수정주가(게임 날짜까지 알려진 기업행위만 반영)", False)
        ch = G.visible_chart(g, sid, adjusted=adj)
        if not ch.empty:
            fig = go.Figure(go.Candlestick(x=ch["trade_date"], open=ch["open"], high=ch["high"], low=ch["low"], close=ch["close"]))
            fig.update_layout(height=380, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig, use_container_width=True)
            last = ch.iloc[-1]
            st.write(f"게임 날짜 종가: **{last['close']:,.2f}** (당시 실제 가격) · 이후 데이터는 보이지 않습니다.")
        with st.form("order"):
            side = st.radio("주문", ["BUY", "SELL"], horizontal=True, format_func=lambda x: {"BUY": "매수", "SELL": "매도"}[x])
            qty = st.number_input("수량(주)", 1, 10_000_000, 10)
            if st.form_submit_button("주문 (다음 거래일 시가)"):
                try:
                    G.place_order(g, side, sid, int(qty)); st.success("주문 접수")
                except ValueError as e:
                    st.error(str(e))
with right:
    st.subheader("내 보유 종목")
    st.dataframe(G.holdings(g, "user"), hide_index=True)
    if g.ai:
        st.subheader("AI 보유 종목")
        st.dataframe(G.holdings(g, "ai"), hide_index=True)

eq = pd.DataFrame(g.user.equity)
fig = go.Figure(go.Scatter(x=eq["date"], y=eq["equity"], name="나"))
if g.ai:
    ae = pd.DataFrame(g.ai.equity)
    fig.add_scatter(x=ae["date"], y=ae["equity"], name="AI")
fig.update_layout(height=280, title="자산 추이", margin=dict(l=10, r=10, t=30, b=10))
st.plotly_chart(fig, use_container_width=True)
with st.expander("거래 기록"):
    st.dataframe(pd.DataFrame(g.user.trades), hide_index=True)
st.caption(f"게임 ID {g.game_id} — 진행할 때마다 자동 저장됩니다.")
