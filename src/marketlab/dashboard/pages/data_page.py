"""market-research-lab 로컬 관리 화면 (Streamlit).

대량 데이터를 브라우저로 보내지 않는다: 필요한 기간만 DuckDB 로 조회하고 집계·페이지네이션한다.
실행: marketlab dashboard  (또는 scripts/dashboard.command)
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import psutil
import streamlit as st

from marketlab.api import MarketData
from marketlab.collectors.base import request_stop
from marketlab.config import get_settings
from marketlab.storage.catalog import catalog, memory_connection
from marketlab.storage.diskguard import GB, assess, dir_size, is_external_volume

S = get_settings()
STD = S.paths.standardized / "daily_prices"


@st.cache_resource(ttl=300)
def md() -> MarketData:
    return MarketData()


@st.cache_data(ttl=60)
def cat_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    try:
        with catalog(read_only=True) as con:
            return con.execute(sql, list(params)).df()
    except Exception as e:
        st.warning(f"카탈로그 조회 실패(수집 작업이 쓰는 중일 수 있음): {e}")
        return pd.DataFrame()


@st.cache_data(ttl=600)
def parquet_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    con = memory_connection()
    try:
        return con.execute(sql, list(params)).df()
    finally:
        con.close()


def glob(country: str) -> str | None:
    p = STD / f"country={country}"
    return (p / "year=*" / "*.parquet").as_posix() if any(p.glob("year=*/*.parquet")) else None


def alerts() -> None:
    a = cat_df("SELECT created_at, level, kind, message FROM pending_alerts WHERE NOT acknowledged ORDER BY created_at DESC LIMIT 10")
    for r in a.itertuples():
        (st.error if r.level == "error" else st.warning)(f"{r.created_at:%Y-%m-%d %H:%M} [{r.kind}] {r.message}")
    adv = S.paths.reports / "storage_advice.md"
    if adv.exists() and "외장 SSD 필요: 예" in adv.read_text(encoding="utf-8"):
        st.error("저장공간 위험: reports/storage_advice.md 를 확인하세요 (외장 SSD 권고).")


st.title("데이터 현황 · 수집 관리")
alerts()
tabs = st.tabs(["데이터 현황", "확보 범위", "종목 조회", "수집 관리", "시스템 상태", "품질"])

# ---------------------------------------------------------------- A. 현황
with tabs[0]:
    cols = st.columns(4)
    for i, c in enumerate(("KR", "US")):
        g = glob(c)
        if not g:
            cols[i * 2].metric(f"{c} 종목 수", "0")
            cols[i * 2 + 1].metric(f"{c} 데이터 행", "0")
            continue
        r = parquet_df(f"SELECT count(*) AS n, count(DISTINCT security_id) AS s, min(trade_date) f, max(trade_date) l "
                       f"FROM read_parquet('{g}', hive_partitioning=true)").iloc[0]
        cols[i * 2].metric(f"{c} 가격 보유 증권", f"{int(r.s):,}", help=f"{r.f} ~ {r.l}")
        cols[i * 2 + 1].metric(f"{c} 데이터 행", f"{int(r.n):,}", help=f"최초 {r.f} / 마지막 {r.l}")
    c1, c2, c3 = st.columns(3)
    c1.metric("프로젝트 저장 용량", f"{dir_size(S.paths.data) / GB:.2f} GB")
    last = cat_df("SELECT max(finished_at) t FROM ingestion_runs WHERE status IN ('success','partial')")
    c2.metric("최신 업데이트", str(last.iloc[0, 0])[:16] if not last.empty else "-")
    sec = cat_df("SELECT country, status, count(*) n FROM securities GROUP BY ALL ORDER BY ALL")
    c3.metric("증권 마스터", f"{int(sec['n'].sum()) if not sec.empty else 0:,}")
    st.subheader("원천별 최근 수집 결과")
    st.dataframe(cat_df("""SELECT source, status, started_at, finished_at, items_ok, items_failed, rows_written, message
                           FROM ingestion_runs QUALIFY row_number() OVER (PARTITION BY source ORDER BY started_at DESC) = 1
                           ORDER BY source"""), use_container_width=True, hide_index=True)
    st.subheader("증권 상태")
    st.dataframe(sec, hide_index=True)

# ---------------------------------------------------------------- B. 확보 범위
with tabs[1]:
    country = st.radio("국가", ["KR", "US"], horizontal=True, key="cov_c")
    g = glob(country)
    if not g:
        st.info("데이터가 없습니다.")
    else:
        mk = parquet_df(f"""SELECT coalesce(market,'?') market, count(DISTINCT security_id) AS securities, count(*) AS n_rows
                            FROM read_parquet('{g}', hive_partitioning=true) GROUP BY 1 ORDER BY 3 DESC""")
        st.dataframe(mk, hide_index=True)
        yr = parquet_df(f"""SELECT year, count(DISTINCT security_id) AS securities, count(DISTINCT trade_date) AS trading_days, count(*) AS n_rows,
                                   avg(CASE WHEN usable THEN 1 ELSE 0 END) usable_ratio
                            FROM read_parquet('{g}', hive_partitioning=true) GROUP BY 1 ORDER BY 1""")
        fig = go.Figure()
        fig.add_bar(x=yr["year"], y=yr["securities"], name="연도별 증권 수")
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=30, b=10), title="연도별 확보 종목 수")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(yr, hide_index=True)
        stt = cat_df("""SELECT status, security_type, count(*) n, count(first_trade_date) with_prices
                        FROM securities WHERE country=? GROUP BY ALL ORDER BY n DESC""", (country,))
        st.subheader("현재 상장 / 상장폐지 구분")
        st.dataframe(stt, hide_index=True)
        gaps = cat_df("""SELECT security_id, detail FROM data_quality_issues WHERE market=? AND check_name='missing_trading_days'
                         ORDER BY detected_at DESC LIMIT 200""", (country,))
        st.subheader("데이터 부족 구간 (상장 구간 내 결측일 상위)")
        st.dataframe(gaps, hide_index=True, use_container_width=True)
        if country == "US":
            st.caption("미국: 무료 원천은 현재 상장 종목 위주이며, 과거 상장폐지 종목 전체 목록을 제공하지 않습니다 → 생존편향 주의.")

# ---------------------------------------------------------------- C. 종목 조회
with tabs[2]:
    q = st.text_input("종목명·코드·티커 검색", "삼성전자")
    if q:
        res = md().search(q, 50)
        st.dataframe(res, hide_index=True, use_container_width=True)
        if not res.empty:
            sid = st.selectbox("증권 선택", res["security_id"].tolist(),
                               format_func=lambda x: f"{x} · {res.set_index('security_id').loc[x, 'name']}")
            c1, c2, c3 = st.columns(3)
            end = c2.date_input("종료", date.today())
            start = c1.date_input("시작", end - timedelta(days=365 * 3))
            adj = c3.selectbox("가격", ["raw", "split", "all_corporate", "total_return"],
                               help="raw=당시 실제 거래가격 / 조정가격은 비교·수익률용 (조회 종료일 기준으로 알려진 기업행위만 반영)")
            px = md().prices(sid, start, end, adjust=adj)
            if px.empty:
                st.info("해당 기간 데이터 없음")
            else:
                fig = go.Figure(go.Candlestick(x=px["trade_date"], open=px["open"], high=px["high"], low=px["low"],
                                               close=px["close"], name=sid))
                fig.update_layout(height=420, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=10, b=10))
                st.plotly_chart(fig, use_container_width=True)
                page_size = 200
                pages = max(1, (len(px) - 1) // page_size + 1)
                pg = st.number_input("페이지", 1, pages, pages)
                st.dataframe(px.iloc[(pg - 1) * page_size: pg * page_size], hide_index=True, use_container_width=True)
            st.subheader("기업행위 (배당·분할·기준가 조정)")
            st.dataframe(md().corporate_actions(sid), hide_index=True, use_container_width=True)
            st.subheader("상장 이력·출처")
            st.dataframe(cat_df("SELECT * FROM listing_events WHERE security_id=? ORDER BY event_date", (sid,)), hide_index=True)
            st.dataframe(cat_df("SELECT * FROM symbol_history WHERE security_id=? ORDER BY valid_from", (sid,)), hide_index=True)
            st.subheader("품질 이슈")
            st.dataframe(cat_df("SELECT check_name, severity, trade_date, detail, resolution FROM data_quality_issues "
                                "WHERE security_id=? ORDER BY trade_date DESC LIMIT 200", (sid,)), hide_index=True)

# ---------------------------------------------------------------- D. 수집 관리
with tabs[3]:
    st.caption("작업은 백그라운드 프로세스로 실행되며 창을 닫아도 계속됩니다. 동시에 하나만 실행됩니다.")

    def launch(args: list[str]) -> None:
        log = S.paths.logs / "dashboard_jobs.log"
        with open(log, "a") as fh:
            subprocess.Popen([sys.executable, "-m", "marketlab.cli.main", *args], cwd=S.paths.root,
                             stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
        st.success(f"시작: marketlab {' '.join(args)} (로그: {log})")

    c = st.columns(6)
    if c[0].button("한국 전체 수집/재개"):
        launch(["ingest", "--market", "KR", "--mode", "full"])
    if c[1].button("미국 전체 수집/재개"):
        launch(["ingest", "--market", "US", "--mode", "full"])
    if c[2].button("지수·환율·금리"):
        launch(["ingest", "--market", "IND"])
    if c[3].button("증분 업데이트"):
        launch(["update", "--all"])
    if c[4].button("실패 재시도"):
        launch(["retry"])
    if c[5].button("수집 중지", type="primary"):
        request_stop()
        st.warning("중지 요청을 기록했습니다. 현재 항목을 마친 뒤 안전하게 멈춥니다.")
    run = cat_df("SELECT run_id, source, mode, started_at, items_total, items_ok, items_failed, rows_written, message "
                 "FROM ingestion_runs WHERE status='running' ORDER BY started_at DESC")
    if run.empty:
        st.info("실행 중인 작업 없음")
    for r in run.itertuples():
        done = (r.items_ok or 0) + (r.items_failed or 0)
        st.progress(min(1.0, done / r.items_total) if r.items_total else 0.0,
                    text=f"{r.source} {done}/{r.items_total or '?'} · 남은 {max(0, (r.items_total or 0) - done)} · {r.message}")
    st.subheader("종목 단위 상태 (재개 기준)")
    st.dataframe(cat_df("SELECT source, status, count(*) n, max(last_attempt_at) last FROM ingestion_items GROUP BY ALL ORDER BY ALL"),
                 hide_index=True)
    st.subheader("최근 실패")
    st.dataframe(cat_df("SELECT source, item_key, status, attempts, error, last_attempt_at FROM ingestion_items "
                        "WHERE status IN ('failed','blocked') ORDER BY last_attempt_at DESC LIMIT 200"), hide_index=True)
    st.subheader("실행 이력")
    st.dataframe(cat_df("SELECT * FROM ingestion_runs ORDER BY started_at DESC LIMIT 100"), hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- E. 시스템
with tabs[4]:
    vm = psutil.virtual_memory()
    a = assess()
    c = st.columns(5)
    c[0].metric("RAM 사용", f"{vm.percent:.0f}%", help=f"{vm.used / GB:.1f} / {vm.total / GB:.1f} GB")
    c[1].metric("CPU 사용", f"{psutil.cpu_percent(interval=0.3):.0f}%")
    c[2].metric("SSD 여유", f"{a.disk_free_gb:.1f} GB", help=f"전체 {a.disk_total_gb:.0f}GB")
    c[3].metric("프로젝트 사용량", f"{a.project_used_gb:.2f} GB", help=f"유효 예산 {a.budget_gb:.1f}GB")
    c[4].metric("저장 위치", "외장" if is_external_volume(S.paths.data) else "내장")
    plan = S.paths.reports / "disk_plan.md"
    if plan.exists():
        with st.expander("저장공간 계획 (disk-plan)", expanded=False):
            st.markdown(plan.read_text(encoding="utf-8"))
    adv = S.paths.reports / "storage_advice.md"
    if adv.exists():
        with st.expander("외장 SSD 권고", expanded=True):
            st.markdown(adv.read_text(encoding="utf-8"))
    st.subheader("원천 접근 점검")
    st.dataframe(cat_df("SELECT * FROM source_checks ORDER BY source"), hide_index=True)
    st.subheader("최근 오류 로그")
    logf = S.paths.logs / "marketlab.jsonl"
    if logf.exists():
        lines = logf.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        errs = [ln for ln in lines if '"level": "ERROR"' in ln or '"level": "WARNING"' in ln or "failed" in ln]
        st.code("\n".join(errs[-50:]) or "(최근 오류 없음)")

# ---------------------------------------------------------------- 품질
with tabs[5]:
    rep = S.paths.reports / "quality_report.md"
    st.markdown(rep.read_text(encoding="utf-8") if rep.exists() else "아직 검증 보고서가 없습니다. `marketlab validate` 를 실행하세요.")
