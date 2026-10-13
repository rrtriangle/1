"""한국 일봉 표준화: marcap 원본 → daily_prices_standardized (country=KR, 연도 파티션).

설계
- 연도별 원본 파일을 하나씩 DuckDB 로 스트리밍 처리 (전체를 pandas 에 올리지 않음).
- 종목코드는 불변 ID 가 아니다. 코드별 거래일 공백·상장폐지 이력으로 '상장 구간(episode)'을 나누고
  security_id = KR-<코드>-<구간 첫 관측일> 로 부여한다. 재사용 판단은 inferred 로 표시.
- 가격은 '당시 실제 거래가격(raw)' 그대로 둔다. 수정주가는 API 에서 point-in-time 으로 계산한다.
- KRX 의 '전일대비(Changes)'는 기준가 대비이므로 (Close - Changes) ≠ 전일종가 이면
  거래소가 기준가를 조정한 날(분할·병합·무상증자·유상증자 권리락 등)로 추정할 수 있다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..collectors import krx_cache, marcap
from ..config import get_settings
from ..logging_setup import get_logger, log_event
from ..storage.catalog import catalog, memory_connection
from ..storage.parquet_io import atomic_replace

log = get_logger("kr_transform")

REUSE_GAP_DAYS = 365          # 이 이상 거래 공백 후 재등장하면 다른 증권으로 본다
NAME_GAP_DAYS = 90            # 공백 + 이름 변경이면 다른 증권으로 본다


def std_dir() -> Path:
    return get_settings().paths.standardized / "daily_prices" / "country=KR"


def raw_glob() -> str:
    return (marcap.raw_dir() / "marcap-*.parquet").as_posix()


def available_years() -> list[int]:
    return sorted(int(p.stem.split("-")[1]) for p in marcap.raw_dir().glob("marcap-*.parquet"))


def kr_final_cutoff(now: datetime | None = None) -> date:
    """이 날짜 이하의 일봉은 확정으로 본다."""
    cfg = get_settings().get("update.finalize_after.KR", {}) or {}
    tz = ZoneInfo(cfg.get("tz", "Asia/Seoul"))
    now = (now or datetime.now(tz)).astimezone(tz)
    hh, mm = map(int, str(cfg.get("close", "15:30")).split(":"))
    final_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(minutes=int(cfg.get("delay_min", 180)))
    return now.date() if now >= final_at else now.date() - timedelta(days=1)


def _raw_select(path_sql: str) -> str:
    """연도별로 다른 물리 타입(string/large_string, ns/us)을 통일하는 선택문."""
    return f"""
        SELECT
            lpad(CAST(Code AS VARCHAR), 6, '0')        AS symbol,
            CAST(Date AS DATE)                         AS trade_date,
            CAST(Name AS VARCHAR)                      AS name,
            CAST(Market AS VARCHAR)                    AS market,
            CAST(MarketId AS VARCHAR)                  AS market_id,
            nullif(nullif(CAST(Dept AS VARCHAR), 'nan'), '') AS dept,
            CAST(Open AS DOUBLE) AS open, CAST(High AS DOUBLE) AS high,
            CAST(Low AS DOUBLE) AS low, CAST(Close AS DOUBLE) AS close,
            CAST(Volume AS DOUBLE) AS volume, CAST(Amount AS DOUBLE) AS amount,
            CAST(Changes AS DOUBLE) AS change, CAST(ChangeCode AS VARCHAR) AS change_code,
            CAST(ChangesRatio AS DOUBLE) AS change_ratio,
            CAST(Marcap AS DOUBLE) AS market_cap, CAST(Stocks AS BIGINT) AS shares_outstanding,
            CAST(Rank AS INTEGER) AS mcap_rank,
            filename AS source_file
        FROM read_parquet({path_sql}, filename=true, union_by_name=true)
    """


def build_episodes(con) -> None:
    """모든 연도의 (코드, 거래일)로 상장 구간을 계산해 kr_episodes 임시 테이블을 만든다."""
    delist = krx_cache.latest_snapshot("delisting")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE obs AS
        SELECT DISTINCT lpad(CAST(Code AS VARCHAR), 6, '0') AS symbol, CAST(Date AS DATE) AS d,
               CAST(Name AS VARCHAR) AS name
        FROM read_parquet('{raw_glob()}', union_by_name=true)
    """)
    if delist:
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE delist AS
            SELECT lpad(CAST(Symbol AS VARCHAR), 6, '0') AS symbol,
                   try_cast(DelistingDate AS DATE) AS delist_date
            FROM read_csv('{delist.as_posix()}', header=true, all_varchar=true)
            WHERE try_cast(DelistingDate AS DATE) IS NOT NULL
        """)
    else:
        con.execute("CREATE OR REPLACE TEMP TABLE delist (symbol VARCHAR, delist_date DATE)")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE obs_gap AS
        SELECT symbol, d, name,
               lag(d) OVER w AS prev_d, lag(name) OVER w AS prev_name
        FROM obs WINDOW w AS (PARTITION BY symbol ORDER BY d)
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE obs_break AS
        SELECT g.*,
            CASE
              WHEN prev_d IS NULL THEN 1
              WHEN d - prev_d > {REUSE_GAP_DAYS} THEN 1
              WHEN d - prev_d > {NAME_GAP_DAYS} AND name <> prev_name THEN 1
              WHEN EXISTS (SELECT 1 FROM delist x WHERE x.symbol = g.symbol
                           AND x.delist_date >= g.prev_d AND x.delist_date < g.d
                           AND g.d - g.prev_d > 5) THEN 1
              ELSE 0 END AS is_break
        FROM obs_gap g
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE kr_episodes AS
        WITH e AS (
            SELECT symbol, d, name, sum(is_break) OVER (PARTITION BY symbol ORDER BY d) AS episode_no
            FROM obs_break
        )
        SELECT symbol, episode_no, min(d) AS start_date, max(d) AS end_date, count(*) AS n_days,
               arg_max(name, d) AS last_name, arg_min(name, d) AS first_name,
               'KR-' || symbol || '-' || strftime(min(d), '%Y%m%d') AS security_id,
               CASE WHEN episode_no > 1 THEN 'inferred_reuse' ELSE 'observed' END AS id_confidence
        FROM e GROUP BY symbol, episode_no
    """)


