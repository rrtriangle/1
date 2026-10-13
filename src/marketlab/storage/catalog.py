"""DuckDB 메타데이터 카탈로그.

대용량 시세는 Parquet 로 두고 DuckDB 에서 직접 조회한다. 이 파일(catalog.duckdb)에는
증권 마스터, 이력, 수집 상태, 품질 문제 등 관리용 메타데이터만 담는다.
DuckDB 는 프로세스 간 단일 writer 이므로 연결은 짧게 열고 닫는다.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb

from ..config import Settings, get_settings

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS securities (
    security_id        VARCHAR PRIMARY KEY,   -- 불변 내부 ID (예: KR-005930-19950502)
    country            VARCHAR NOT NULL,      -- KR / US
    market             VARCHAR,               -- KOSPI, KOSDAQ, KONEX, NASDAQ, NYSE, NYSE American ...
    exchange           VARCHAR,
    currency           VARCHAR,
    security_type      VARCHAR,               -- common, preferred, etf, adr, reit, spac, ...
    type_method        VARCHAR,               -- reported / heuristic / unknown
    isin               VARCHAR,
    cik                VARCHAR,               -- SEC 기업 단위 ID (증권 단위 아님)
    name               VARCHAR,               -- 가장 최근 확인된 이름
    first_trade_date   DATE,
    last_trade_date    DATE,
    listing_date       DATE,
    delisting_date     DATE,
    status             VARCHAR,               -- listed / delisted / unknown
    delisting_reason   VARCHAR,
    id_confidence      VARCHAR,               -- observed / inferred_reuse / unverified
    sources            VARCHAR,
    updated_at         TIMESTAMP
);

CREATE TABLE IF NOT EXISTS symbol_history (
    security_id   VARCHAR NOT NULL,
    symbol_type   VARCHAR NOT NULL,           -- krx_code / ticker / yahoo / stooq / isin
    symbol        VARCHAR NOT NULL,
    exchange      VARCHAR,
    name          VARCHAR,
    valid_from    DATE,
    valid_to      DATE,
    source        VARCHAR,
    confidence    VARCHAR,                    -- observed / reported / inferred
    PRIMARY KEY (security_id, symbol_type, symbol, name, valid_from)
);

CREATE TABLE IF NOT EXISTS listing_events (
    security_id   VARCHAR,
    symbol        VARCHAR,
    event_type    VARCHAR NOT NULL,           -- listed / delisted / suspended / merged / renamed / code_reused
    event_date    DATE,
    detail        VARCHAR,
    to_symbol     VARCHAR,
    to_name       VARCHAR,
    source        VARCHAR,
    confidence    VARCHAR
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    security_id   VARCHAR NOT NULL,
    ex_date       DATE NOT NULL,
    action_type   VARCHAR NOT NULL,           -- split / reverse_split / dividend / base_price_adjustment ...
    ratio         DOUBLE,                     -- 가격 조정 계수(이전 가격에 곱함) 또는 분할 비율
    cash_amount   DOUBLE,
    currency      VARCHAR,
    known_from    DATE,                       -- 이 정보가 시장에 알려졌다고 간주할 최초 일자
    source        VARCHAR NOT NULL,
    confidence    VARCHAR,                    -- reported / inferred_unverified
    detail        VARCHAR,
    PRIMARY KEY (security_id, ex_date, action_type, source)
);

CREATE TABLE IF NOT EXISTS source_manifest (
    source        VARCHAR NOT NULL,
    file_key      VARCHAR NOT NULL,
    remote_url    VARCHAR,
    remote_sha    VARCHAR,                    -- 원격 측 식별 해시 (git blob sha 등)
    local_path    VARCHAR,
    size_bytes    BIGINT,
    sha256        VARCHAR,
    row_count     BIGINT,
    fetched_at    TIMESTAMP,
    version       VARCHAR,
    status        VARCHAR,
    PRIMARY KEY (source, file_key)
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id        VARCHAR PRIMARY KEY,
    source        VARCHAR,
    market        VARCHAR,
    mode          VARCHAR,
    status        VARCHAR,                    -- running / success / partial / failed / stopped / blocked
    started_at    TIMESTAMP,
    finished_at   TIMESTAMP,
    items_total   BIGINT,
    items_ok      BIGINT,
    items_failed  BIGINT,
    rows_written  BIGINT,
    bytes_written BIGINT,
    message       VARCHAR,
    pid           INTEGER
);

CREATE TABLE IF NOT EXISTS ingestion_items (
    source            VARCHAR NOT NULL,
    item_key          VARCHAR NOT NULL,       -- 종목·파일 단위 작업 키
    status            VARCHAR,                -- ok / failed / empty / blocked / pending
    first_date        DATE,
    last_success_date DATE,
    last_attempt_at   TIMESTAMP,
    attempts          INTEGER,
    rows              BIGINT,
    error             VARCHAR,
    PRIMARY KEY (source, item_key)
);

CREATE TABLE IF NOT EXISTS data_quality_issues (
    run_id        VARCHAR,
    check_name    VARCHAR NOT NULL,
    severity      VARCHAR,                    -- info / warn / error
    market        VARCHAR,
    security_id   VARCHAR,
    symbol        VARCHAR,
    trade_date    DATE,
    source        VARCHAR,
    detail        VARCHAR,
    resolution    VARCHAR,                    -- kept / supplemented / quarantined / restricted / open
    detected_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trading_calendars (
    market        VARCHAR NOT NULL,           -- KR / US
    trade_date    DATE NOT NULL,
    tz            VARCHAR,
    source        VARCHAR,                    -- exchange_calendars / observed / both
    PRIMARY KEY (market, trade_date)
);

CREATE TABLE IF NOT EXISTS pending_alerts (
    created_at    TIMESTAMP,
    level         VARCHAR,
    kind          VARCHAR,
    message       VARCHAR,
    acknowledged  BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS source_checks (
    source        VARCHAR PRIMARY KEY,
    checked_at    TIMESTAMP,
    reachable     BOOLEAN,
    http_status   INTEGER,
    detail        VARCHAR
);
"""


