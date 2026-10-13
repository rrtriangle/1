"""대시보드 공통 유틸."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from marketlab.storage.catalog import catalog


@st.cache_data(ttl=60)
def cat_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    try:
        with catalog(read_only=True) as con:
            return con.execute(sql, list(params)).df()
    except Exception as e:
        st.warning(f"카탈로그 조회 실패: {e}")
        return pd.DataFrame()


def research_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    from marketlab.research.store import research_db
    try:
        with research_db(read_only=True) as db:
            return db.execute(sql, list(params)).df()
    except Exception as e:
        st.warning(f"연구 DB 조회 실패(학습 작업이 쓰는 중일 수 있음): {e}")
        return pd.DataFrame()


def pct(x, nd=1):
    return "-" if x is None or pd.isna(x) else f"{x * 100:.{nd}f}%"


def disclaimer():
    st.caption("이 화면은 과거 데이터에 기반한 연구·의사결정 지원 정보입니다. 미래 수익을 보장하지 않으며 실거래 주문 기능은 없습니다.")
