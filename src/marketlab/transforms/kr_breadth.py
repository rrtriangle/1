"""한국 시장 전체 지표: marcap 표준화 데이터에서 시장별 상승·하락 비율, 거래대금, 시가총액, 수익률을 계산.

KRX 지수 캐시는 갱신이 늦을 수 있어, 전 종목 일봉에서 직접 계산한 시장 지표를 함께 제공한다.
- cap_weighted_return: 전일 시가총액 가중 수익률 (기준가 조정일은 조정 기준가 대비)
- 신주(new_shares)는 이중 집계를 피하려 제외
"""
from __future__ import annotations

from ..config import get_settings
from ..storage.catalog import memory_connection
from ..storage.parquet_io import atomic_replace
from .kr_prices import std_dir


def run() -> dict:
    s = get_settings()
    out = s.paths.standardized / "market_indicators" / "kr_breadth.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    glob = (std_dir() / "year=*" / "*.parquet").as_posix()
    cfg = s.get("update.finalize_after.KR", {}) or {}
    delay = int(cfg.get("delay_min", 180))
    con = memory_connection()
    try:
        con.execute(f"""
            CREATE TEMP TABLE d AS
            SELECT CASE WHEN market LIKE 'KOSDAQ%' THEN 'KOSDAQ' ELSE market END AS mkt, trade_date,
                   close, coalesce(base_price, prev_close) AS ref, prev_close, volume, amount, market_cap,
                   is_traded
            FROM read_parquet('{glob}', hive_partitioning=true)
            WHERE usable AND NOT regexp_matches(name, '\\d신\\)?$|신\\)$')
        """)
        con.execute(f"""
            CREATE TEMP TABLE agg AS
            SELECT mkt, trade_date,
                   count(*) AS n_listed,
                   sum(CASE WHEN is_traded THEN 1 ELSE 0 END) AS n_traded,
                   sum(CASE WHEN close > ref THEN 1 ELSE 0 END) AS n_up,
                   sum(CASE WHEN close < ref THEN 1 ELSE 0 END) AS n_down,
                   sum(amount) AS total_amount, sum(volume) AS total_volume, sum(market_cap) AS total_mcap,
                   sum(CASE WHEN ref > 0 THEN (close / ref - 1) * (market_cap / nullif(close, 0) * ref) END)
                     / nullif(sum(CASE WHEN ref > 0 THEN market_cap / nullif(close, 0) * ref END), 0) AS cw_ret,
                   avg(CASE WHEN ref > 0 AND is_traded THEN close / ref - 1 END) AS ew_ret
            FROM d GROUP BY ALL
        """)
        tmp = out.with_name(".kr_breadth.tmp")
        con.execute(f"""
            COPY (
              WITH long AS (
                SELECT mkt, trade_date, 'ADV_RATIO' AS k, n_up / nullif(n_up + n_down, 0) AS v, 'ratio' AS unit FROM agg
                UNION ALL SELECT mkt, trade_date, 'N_LISTED', n_listed, 'count' FROM agg
                UNION ALL SELECT mkt, trade_date, 'N_UP', n_up, 'count' FROM agg
                UNION ALL SELECT mkt, trade_date, 'N_DOWN', n_down, 'count' FROM agg
                UNION ALL SELECT mkt, trade_date, 'TOTAL_AMOUNT', total_amount, 'KRW' FROM agg
                UNION ALL SELECT mkt, trade_date, 'TOTAL_MCAP', total_mcap, 'KRW' FROM agg
                UNION ALL SELECT mkt, trade_date, 'CW_RETURN', cw_ret, 'return' FROM agg
                UNION ALL SELECT mkt, trade_date, 'EW_RETURN', ew_ret, 'return' FROM agg
              )
              SELECT 'MARCAP:' || mkt || '_' || k AS indicator_id, mkt || ' ' || k AS name, 'marcap_derived' AS source,
                     'KR' AS market, 'KRW' AS currency, unit, trade_date AS obs_date, CAST(v AS DOUBLE) AS value,
                     NULL::DOUBLE AS open, NULL::DOUBLE AS high, NULL::DOUBLE AS low, NULL::DOUBLE AS volume,
                     NULL::DOUBLE AS amount, NULL::DOUBLE AS market_cap,
                     timezone('Asia/Seoul', trade_date + TIME '15:30:00') + INTERVAL {delay} MINUTE AS available_at,
                     now() AS ingested_at
              FROM long ORDER BY indicator_id, obs_date
            ) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd)
        """)
        atomic_replace(tmp, out)
        n = con.execute(f"SELECT count(*), min(obs_date), max(obs_date) FROM read_parquet('{out.as_posix()}')").fetchone()
    finally:
        con.close()
    return {"rows": n[0], "min": str(n[1]), "max": str(n[2])}
