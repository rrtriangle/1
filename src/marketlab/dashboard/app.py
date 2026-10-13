"""market-research-lab 통합 로컬 웹앱 (데이터 관리 + 머신러닝 연구 + 백테스트 + 과거 시장 게임).

실행: marketlab dashboard   (기본: 이 컴퓨터에서만 접속 http://localhost:8501)
"""
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="market-research-lab", layout="wide", initial_sidebar_state="auto")
P = Path(__file__).parent / "views"
nav = st.navigation([
    st.Page(P / "data_page.py", title="1. 데이터 현황", icon="🗂️", default=True),
    st.Page(P / "market_page.py", title="2. 시장 분석", icon="🌐"),
    st.Page(P / "stock_page.py", title="3. 개별 종목 분석", icon="🔎"),
    st.Page(P / "lab_page.py", title="4. 머신러닝 연구소", icon="🧪"),
    st.Page(P / "backtest_page.py", title="5. 전략 백테스트", icon="📈"),
    st.Page(P / "game_page.py", title="6. 과거 시장 게임", icon="🎮"),
    st.Page(P / "system_page.py", title="7. 시스템 관리", icon="⚙️"),
])
nav.run()
