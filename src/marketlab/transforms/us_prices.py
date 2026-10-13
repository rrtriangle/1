"""미국 일봉 표준화: yfinance 원본 → daily_prices_standardized (country=US, 연도 파티션).

- 같은 (security_id, trade_date)가 여러 번 수집되면 가장 최근 수집본을 쓴다 (원본은 모두 보존).
- Yahoo Close 는 분할 반영값이므로, 수집 시점까지 알려진 분할 이력으로 당시 거래가격을 역산한다.
    raw_close(d)  = split_adj_close(d) × Π(분할비율, ex_date > d)
    raw_volume(d) = volume(d) ÷ Π(...)
  → price_basis = 'raw_reconstructed'. Yahoo 원값은 split_adj_close, 배당까지 반영값은 adj_close_tr 로 함께 둔다.
- 연도 단위로 처리하여 메모리 사용을 제한한다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..collectors import yfinance_us
from ..config import get_settings
from ..logging_setup import get_logger, log_event
from ..storage.catalog import catalog, memory_connection
from ..storage.parquet_io import atomic_replace

log = get_logger("us_transform")


def std_dir() -> Path:
    return get_settings().paths.standardized / "daily_prices" / "country=US"


def raw_glob() -> str:
    return (yfinance_us.raw_dir() / "fetch_date=*" / "*.parquet").as_posix()


def us_final_cutoff(now: datetime | None = None) -> date:
    cfg = get_settings().get("update.finalize_after.US", {}) or {}
    tz = ZoneInfo(cfg.get("tz", "America/New_York"))
    now = (now or datetime.now(tz)).astimezone(tz)
    hh, mm = map(int, str(cfg.get("close", "16:00")).split(":"))
    final_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(minutes=int(cfg.get("delay_min", 240)))
    return now.date() if now >= final_at else now.date() - timedelta(days=1)


def _state_path() -> Path:
    return get_settings().paths.state_dir / "us_standardized_at.txt"


def affected_years(con, since: str | None) -> list[int]:
    where = f"WHERE fetched_at > TIMESTAMPTZ '{since}'" if since else ""
    rows = con.execute(f"SELECT DISTINCT year(trade_date) FROM read_parquet('{raw_glob()}', hive_partitioning=false) {where}").fetchall()
    return sorted(r[0] for r in rows if r[0] is not None)


def run(years: list[int] | None = None, run_id: str = "manual") -> dict:
    s = get_settings()
    if not list(yfinance_us.raw_dir().glob("fetch_date=*/*.parquet")):
        return {"years": [], "note": "yfinance 원본 없음"}
    big_move = float(s.get("validation.big_move_threshold", 0.35))
    cutoff = us_final_cutoff()
    con = memory_connection()
    started = datetime.now().astimezone().isoformat()
    try:
        since = _state_path().read_text().strip() if _state_path().exists() else None
        if years is None:
            years = affected_years(con, since)
        with catalog(read_only=True) as cat:
            splits = cat.execute("""
                SELECT security_id, ex_date, ratio FROM corporate_actions
                WHERE source = 'yahoo' AND action_type IN ('split', 'reverse_split') AND ratio > 0
            """).df()
        con.register("splits_df", splits)
        con.execute("""
            CREATE OR REPLACE TEMP TABLE split_cum AS
            SELECT security_id, ex_date,
                   exp(sum(ln(ratio)) OVER (PARTITION BY security_id ORDER BY ex_date DESC
                                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)) AS cum_from
            FROM splits_df
        """)
        with catalog(read_only=True) as cat:
            sec = cat.execute("SELECT security_id, market, name FROM securities WHERE country='US'").df()
        con.register("securities_df", sec)
        results = []
        for y in years:
            out = std_dir() / f"year={y}" / "part-0.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(".part-0.parquet.tmp")
            con.execute(f"""
                COPY (
                WITH r AS (
                    SELECT * FROM read_parquet('{raw_glob()}', hive_partitioning=false)
                    WHERE trade_date BETWEEN DATE '{y - 1}-12-01' AND DATE '{y}-12-31'
                    QUALIFY row_number() OVER (PARTITION BY security_id, trade_date ORDER BY fetched_at DESC) = 1
                ), f AS (
                    SELECT r.*, coalesce(sc.cum_from, 1.0) AS split_factor
                    FROM r ASOF LEFT JOIN split_cum sc
                      ON r.security_id = sc.security_id AND sc.ex_date > r.trade_date
                ), w AS (
                    SELECT f.*, lag(close) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev_split_adj_close
                    FROM f
                ), q AS (
                    SELECT
                        w.security_id, 'US' AS country, s.market, NULL::VARCHAR AS market_id, w.yahoo_symbol AS symbol,
                        s.name, w.trade_date, 'USD' AS currency, 'yfinance' AS source, 'raw_reconstructed' AS price_basis,
                        open * split_factor AS open, high * split_factor AS high, low * split_factor AS low,
                        close * split_factor AS close, CAST(round(volume / split_factor) AS BIGINT) AS volume,
                        close * volume AS amount, NULL::DOUBLE AS market_cap, NULL::BIGINT AS shares_outstanding,
                        close AS split_adj_close, adj_close AS adj_close_tr, split_factor,
                        dividends, stock_splits,
                        volume > 0 AS is_traded,
                        trade_date <= DATE '{cutoff.isoformat()}' AS is_final,
                        list_filter([
                            CASE WHEN close IS NULL OR close <= 0 THEN 'nonpositive_close' END,
                            CASE WHEN volume < 0 THEN 'negative_volume_or_amount' END,
                            CASE WHEN volume > 0 AND open > 0 AND (high < greatest(open, close, low) * 0.9999
                                  OR low > least(open, close, high) * 1.0001) THEN 'ohlc_inconsistent' END,
                            CASE WHEN volume = 0 THEN 'no_trade' END,
                            CASE WHEN coalesce(stock_splits, 0) NOT IN (0, 1) THEN 'split_day' END,
                            CASE WHEN prev_split_adj_close > 0 AND abs(close / prev_split_adj_close - 1) > {big_move}
                                  AND coalesce(stock_splits, 0) IN (0, 1) THEN 'big_move_unexplained' END,
                            CASE WHEN split_factor <> 1 THEN 'raw_reconstructed_from_splits' END
                        ], x -> x IS NOT NULL) AS quality_flags,
                        fetched_at AS ingested_at
                    FROM w LEFT JOIN securities_df s USING (security_id)
                    WHERE year(trade_date) = {y}
                ), g AS (
                    SELECT *,
                        CASE WHEN list_has_any(quality_flags, ['nonpositive_close', 'negative_volume_or_amount', 'ohlc_inconsistent']) THEN 'X'
                             WHEN list_has_any(quality_flags, ['big_move_unexplained']) THEN 'C'
                             WHEN list_has_any(quality_flags, ['no_trade', 'split_day', 'raw_reconstructed_from_splits']) THEN 'B'
                             ELSE 'A' END AS quality_grade
                    FROM q
                )
                SELECT *, (quality_grade <> 'X' AND is_final) AS usable FROM g
                ORDER BY security_id, trade_date
                ) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 200000)
            """)
            atomic_replace(tmp, out)
            n = con.execute(f"SELECT count(*), count(DISTINCT security_id) FROM read_parquet('{out.as_posix()}')").fetchone()
            results.append({"year": y, "rows": n[0], "securities": n[1]})
            log_event(log, "standardized", year=y, rows=n[0])
        _update_us_master(con)
        update_calendar(con)
        _state_path().write_text(started)
    finally:
        con.close()
    return {"years": years, "per_year": results}


def _update_us_master(con) -> None:
    """수집된 가격 범위로 미국 증권의 first/last_trade_date 갱신."""
    glob = (std_dir() / "year=*" / "*.parquet").as_posix()
    if not list(std_dir().glob("year=*/*.parquet")):
        return
    rng = con.execute(f"""
        SELECT security_id, min(trade_date) AS f, max(trade_date) AS l
        FROM read_parquet('{glob}', hive_partitioning=true) GROUP BY 1
    """).df()
    with catalog() as cat:
        cat.register("rng", rng)
        cat.execute("""
            UPDATE securities SET first_trade_date = rng.f, last_trade_date = rng.l, updated_at = now()
            FROM rng WHERE securities.security_id = rng.security_id
        """)


def update_calendar(con) -> int:
    """US 거래일 = 데이터 관측일(종목 50개 이상 거래된 날) ∪ exchange_calendars(XNYS)."""
    import exchange_calendars as xcals
    import pandas as pd

    glob = (std_dir() / "year=*" / "*.parquet").as_posix()
    if not list(std_dir().glob("year=*/*.parquet")):
        return 0
    obs = {r[0] for r in con.execute(f"""SELECT trade_date FROM read_parquet('{glob}', hive_partitioning=true)
                                         GROUP BY 1 HAVING count(*) >= 50""").fetchall()}
    try:
        cal = xcals.get_calendar("XNYS", start="1962-01-01")
        xc = {d.date() for d in cal.sessions if d.date() <= date.today() + timedelta(days=370)}
    except Exception:
        xc = set()
    rows = [("US", d, "America/New_York", "both" if d in obs and d in xc else ("observed" if d in obs else "exchange_calendars"))
            for d in sorted(obs | xc)]
    with catalog() as cat:
        cat.register("cal_df", pd.DataFrame(rows, columns=["market", "trade_date", "tz", "source"]))
        cat.execute("DELETE FROM trading_calendars WHERE market='US'")
        cat.execute("INSERT INTO trading_calendars SELECT * FROM cal_df")
    return len(rows)