def _process_tmp(s: Settings):
    """프로세스별 DuckDB 임시 폴더 (여러 프로세스가 동시에 디스크로 넘길 때 파일 이름 충돌 방지).
    종료된 프로세스의 폴더는 정리한다."""
    import os
    import shutil

    base = s.paths.tmp / "duckdb"
    base.mkdir(parents=True, exist_ok=True)
    mine = base / f"pid-{os.getpid()}"
    if not mine.exists():
        for d in base.glob("pid-*"):
            try:
                pid = int(d.name[4:])
                os.kill(pid, 0)
            except (ValueError, ProcessLookupError):
                shutil.rmtree(d, ignore_errors=True)
            except PermissionError:
                pass
        mine.mkdir(parents=True, exist_ok=True)
    return mine


def configure_connection(con: duckdb.DuckDBPyConnection, settings: Settings | None = None) -> None:
    """초기 안전 설정. 8GB RAM 맥북 기준."""
    s = settings or get_settings()
    d = s.get("duckdb", {}) or {}
    tmp = _process_tmp(s)
    con.execute(f"SET memory_limit='{d.get('memory_limit', '2GB')}'")
    con.execute(f"SET threads={int(d.get('threads', 2))}")
    con.execute(f"SET preserve_insertion_order={'true' if d.get('preserve_insertion_order') else 'false'}")
    con.execute(f"SET temp_directory='{tmp.as_posix()}'")
    con.execute(f"SET max_temp_directory_size='{d.get('max_temp_directory_size', '4GB')}'")
    con.execute("SET TimeZone='UTC'")
    con.execute("SET enable_progress_bar=false")


def memory_connection(settings: Settings | None = None) -> duckdb.DuckDBPyConnection:
    """Parquet 직접 조회용 인메모리 연결 (파일 잠금 없음)."""
    con = duckdb.connect(":memory:")
    configure_connection(con, settings)
    return con


@contextmanager
def catalog(read_only: bool = False, settings: Settings | None = None,
            retries: int = 20) -> Iterator[duckdb.DuckDBPyConnection]:
    """카탈로그 연결. 다른 프로세스가 쓰는 중이면 잠시 기다렸다 재시도한다."""
    s = settings or get_settings()
    path: Path = s.paths.catalog_db
    path.parent.mkdir(parents=True, exist_ok=True)
    if read_only and not path.exists():
        read_only = False
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            con = duckdb.connect(path.as_posix(), read_only=read_only)
            break
        except duckdb.IOException as e:  # 잠금 충돌
            last_err = e
            time.sleep(min(0.25 * (attempt + 1), 3.0))
    else:
        raise RuntimeError(f"카탈로그 DB 잠금 해제를 기다리다 실패: {last_err}")
    try:
        configure_connection(con, s)
        if not read_only:
            con.execute(SCHEMA_SQL)
        yield con
    finally:
        con.close()


def init_catalog() -> None:
    with catalog() as con:
        con.execute("SELECT 1")