def standardize_year(con, year: int, cutoff: date, big_move: float) -> dict:
    files = [marcap.local_path(y) for y in (year - 1, year) if marcap.local_path(y).exists()]
    path_sql = "[" + ",".join(f"'{p.as_posix()}'" for p in files) + "]"
    con.execute(f"CREATE OR REPLACE TEMP TABLE yr_raw AS {_raw_select(path_sql)}")
    dup = con.execute(f"""
        SELECT count(*) FROM (SELECT symbol, trade_date FROM yr_raw WHERE year(trade_date) = {year}
                              GROUP BY ALL HAVING count(*) > 1)""").fetchone()[0]
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE yr_std AS
        WITH r AS (
            SELECT *, row_number() OVER (PARTITION BY symbol, trade_date ORDER BY source_file DESC) AS dup_rank
            FROM yr_raw
        ), d AS (SELECT * EXCLUDE (dup_rank) FROM r WHERE dup_rank = 1),
        w AS (
            SELECT d.*, e.security_id,
                   lag(close) OVER (PARTITION BY e.security_id ORDER BY trade_date) AS prev_close,
                   lag(shares_outstanding) OVER (PARTITION BY e.security_id ORDER BY trade_date) AS prev_shares
            FROM d JOIN kr_episodes e
              ON d.symbol = e.symbol AND d.trade_date BETWEEN e.start_date AND e.end_date
        )
        SELECT
            security_id, 'KR' AS country, market, market_id, symbol, name, trade_date,
            'KRW' AS currency, 'marcap' AS source, 'raw' AS price_basis,
            open, high, low, close, CAST(volume AS BIGINT) AS volume, amount,
            market_cap, shares_outstanding, change, change_code, change_ratio, mcap_rank, dept,
            prev_close, close - change AS base_price,
            CASE WHEN prev_close > 0 AND change IS NOT NULL
                      AND abs((close - change) - prev_close) > greatest(prev_close * 0.001, 1.0)
                 THEN (close - change) / prev_close END AS base_adj_factor,
            volume > 0 AS is_traded,
            trade_date <= DATE '{cutoff.isoformat()}' AS is_final,
            list_filter([
                CASE WHEN (close IS NULL OR close <= 0) AND volume > 0 THEN 'nonpositive_close' END,
                CASE WHEN (close IS NULL OR close <= 0) AND coalesce(volume, 0) = 0 THEN 'no_price' END,
                CASE WHEN volume < 0 OR amount < 0 THEN 'negative_volume_or_amount' END,
                CASE WHEN volume > 0 AND open > 0 AND (high < greatest(open, close, low)
                          OR low > least(open, close, high)) THEN 'ohlc_inconsistent' END,
                CASE WHEN volume > 0 AND (open <= 0 OR high <= 0 OR low <= 0) THEN 'zero_ohlc_with_volume' END,
                CASE WHEN volume = 0 THEN 'no_trade' END,
                CASE WHEN volume > 0 AND close > 0 AND amount IS NOT NULL
                          AND (amount < volume * low * 0.5 OR amount > volume * high * 2) AND low > 0
                     THEN 'amount_out_of_range' END,
                CASE WHEN prev_close > 0 AND change IS NOT NULL
                      AND abs((close - change) - prev_close) > greatest(prev_close * 0.001, 1.0)
                     THEN 'base_price_adjusted' END,
                CASE WHEN prev_close > 0 AND abs(close / prev_close - 1) > {big_move}
                      AND NOT (change IS NOT NULL AND abs((close - change) - prev_close) > greatest(prev_close * 0.001, 1.0))
                     THEN 'big_move_unexplained' END
            ], x -> x IS NOT NULL) AS quality_flags,
            source_file,
            now() AS ingested_at
        FROM w
        WHERE year(trade_date) = {year}
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE yr_out AS
        SELECT *,
            CASE
              WHEN list_has_any(quality_flags, ['nonpositive_close', 'negative_volume_or_amount', 'ohlc_inconsistent']) THEN 'X'
              WHEN list_has_any(quality_flags, ['big_move_unexplained', 'zero_ohlc_with_volume', 'amount_out_of_range']) THEN 'C'
              WHEN list_has_any(quality_flags, ['base_price_adjusted', 'no_trade', 'no_price']) THEN 'B'
              ELSE 'A' END AS quality_grade
        FROM yr_std
    """)
    con.execute("ALTER TABLE yr_out ADD COLUMN usable BOOLEAN")
    # 가격이 한 번도 형성되지 않은 상장 종목(close=0, 거래 없음)은 오류는 아니지만 가격으로 쓸 수 없다
    con.execute("UPDATE yr_out SET usable = (quality_grade <> 'X' AND is_final AND NOT list_contains(quality_flags, 'no_price'))")
    out = std_dir() / f"year={year}" / "part-0.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(".part-0.parquet.tmp")
    con.execute(f"COPY (SELECT * FROM yr_out ORDER BY security_id, trade_date) TO '{tmp.as_posix()}' "
                f"(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 200000)")
    atomic_replace(tmp, out)
    stats = con.execute("""
        SELECT count(*), count(DISTINCT security_id), min(trade_date), max(trade_date),
               sum(CASE WHEN quality_grade='X' THEN 1 ELSE 0 END),
               sum(CASE WHEN quality_grade='C' THEN 1 ELSE 0 END),
               sum(CASE WHEN base_adj_factor IS NOT NULL THEN 1 ELSE 0 END)
        FROM yr_out""").fetchone()
    return {"year": year, "rows": stats[0], "securities": stats[1], "min": stats[2], "max": stats[3],
            "grade_x": stats[4], "grade_c": stats[5], "base_adj": stats[6], "duplicates": dup,
            "bytes": out.stat().st_size}


def _record_issues(con, run_id: str, years: list[int]) -> None:
    """경고·오류 등급 행과 중복을 data_quality_issues 에 기록 (해당 연도는 교체)."""
    if not years:
        return
    glob = (std_dir() / "year=*" / "*.parquet").as_posix()
    yr_list = ",".join(str(y) for y in years)
    issues = con.execute(f"""
        SELECT security_id, symbol, trade_date, unnest(quality_flags) AS flag, close, prev_close
        FROM read_parquet('{glob}', hive_partitioning=true)
        WHERE year IN ({yr_list}) AND quality_grade IN ('X', 'C')
    """).fetchall()
    rows = []
    for sid, sym, d, flag, close, prev in issues:
        if flag in ("no_trade", "base_price_adjusted", "no_price"):
            continue
        sev = "error" if flag in ("nonpositive_close", "negative_volume_or_amount", "ohlc_inconsistent") else "warn"
        res = "restricted" if sev == "error" else "kept"
        rows.append((run_id, flag, sev, "KR", sid, sym, d, "marcap", f"close={close} prev_close={prev}", res))
    import pandas as pd

    with catalog() as cat:
        cat.execute(f"DELETE FROM data_quality_issues WHERE market='KR' AND source='marcap' "
                    f"AND year(trade_date) IN ({yr_list})")
        if rows:
            cat.register("iss_df", pd.DataFrame(rows, columns=[
                "run_id", "check_name", "severity", "market", "security_id", "symbol", "trade_date",
                "source", "detail", "resolution"]))
            cat.execute("INSERT INTO data_quality_issues SELECT *, now() FROM iss_df")


def _record_corporate_actions(con) -> int:
    """기준가 조정일을 추정 기업행위로 기록. 상장주식수 변화로 분할/병합 여부를 분류."""
    glob = (std_dir() / "year=*" / "*.parquet").as_posix()
    tbl = con.execute(f"""
        WITH s AS (
            SELECT security_id, trade_date, base_adj_factor AS f, shares_outstanding AS sh,
                   lag(shares_outstanding) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev_sh,
                   prev_close, base_price
            FROM read_parquet('{glob}', hive_partitioning=true)
        )
        SELECT security_id, trade_date AS ex_date,
               CASE WHEN sh > 0 AND prev_sh > 0 AND f > 0 AND abs(sh / prev_sh * f - 1) < 0.03
                    THEN CASE WHEN f < 1 THEN 'split' ELSE 'reverse_split' END
                    ELSE 'base_price_adjustment' END AS action_type,
               f AS ratio, NULL::DOUBLE AS cash_amount, 'KRW' AS currency, trade_date AS known_from,
               'marcap_inferred' AS source, 'inferred_unverified' AS confidence,
               'prev_close=' || prev_close || ' base=' || base_price || ' shares ' || coalesce(prev_sh::VARCHAR, '?')
                   || '->' || coalesce(sh::VARCHAR, '?') AS detail
        FROM s WHERE f IS NOT NULL
    """).fetch_arrow_table()
    with catalog() as cat:
        cat.register("ca_df", tbl)
        cat.execute("DELETE FROM corporate_actions WHERE source='marcap_inferred'")
        cat.execute("INSERT OR REPLACE INTO corporate_actions SELECT * FROM ca_df")
    return tbl.num_rows


def build_security_master(con) -> dict:
    """securities / symbol_history / listing_events (KR) 갱신."""
    listing = krx_cache.latest_snapshot("krx")
    desc = krx_cache.latest_snapshot("desc")
    delist = krx_cache.latest_snapshot("delisting")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE cur_list AS
        SELECT lpad(Code, 6, '0') AS symbol, ISU_CD AS isin, Name AS name, Market AS market
        FROM {"read_csv('" + listing.as_posix() + "', header=true, all_varchar=true)" if listing else "(SELECT NULL::VARCHAR Code, NULL::VARCHAR ISU_CD, NULL::VARCHAR Name, NULL::VARCHAR Market WHERE false)"}
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE desc_list AS
        SELECT lpad(Code, 6, '0') AS symbol, try_cast(ListingDate AS DATE) AS listing_date, Industry AS industry
        FROM {"read_csv('" + desc.as_posix() + "', header=true, all_varchar=true)" if desc else "(SELECT NULL::VARCHAR Code, NULL::VARCHAR ListingDate, NULL::VARCHAR Industry WHERE false)"}
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE delist_full AS
        SELECT lpad(Symbol, 6, '0') AS symbol, Name AS name, Market AS market, SecuGroup AS secu_group,
               Kind AS kind, try_cast(ListingDate AS DATE) AS listing_date,
               try_cast(DelistingDate AS DATE) AS delist_date, Reason AS reason,
               nullif(ToSymbol, '') AS to_symbol, nullif(ToName, '') AS to_name
        FROM {"read_csv('" + delist.as_posix() + "', header=true, all_varchar=true)" if delist else "(SELECT NULL::VARCHAR Symbol, NULL::VARCHAR Name, NULL::VARCHAR Market, NULL::VARCHAR SecuGroup, NULL::VARCHAR Kind, NULL::VARCHAR ListingDate, NULL::VARCHAR DelistingDate, NULL::VARCHAR Reason, NULL::VARCHAR ToSymbol, NULL::VARCHAR ToName WHERE false)"}
    """)
    last_market_day = con.execute(f"SELECT max(CAST(Date AS DATE)) FROM read_parquet('{raw_glob()}', union_by_name=true)").fetchone()[0]
    glob = (std_dir() / "year=*" / "*.parquet").as_posix()
    # 구간별 최근 시장 구분
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE ep_market AS
        SELECT security_id, arg_max(market, trade_date) AS market
        FROM read_parquet('{glob}', hive_partitioning=true) GROUP BY security_id
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE kr_securities AS
        WITH dl AS (
            -- 구간 종료일과 가장 가까운 상장폐지 기록 매칭
            SELECT e.security_id, d.*,
                   row_number() OVER (PARTITION BY e.security_id ORDER BY abs(d.delist_date - e.end_date)) AS rk
            FROM kr_episodes e JOIN delist_full d ON d.symbol = e.symbol
             AND d.delist_date BETWEEN e.end_date - INTERVAL 40 DAY AND e.end_date + INTERVAL 400 DAY
        )
        SELECT
            e.security_id, 'KR' AS country,
            coalesce(m.market, dl.market) AS market, 'KRX' AS exchange, 'KRW' AS currency,
            CASE
              WHEN regexp_matches(e.last_name, '\\d신\\)?$|신\\)$') THEN 'new_shares'
              WHEN dl.kind = '보통주' THEN 'common'
              WHEN dl.kind IN ('종류주권', '우선주') THEN 'preferred'
              WHEN e.last_name LIKE '%스팩%' OR e.last_name ILIKE '%SPAC%' THEN 'spac'
              WHEN e.last_name LIKE '%리츠%' OR e.last_name ILIKE '%REIT%' THEN 'reit'
              WHEN right(e.symbol, 1) <> '0' THEN 'preferred'
              ELSE 'common' END AS security_type,
            CASE WHEN dl.kind IN ('보통주', '종류주권', '우선주') THEN 'reported' ELSE 'heuristic' END AS type_method,
            CASE WHEN e.end_date >= DATE '{last_market_day}' - INTERVAL 10 DAY THEN cl.isin END AS isin,
            NULL::VARCHAR AS cik,
            e.last_name AS name, e.start_date AS first_trade_date, e.end_date AS last_trade_date,
            coalesce(dl.listing_date, CASE WHEN e.end_date >= DATE '{last_market_day}' - INTERVAL 10 DAY THEN ds.listing_date END) AS listing_date,
            dl.delist_date AS delisting_date,
            CASE WHEN dl.delist_date IS NOT NULL AND dl.delist_date <= DATE '{last_market_day}' THEN 'delisted'
                 WHEN regexp_matches(e.last_name, '\\d신\\)?$|신\\)$')
                      AND e.end_date < DATE '{last_market_day}' - INTERVAL 10 DAY THEN 'merged'
                 WHEN cl.symbol IS NOT NULL AND e.end_date >= DATE '{last_market_day}' - INTERVAL 10 DAY THEN 'listed'
                 WHEN e.end_date >= DATE '{last_market_day}' - INTERVAL 10 DAY THEN 'listed'
                 ELSE 'unknown' END AS status,
            CASE WHEN dl.reason IS NOT NULL THEN dl.reason
                 WHEN regexp_matches(e.last_name, '\\d신\\)?$|신\\)$')
                      AND e.end_date < DATE '{last_market_day}' - INTERVAL 10 DAY
                 THEN '신주 상장분 → 구주 병합 추정 (배당기산일 차이 해소)' END AS delisting_reason,
            e.id_confidence,
            'marcap' || CASE WHEN dl.symbol IS NOT NULL THEN ',krx_delisting' ELSE '' END
                     || CASE WHEN cl.symbol IS NOT NULL THEN ',krx_listing' ELSE '' END AS sources,
            now() AS updated_at,
            dl.to_symbol, dl.to_name
        FROM kr_episodes e
        LEFT JOIN ep_market m USING (security_id)
        LEFT JOIN (SELECT * FROM dl WHERE rk = 1) dl USING (security_id)
        LEFT JOIN cur_list cl ON cl.symbol = e.symbol AND e.end_date >= DATE '{last_market_day}' - INTERVAL 10 DAY
        LEFT JOIN desc_list ds ON ds.symbol = e.symbol
    """)
    # 이름·시장 변경 이력 (연속 구간 압축)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE kr_symhist AS
        WITH p AS (
            SELECT security_id, symbol, name, market, trade_date,
                   CASE WHEN name IS DISTINCT FROM lag(name) OVER w OR market IS DISTINCT FROM lag(market) OVER w
                        THEN 1 ELSE 0 END AS chg
            FROM read_parquet('{glob}', hive_partitioning=true)
            WINDOW w AS (PARTITION BY security_id ORDER BY trade_date)
        ), g AS (SELECT *, sum(chg) OVER (PARTITION BY security_id ORDER BY trade_date) AS grp FROM p)
        SELECT security_id, 'krx_code' AS symbol_type, symbol, market AS exchange, name,
               min(trade_date) AS valid_from, max(trade_date) AS valid_to,
               'marcap' AS source, 'observed' AS confidence
        FROM g GROUP BY security_id, symbol, name, market, grp
    """)
    with catalog() as cat:
        cat.register("kr_sec_df", con.execute("SELECT * EXCLUDE (to_symbol, to_name) FROM kr_securities").fetch_arrow_table())
        cat.execute("DELETE FROM securities WHERE country='KR'")
        cat.execute("INSERT INTO securities SELECT * FROM kr_sec_df")
        cat.register("kr_sym_df", con.execute("""
            SELECT security_id, symbol_type, symbol, exchange, name, valid_from, valid_to, source, confidence
            FROM kr_symhist
            UNION ALL
            SELECT security_id, 'isin', isin, exchange, name, first_trade_date, NULL, 'krx_listing', 'reported'
            FROM kr_securities WHERE isin IS NOT NULL
        """).fetch_arrow_table())
        cat.execute("DELETE FROM symbol_history WHERE security_id LIKE 'KR-%'")
        cat.execute("INSERT OR IGNORE INTO symbol_history SELECT * FROM kr_sym_df")
        cat.register("kr_ev_df", con.execute(f"""
            SELECT security_id, symbol, 'listed' AS event_type, listing_date AS event_date,
                   NULL AS detail, NULL AS to_symbol, NULL AS to_name, 'krx' AS source, 'reported' AS confidence
            FROM kr_securities s JOIN kr_episodes e USING (security_id) WHERE listing_date IS NOT NULL
            UNION ALL
            SELECT security_id, symbol, 'first_observed', first_trade_date,
                   CASE WHEN first_trade_date = DATE '1995-05-02' THEN 'marcap 데이터 시작일 (실제 상장일 이전일 수 있음)' END,
                   NULL, NULL, 'marcap', 'observed'
            FROM kr_securities s JOIN kr_episodes e USING (security_id)
            UNION ALL
            SELECT security_id, symbol, 'delisted', delisting_date, delisting_reason, to_symbol, to_name,
                   'krx_delisting', 'reported'
            FROM kr_securities s JOIN kr_episodes e USING (security_id) WHERE delisting_date IS NOT NULL
            UNION ALL
            SELECT security_id, symbol, 'merged', last_trade_date, delisting_reason,
                   left(symbol, 5) || '0', NULL, 'marcap', 'inferred'
            FROM kr_securities s JOIN kr_episodes e USING (security_id) WHERE status = 'merged'
            UNION ALL
            SELECT security_id, symbol, 'disappeared_unexplained', last_trade_date,
                   '마지막 관측 이후 거래 기록 없음, 상장폐지 기록과 매칭되지 않음 (청산·보상 정보 미확인)',
                   NULL, NULL, 'marcap', 'inferred'
            FROM kr_securities s JOIN kr_episodes e USING (security_id) WHERE status = 'unknown'
            UNION ALL
            SELECT security_id, symbol, 'code_reused', first_trade_date,
                   '동일 코드의 이전 상장 구간과 분리 (공백/상장폐지 이력 기반 추정)', NULL, NULL, 'marcap', 'inferred'
            FROM kr_securities s JOIN kr_episodes e USING (security_id) WHERE s.id_confidence = 'inferred_reuse'
        """).fetch_arrow_table())
        cat.execute("DELETE FROM listing_events WHERE security_id LIKE 'KR-%'")
        cat.execute("INSERT INTO listing_events SELECT * FROM kr_ev_df")
        # 상장폐지 목록에는 있으나 marcap 에서 관측되지 않은 종목 (1995 이전 폐지 등)
        unmatched = con.execute(f"""
            SELECT count(*) FROM delist_full d
            WHERE NOT EXISTS (SELECT 1 FROM kr_episodes e WHERE e.symbol = d.symbol)
        """).fetchone()[0]
        stats = cat.execute("""SELECT count(*), sum(CASE WHEN status='delisted' THEN 1 ELSE 0 END),
                                      sum(CASE WHEN status='listed' THEN 1 ELSE 0 END),
                                      sum(CASE WHEN status='unknown' THEN 1 ELSE 0 END)
                               FROM securities WHERE country='KR'""").fetchone()
    return {"securities": stats[0], "delisted": stats[1], "listed": stats[2], "unknown": stats[3],
            "delisting_records_without_price_data": unmatched}


def update_calendar(con) -> int:
    """KR 거래일 = marcap 관측일 ∪ exchange_calendars(XKRX). 출처를 함께 기록."""
    import exchange_calendars as xcals

    obs = {r[0] for r in con.execute(
        f"SELECT DISTINCT CAST(Date AS DATE) FROM read_parquet('{raw_glob()}', union_by_name=true)").fetchall()}
    try:
        cal = xcals.get_calendar("XKRX", start="1995-01-01")
        xc = {d.date() for d in cal.sessions if d.date() <= date.today() + timedelta(days=370)}
    except Exception:
        xc = set()
    rows = []
    for d in sorted(obs | xc):
        src = "both" if d in obs and d in xc else ("observed" if d in obs else "exchange_calendars")
        rows.append(("KR", d, "Asia/Seoul", src))
    import pandas as pd

    with catalog() as cat:
        cat.register("cal_df", pd.DataFrame(rows, columns=["market", "trade_date", "tz", "source"]))
        cat.execute("DELETE FROM trading_calendars WHERE market='KR'")
        cat.execute("INSERT INTO trading_calendars SELECT * FROM cal_df")
    return len(rows)


def run(years: list[int] | None = None, run_id: str = "manual") -> dict:
    """연도 목록이 없으면 전체 재표준화. 증권 구간은 항상 전체 기준으로 재계산."""
    s = get_settings()
    all_years = available_years()
    if not all_years:
        return {"years": [], "note": "marcap 원본 없음"}
    years = sorted(set(all_years if years is None else years) & set(all_years))
    big_move = float(s.get("validation.big_move_threshold", 0.35))
    cutoff = kr_final_cutoff()
    con = memory_connection()
    try:
        build_episodes(con)
        # 구간 경계가 바뀌면 security_id 가 바뀔 수 있으므로, 이전 구간표와 비교해 영향을 받는 연도도 다시 만든다
        prev_path = s.paths.metadata / "kr_episodes.parquet"
        if prev_path.exists():
            # 구간 시작일(=security_id)이 생기거나 사라진 경우만 비교 (종료일 연장은 최신 연도만 영향)
            changed = con.execute(f"""
                WITH new AS (SELECT symbol, start_date, end_date FROM kr_episodes),
                     old AS (SELECT symbol, start_date, end_date FROM read_parquet('{prev_path.as_posix()}')),
                     diff AS (
                        SELECT n.* FROM new n ANTI JOIN old o USING (symbol, start_date)
                        UNION ALL
                        SELECT o.* FROM old o ANTI JOIN new n USING (symbol, start_date))
                SELECT DISTINCT y FROM (
                    SELECT unnest(range(year(start_date), year(end_date) + 1)) AS y FROM diff
                ) WHERE y IN ({",".join(map(str, all_years))})
            """).fetchall()
            extra = {r[0] for r in changed} - set(years)
            if extra:
                log_event(log, "구간 변경으로 추가 재표준화", years=sorted(extra))
            years = sorted(set(years) | extra)
        results = []
        for y in years:
            r = standardize_year(con, y, cutoff, big_move)
            results.append(r)
            log_event(log, "standardized", **{k: str(v) for k, v in r.items()})
        tmp = prev_path.with_name(".kr_episodes.tmp")
        con.execute(f"COPY kr_episodes TO '{tmp.as_posix()}' (FORMAT parquet)")
        atomic_replace(tmp, prev_path)
        _record_issues(con, run_id, years)
        n_ca = _record_corporate_actions(con)
        master = build_security_master(con)
        n_cal = update_calendar(con)
    finally:
        con.close()
    return {"years": years, "per_year": results, "corporate_actions_inferred": n_ca,
            "master": master, "calendar_days": n_cal}
