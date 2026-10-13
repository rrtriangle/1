"""특징·라벨 생성 엔진 (DuckDB SQL, 연도 단위 분할 처리).

[시점 규칙 — 미래정보 누출 방지]
- 특징(t): 거래일 t 장 마감까지 알 수 있는 값만 사용 (같은 종목의 t 이하 행, 같은 날 횡단면 순위,
  available_at ≤ 의사결정 시각인 시장지표).
- 의사결정 시각: 해당 시장 t일 마감 + 확정 지연 (KR 18:30 KST, US 20:00 ET).
  → KR 의 t일 특징에는 미국 t-1일 지표까지만, US 의 t일 특징에는 한국 t일 지표까지 들어간다.
- 라벨(t, k): t+1일 시가에 매수(체결 가능해야 함), t+k일 종가에 청산한 수익률.
      y_ret_k = I(t+k) / ( I(t) × open(t+1)/ref(t+1) ) − 1
  I 는 기업행위(분할·증자 기준가 조정)와 US 배당을 반영한 누적 수익률 지수로, t 이전 데이터만으로 앞에서부터
  누적하므로 미래 기업행위 정보가 과거 특징에 들어가지 않는다. 라벨은 학습에만 쓰이며 특징에 섞이지 않는다.
- t+1일 시가가 없으면(거래정지 등) 진입 불가 → 라벨 없음(entry_ok=false).
- t+k 전에 거래가 끝나면(상장폐지 등) 마지막 종가까지의 수익률을 쓰고 y_trunc_k=1 로 표시한다.
  (청산가치 불명 → 백테스트에서 보수적 시나리오 별도 제공)

[기술적 지표 대체 구현]
DuckDB 에 지수이동평균(EMA)이 없어 MACD 는 단순이동평균(12/26/9) 기반, ADX 는 추세효율비(Kaufman ER, 14일)로 대체했다.
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pandas as pd

from ..config import get_settings
from ..logging_setup import get_logger, log_event
from ..storage.catalog import catalog, memory_connection
from ..storage.parquet_io import atomic_replace
from .resources import rss_gb

log = get_logger("features")

FEATURES = [
    # 가격·추세
    "ret_1", "ret_5", "ret_10", "ret_20", "ret_60", "ret_120",
    "ma_gap_5", "ma_gap_20", "ma_gap_60", "ma_gap_120", "ma_5_20", "ma_20_60", "ma_60_120",
    "pos_high_252", "pos_low_252", "new_high_252", "new_low_252", "days_since_high_252", "up_ratio_20",
    # 거래량
    "log_amount_20", "vol_ratio_5_60", "amount_chg_5_60", "vol_cv_20", "pv_corr_20", "amihud_20", "turnover_20",
    # 변동성
    "rv_5", "rv_20", "rv_60", "hl_range_20", "atr_14", "dd_60", "jump_freq_60", "vol_trend_5_60",
    # 상대성과
    "xret_20", "xret_60", "beta_120", "sharpe_60", "rank_ret_20", "rank_ret_60", "rank_rv_20", "rank_amount_20",
    "log_mcap",
    # 기술적 지표
    "rsi_14", "macd", "macd_signal", "bb_pctb_20", "bb_width_20", "er_14",
    # 시장 환경
    "mkt_ret_20", "mkt_ret_60", "mkt_ma200_gap", "mkt_rv_20", "mkt_rv_chg", "mkt_adv_ratio_5", "mkt_amount_chg",
    "vix", "vix_chg_20", "fx_ret_20", "us10y", "us10y_chg_20", "t10y2y",
]
LABELS = []
for _k in (5, 20, 60):
    LABELS += [f"y_ret_{_k}", f"y_up_{_k}", f"y_xs_{_k}", f"y_rank_{_k}", f"y_vol_{_k}", f"y_mdd_{_k}", f"y_trunc_{_k}"]
META = ["security_id", "market", "trade_date", "close", "open_next", "amount_20", "market_cap", "security_type",
        "eligible", "entry_ok", "quality_grade"]

DECISION = {"KR": ("Asia/Seoul", "15:30", 180), "US": ("America/New_York", "16:00", 240)}


def features_dir(market: str) -> Path:
    s = get_settings()
    return s.paths.data / s.get("research.features_dir", "features") / f"market={market}"


def std_glob(market: str) -> str:
    return (get_settings().paths.standardized / "daily_prices" / f"country={market}" / "year=*" / "*.parquet").as_posix()


def available_years(market: str) -> list[int]:
    d = get_settings().paths.standardized / "daily_prices" / f"country={market}"
    return sorted(int(p.name.split("=")[1]) for p in d.glob("year=*") if any(p.glob("*.parquet")))


def _indicator(con, iid: str) -> str:
    base = get_settings().paths.standardized / "market_indicators"
    return f"(SELECT obs_date, value, available_at FROM read_parquet('{(base / '*.parquet').as_posix()}', union_by_name=true) WHERE indicator_id = '{iid}')"


def build_market_env(con, market: str) -> None:
    """시장 환경 특징 (날짜 단위). 지표는 available_at ≤ 의사결정 시각 인 최신 값으로 as-of 조인."""
    tz, close, delay = DECISION[market]
    if market == "KR":
        # marcap 전 종목 시총가중 수익률(KOSPI+KOSDAQ 합산 대용: KOSPI 사용), 상승비율, 거래대금
        mret = _indicator(con, "MARCAP:KOSPI_CW_RETURN")
        adv = _indicator(con, "MARCAP:KOSPI_ADV_RATIO")
        amt = _indicator(con, "MARCAP:KOSPI_TOTAL_AMOUNT")
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE mkt_base AS
            SELECT r.obs_date AS d, r.value AS mret, a.value AS adv, m.value AS amount
            FROM {mret} r LEFT JOIN {adv} a USING (obs_date) LEFT JOIN {amt} m USING (obs_date)
        """)
    else:
        spx = _indicator(con, "YAHOO:^GSPC")
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE mkt_base AS
            SELECT obs_date AS d, value / lag(value) OVER (ORDER BY obs_date) - 1 AS mret,
                   NULL::DOUBLE AS adv, NULL::DOUBLE AS amount FROM {spx}
        """)
        # 미국 상승비율·거래대금: 보유 데이터 전 종목(생존편향 있음)으로 계산
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE us_breadth AS
            SELECT trade_date AS d, avg(CASE WHEN split_adj_close > prev THEN 1.0 WHEN split_adj_close < prev THEN 0.0 END) adv,
                   sum(amount) amount
            FROM (SELECT trade_date, split_adj_close, amount,
                         lag(split_adj_close) OVER (PARTITION BY security_id ORDER BY trade_date) prev
                  FROM read_parquet('{std_glob("US")}', hive_partitioning=true) WHERE usable)
            GROUP BY 1
        """)
        con.execute("""UPDATE mkt_base SET adv = b.adv, amount = b.amount FROM us_breadth b WHERE mkt_base.d = b.d""")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE mkt_feat AS
        WITH b AS (
            SELECT d, mret, adv, amount, ln(1 + coalesce(mret, 0)) lr,
                   timezone('{tz}', d + TIME '{close}:00') + INTERVAL {delay} MINUTE AS decision_ts
            FROM mkt_base
        ), c AS (SELECT *, sum(lr) OVER (ORDER BY d) AS li FROM b)
        SELECT d, decision_ts,
            exp(sum(lr) OVER w20) - 1 AS mkt_ret_20,
            exp(sum(lr) OVER w60) - 1 AS mkt_ret_60,
            li - avg(li) OVER w200 AS mkt_ma200_gap,
            stddev_samp(lr) OVER w20 * sqrt(252) AS mkt_rv_20,
            (stddev_samp(lr) OVER w20) / nullif(stddev_samp(lr) OVER w120, 0) - 1 AS mkt_rv_chg,
            avg(adv) OVER w5 AS mkt_adv_ratio_5,
            avg(amount) OVER w5 / nullif(avg(amount) OVER w60, 0) - 1 AS mkt_amount_chg,
            mret AS mkt_ret_1
        FROM c
        WINDOW w5 AS (ORDER BY d ROWS 4 PRECEDING), w20 AS (ORDER BY d ROWS 19 PRECEDING),
               w60 AS (ORDER BY d ROWS 59 PRECEDING), w120 AS (ORDER BY d ROWS 119 PRECEDING),
               w200 AS (ORDER BY d ROWS 199 PRECEDING)
    """)
    # 외부 지표 (VIX, 환율, 금리) — 의사결정 시각 기준 as-of
    for name, iid, transform in [("vix", "YAHOO:^VIX", "level"), ("krw", "YAHOO:KRW=X", "level"),
                                 ("us10y", "FRED:DGS10", "level"), ("t10y2y", "FRED:T10Y2Y", "level")]:
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE ind_{name} AS
            SELECT obs_date, value, available_at FROM {_indicator(con, iid)} WHERE value IS NOT NULL
        """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE env AS
        SELECT m.*, v.value AS vix, v.obs_date AS vix_date, k.value AS krw, k.obs_date AS krw_date,
               u.value AS us10y, u.obs_date AS us10y_date, s.value AS t10y2y
        FROM mkt_feat m
        ASOF LEFT JOIN ind_vix v ON v.available_at <= m.decision_ts
        ASOF LEFT JOIN ind_krw k ON k.available_at <= m.decision_ts
        ASOF LEFT JOIN ind_us10y u ON u.available_at <= m.decision_ts
        ASOF LEFT JOIN ind_t10y2y s ON s.available_at <= m.decision_ts
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE env2 AS
        SELECT *, vix / nullif(lag(vix, 20) OVER (ORDER BY d), 0) - 1 AS vix_chg_20,
               krw / nullif(lag(krw, 20) OVER (ORDER BY d), 0) - 1 AS fx_ret_20,
               us10y - lag(us10y, 20) OVER (ORDER BY d) AS us10y_chg_20
        FROM env
    """)


def _daily_sql(market: str, start: date, end: date, extra_where: str = "TRUE") -> str:
    """종목 일별 수익률 기초 테이블. KR=기준가 대비, US=분할반영 종가(+배당) 대비."""
    if market == "KR":
        return f"""
            SELECT security_id, market, trade_date, open, high, low, close, volume, amount, market_cap,
                   quality_grade, usable, is_traded,
                   coalesce(base_price, prev_close) AS ref,
                   CASE WHEN close > 0 AND coalesce(base_price, prev_close) > 0
                        THEN close / coalesce(base_price, prev_close) - 1 END AS r,
                   CASE WHEN open > 0 AND coalesce(base_price, prev_close) > 0
                        THEN open / coalesce(base_price, prev_close) END AS open_gap,
                   high / nullif(close, 0) AS h_rel, low / nullif(close, 0) AS l_rel
            FROM read_parquet('{std_glob(market)}', hive_partitioning=true)
            WHERE trade_date BETWEEN DATE '{start}' AND DATE '{end}' AND {extra_where}
        """
    return f"""
        SELECT security_id, market, trade_date, open, high, low, close, volume, amount, NULL::DOUBLE AS market_cap,
               quality_grade, usable, is_traded,
               lag(split_adj_close) OVER (PARTITION BY security_id ORDER BY trade_date) AS ref,
               (split_adj_close + coalesce(dividends, 0)) / nullif(lag(split_adj_close) OVER (PARTITION BY security_id ORDER BY trade_date), 0) - 1 AS r,
               (open / nullif(split_factor, 0)) / nullif(lag(split_adj_close) OVER (PARTITION BY security_id ORDER BY trade_date), 0) AS open_gap,
               high / nullif(close, 0) AS h_rel, low / nullif(close, 0) AS l_rel
        FROM read_parquet('{std_glob(market)}', hive_partitioning=true)
        WHERE trade_date BETWEEN DATE '{start}' AND DATE '{end}' AND {extra_where}
    """


def build_year(con, market: str, year: int, cutoff: date | None = None, out_path: Path | None = None) -> dict:
    """cutoff: 이 날짜 이후 원천 데이터를 아예 보지 않고 계산 (누출 검사용)."""
    s = get_settings()
    u = s.get("research.universe", {}) or {}
    cur = "krw" if market == "KR" else "usd"
    min_price = float(u.get(f"min_price_{cur}", 0))
    min_amt = float(u.get(f"min_avg_amount_{cur}", 0))
    min_hist = int(u.get("min_history_days", 120))
    types = u.get("security_types", ["common"])
    start = date(year - 2, 1, 1)
    end = date(year + 1, 6, 30)  # 60거래일 라벨용 미래 버퍼
    if cutoff:
        end = min(end, cutoff)
    t0 = time.time()
    # 연구 대상 증권 종류만 읽고(종목별 특징은 자기 이력에만 의존), 행 수에 따라 종목 해시 버킷으로 나눠 계산 → 메모리 상한 유지
    with catalog(read_only=True) as cat:
        sec = cat.execute("SELECT security_id, security_type, status FROM securities WHERE country = ?", [market]).df()
    con.register("sec_df", sec)
    types_sql = ",".join(f"'{t}'" for t in types)
    con.execute(f"CREATE OR REPLACE TEMP TABLE sec_keep AS SELECT security_id FROM sec_df WHERE coalesce(security_type, 'common') IN ({types_sql})")
    keep = "security_id IN (SELECT security_id FROM sec_keep)"
    n_src, last_day = con.execute(f"""SELECT count(*), max(trade_date) FROM read_parquet('{std_glob(market)}', hive_partitioning=true)
                                      WHERE trade_date BETWEEN DATE '{start}' AND DATE '{end}' AND {keep}""").fetchone()
    nb = max(1, -(-int(n_src) // 2_500_000))
    con.execute("DROP TABLE IF EXISTS out_acc")
    for b in range(nb):
        bucket = f"{keep} AND hash(security_id) % {nb} = {b}"
        _chunk(con, market, year, start, end, bucket, last_day)
        con.execute("CREATE TABLE IF NOT EXISTS out_acc AS SELECT * FROM out0 LIMIT 0")
        con.execute("INSERT INTO out_acc SELECT * FROM out0")
    _finish_year(con, market, year, out_path, types_sql, min_price, min_amt, min_hist, t0, nb)
    n = con.execute(f"SELECT count(*) FROM read_parquet('{(out_path or (features_dir(market) / f'year={year}' / 'part-0.parquet')).as_posix()}')").fetchone()
    out = out_path or (features_dir(market) / f"year={year}" / "part-0.parquet")
    res = {"market": market, "year": year, "rows_all": int(con.execute("SELECT count(*) FROM out_acc").fetchone()[0]),
           "eligible": n[0], "buckets": nb, "sec": round(time.time() - t0, 1), "rss_gb": round(rss_gb(), 2), "bytes": out.stat().st_size}
    con.execute("DROP TABLE IF EXISTS out_acc")
    log_event(log, "features built", **res)
    return res


def _chunk(con, market: str, year: int, start: date, end: date, where: str, last_day) -> None:
    con.execute(f"CREATE OR REPLACE TEMP TABLE d0 AS {_daily_sql(market, start, end, where)}")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE d1 AS
        SELECT d0.*, ln(1 + greatest(coalesce(r, 0), -0.99)) AS lr,
               row_number() OVER (PARTITION BY security_id ORDER BY trade_date) AS ix
        FROM d0
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE d2 AS
        SELECT d1.*, sum(lr) OVER (PARTITION BY security_id ORDER BY trade_date) AS li,
               e.mkt_ret_1, ln(1 + coalesce(e.mkt_ret_1, 0)) AS mlr
        FROM d1 LEFT JOIN env2 e ON e.d = d1.trade_date
    """)
    W = "PARTITION BY security_id ORDER BY trade_date"
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE f1 AS
        SELECT security_id, market, trade_date, close, amount, market_cap, quality_grade, usable, ix, li, lr, r,
            exp(lr) - 1 AS ret_1,
            exp(li - lag(li, 5) OVER ({W})) - 1 AS ret_5,
            exp(li - lag(li, 10) OVER ({W})) - 1 AS ret_10,
            exp(li - lag(li, 20) OVER ({W})) - 1 AS ret_20,
            exp(li - lag(li, 60) OVER ({W})) - 1 AS ret_60,
            exp(li - lag(li, 120) OVER ({W})) - 1 AS ret_120,
            exp(li - ln(avg(exp(li - li_anchor)) OVER w5) - li_anchor) - 1 AS ma_gap_5,
            exp(li - ln(avg(exp(li - li_anchor)) OVER w20) - li_anchor) - 1 AS ma_gap_20,
            exp(li - ln(avg(exp(li - li_anchor)) OVER w60) - li_anchor) - 1 AS ma_gap_60,
            exp(li - ln(avg(exp(li - li_anchor)) OVER w120) - li_anchor) - 1 AS ma_gap_120,
            avg(exp(li - li_anchor)) OVER w5 / nullif(avg(exp(li - li_anchor)) OVER w20, 0) - 1 AS ma_5_20,
            avg(exp(li - li_anchor)) OVER w20 / nullif(avg(exp(li - li_anchor)) OVER w60, 0) - 1 AS ma_20_60,
            avg(exp(li - li_anchor)) OVER w60 / nullif(avg(exp(li - li_anchor)) OVER w120, 0) - 1 AS ma_60_120,
            (avg(exp(li - li_anchor)) OVER w12 - avg(exp(li - li_anchor)) OVER w26) / nullif(exp(li - li_anchor), 0) AS macd,
            exp(li - max(li) OVER w252) - 1 AS pos_high_252,
            exp(li - min(li) OVER w252) - 1 AS pos_low_252,
            CASE WHEN li >= max(li) OVER w252 - 1e-9 AND count(*) OVER w252 >= 120 THEN 1 ELSE 0 END AS new_high_252,
            CASE WHEN li <= min(li) OVER w252 + 1e-9 AND count(*) OVER w252 >= 120 THEN 1 ELSE 0 END AS new_low_252,
            ix - arg_max(ix, li + ix * 1e-12) OVER w252 AS days_since_high_252,
            avg(CASE WHEN r > 0 THEN 1.0 WHEN r < 0 THEN 0.0 ELSE 0.5 END) OVER w20 AS up_ratio_20,
            ln(1 + avg(amount) OVER w20) AS log_amount_20,
            avg(amount) OVER w20 AS amount_20,
            avg(volume) OVER w5 / nullif(avg(volume) OVER w60, 0) AS vol_ratio_5_60,
            avg(amount) OVER w5 / nullif(avg(amount) OVER w60, 0) AS amount_chg_5_60,
            stddev_samp(volume) OVER w20 / nullif(avg(volume) OVER w20, 0) AS vol_cv_20,
            corr(lr, ln(1 + volume)) OVER w20 AS pv_corr_20,
            avg(CASE WHEN amount > 0 THEN abs(r) / amount * 1e9 END) OVER w20 AS amihud_20,
            avg(amount / nullif(market_cap, 0)) OVER w20 AS turnover_20,
            stddev_samp(lr) OVER w5 * sqrt(252) AS rv_5,
            stddev_samp(lr) OVER w20 * sqrt(252) AS rv_20,
            stddev_samp(lr) OVER w60 * sqrt(252) AS rv_60,
            avg(CASE WHEN is_traded AND close > 0 THEN h_rel - l_rel END) OVER w20 AS hl_range_20,
            avg(CASE WHEN is_traded AND close > 0 THEN greatest(h_rel - l_rel, abs(h_rel - 1 / (1 + coalesce(r, 0))),
                     abs(l_rel - 1 / (1 + coalesce(r, 0)))) END) OVER w14 AS atr_14,
            exp(li - max(li) OVER w60) - 1 AS dd_60,
            avg(CASE WHEN abs(r) > 0.1 THEN 1.0 ELSE 0.0 END) OVER w60 AS jump_freq_60,
            (stddev_samp(lr) OVER w5) / nullif(stddev_samp(lr) OVER w60, 0) AS vol_trend_5_60,
            exp(li - lag(li, 20) OVER ({W}) - (sum(mlr) OVER w20)) - 1 AS xret_20,
            exp(li - lag(li, 60) OVER ({W}) - (sum(mlr) OVER w60)) - 1 AS xret_60,
            covar_samp(lr, mlr) OVER w120 / nullif(var_samp(mlr) OVER w120, 0) AS beta_120,
            avg(lr) OVER w60 / nullif(stddev_samp(lr) OVER w60, 0) * sqrt(252) AS sharpe_60,
            ln(nullif(market_cap, 0)) AS log_mcap,
            100 - 100 / (1 + sum(greatest(lr, 0)) OVER w14 / nullif(sum(greatest(-lr, 0)) OVER w14, 0)) AS rsi_14,
            (exp(li - li_anchor) - avg(exp(li - li_anchor)) OVER w20) / nullif(2 * stddev_samp(exp(li - li_anchor)) OVER w20, 0) AS bb_pctb_20,
            4 * stddev_samp(exp(li - li_anchor)) OVER w20 / nullif(avg(exp(li - li_anchor)) OVER w20, 0) AS bb_width_20,
            abs(li - lag(li, 14) OVER ({W})) / nullif(sum(abs(lr)) OVER w14, 0) AS er_14,
            count(*) OVER w252 AS hist_n
        FROM (SELECT *, first_value(li) OVER ({W} ROWS BETWEEN 260 PRECEDING AND CURRENT ROW) AS li_anchor FROM d2)
        WINDOW w5 AS ({W} ROWS 4 PRECEDING), w12 AS ({W} ROWS 11 PRECEDING), w14 AS ({W} ROWS 13 PRECEDING),
               w20 AS ({W} ROWS 19 PRECEDING), w26 AS ({W} ROWS 25 PRECEDING), w60 AS ({W} ROWS 59 PRECEDING),
               w120 AS ({W} ROWS 119 PRECEDING), w252 AS ({W} ROWS 251 PRECEDING)
    """)
    # 라벨: t+1 시가 진입 → t+k 종가 청산 (미래 창은 라벨 전용)
    lab = []
    for k in (5, 20, 60):
        lab.append(f"""
            CASE WHEN lead(open_gap, 1) OVER ({W}) > 0 THEN
              exp(coalesce(lead(li, {k}) OVER ({W}), last_value(li) OVER ({W} ROWS BETWEEN CURRENT ROW AND {k} FOLLOWING))
                  - li - ln(lead(open_gap, 1) OVER ({W}))) - 1 END AS y_ret_{k},
            CASE WHEN lead(li, {k}) OVER ({W}) IS NULL THEN 1 ELSE 0 END AS y_trunc_{k},
            stddev_samp(lr) OVER ({W} ROWS BETWEEN 1 FOLLOWING AND {k} FOLLOWING) * sqrt(252) AS y_vol_{k},
            exp(min(li) OVER ({W} ROWS BETWEEN 1 FOLLOWING AND {k} FOLLOWING) - li
                - ln(nullif(lead(open_gap, 1) OVER ({W}), 0))) - 1 AS y_mdd_{k},
            count(*) OVER ({W} ROWS BETWEEN 1 FOLLOWING AND {k} FOLLOWING) AS y_n_{k}""")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE lab AS
        SELECT security_id, trade_date, lead(open, 1) OVER ({W}) AS open_next,
               lead(open_gap, 1) OVER ({W}) > 0 AS entry_ok, {",".join(lab)}
        FROM d2
    """)
    # 데이터 끝(최근)에서 미래가 없는 경우는 상장폐지와 구분: 해당 종목이 데이터 끝까지 거래 중이면 라벨 없음
    con.execute("""
        CREATE OR REPLACE TEMP TABLE sec_last AS
        SELECT security_id, max(trade_date) AS last_d FROM d2 GROUP BY 1
    """)
    sel_f = ", ".join(f"CAST(f.{c} AS FLOAT) AS {c}" for c in FEATURES if not c.startswith(("rank_", "mkt_", "vix", "fx_", "us10y", "t10y2y", "macd_signal")))
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE out0 AS
        SELECT f.security_id, f.market, f.trade_date, f.close, l.open_next, f.amount_20, f.market_cap,
               s.security_type, f.quality_grade, coalesce(l.entry_ok, false) AS entry_ok, f.hist_n,
               {sel_f},
               CAST(avg(f.macd) OVER (PARTITION BY f.security_id ORDER BY f.trade_date ROWS 8 PRECEDING) AS FLOAT) AS macd_signal,
               CAST(e.mkt_ret_20 AS FLOAT) mkt_ret_20, CAST(e.mkt_ret_60 AS FLOAT) mkt_ret_60,
               CAST(e.mkt_ma200_gap AS FLOAT) mkt_ma200_gap, CAST(e.mkt_rv_20 AS FLOAT) mkt_rv_20,
               CAST(e.mkt_rv_chg AS FLOAT) mkt_rv_chg, CAST(e.mkt_adv_ratio_5 AS FLOAT) mkt_adv_ratio_5,
               CAST(e.mkt_amount_chg AS FLOAT) mkt_amount_chg, CAST(e.vix AS FLOAT) vix,
               CAST(e.vix_chg_20 AS FLOAT) vix_chg_20, CAST(e.fx_ret_20 AS FLOAT) fx_ret_20,
               CAST(e.us10y AS FLOAT) us10y, CAST(e.us10y_chg_20 AS FLOAT) us10y_chg_20, CAST(e.t10y2y AS FLOAT) t10y2y,
               {",".join(f"CASE WHEN l.y_n_{k} = 0 AND sl.last_d >= DATE '{last_day}' - INTERVAL 7 DAY THEN NULL ELSE CAST(l.y_ret_{k} AS FLOAT) END AS y_ret_{k}, CAST(l.y_vol_{k} AS FLOAT) AS y_vol_{k}, CAST(l.y_mdd_{k} AS FLOAT) AS y_mdd_{k}, CASE WHEN l.y_n_{k} < {k} AND sl.last_d >= DATE '{last_day}' - INTERVAL 7 DAY THEN NULL ELSE l.y_trunc_{k} END AS y_trunc_{k}" for k in (5, 20, 60))}
        FROM f1 f
        JOIN lab l USING (security_id, trade_date)
        JOIN sec_last sl USING (security_id)
        LEFT JOIN sec_df s USING (security_id)
        LEFT JOIN env2 e ON e.d = f.trade_date
        WHERE year(f.trade_date) = {year}
    """)
    # 미래가 아직 오지 않은(데이터 끝) 라벨 제거
    for k in (5, 20, 60):
        con.execute(f"UPDATE out0 SET y_ret_{k} = NULL, y_vol_{k} = NULL, y_mdd_{k} = NULL WHERE y_trunc_{k} IS NULL")


def _finish_year(con, market, year, out_path, types_sql, min_price, min_amt, min_hist, t0, nb) -> None:
    out = out_path or (features_dir(market) / f"year={year}" / "part-0.parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(".part-0.tmp")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE out1 AS
        SELECT *, (coalesce(security_type, 'common') IN ({types_sql}) AND quality_grade IN ('A', 'B', 'C')
                   AND close >= {min_price} AND amount_20 >= {min_amt} AND hist_n >= {min_hist}) AS eligible
        FROM out_acc
    """)
    # 저장은 학습·분석 대상(eligible) 행만. 횡단면 순위도 같은 날 투자 가능 종목 안에서 계산한다.
    con.execute(f"""
        COPY (
          SELECT *,
            CAST(percent_rank() OVER (PARTITION BY trade_date ORDER BY round(ret_20, 6)) AS FLOAT) AS rank_ret_20,
            CAST(percent_rank() OVER (PARTITION BY trade_date ORDER BY round(ret_60, 6)) AS FLOAT) AS rank_ret_60,
            CAST(percent_rank() OVER (PARTITION BY trade_date ORDER BY round(rv_20, 6)) AS FLOAT) AS rank_rv_20,
            CAST(percent_rank() OVER (PARTITION BY trade_date ORDER BY round(log_amount_20, 6)) AS FLOAT) AS rank_amount_20,
            {",".join(f"CAST(y_ret_{k} - avg(y_ret_{k}) OVER (PARTITION BY trade_date) AS FLOAT) AS y_xs_{k}, CAST(percent_rank() OVER (PARTITION BY trade_date ORDER BY round(y_ret_{k}, 6)) AS FLOAT) AS y_rank_{k}, CASE WHEN y_ret_{k} IS NULL THEN NULL WHEN y_ret_{k} > 0 THEN 1 ELSE 0 END::TINYINT AS y_up_{k}" for k in (5, 20, 60))}
          FROM out1 WHERE eligible
          ORDER BY trade_date, security_id
        ) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 100000)
    """)
    atomic_replace(tmp, out)


def build(market: str, years: list[int] | None = None) -> list[dict]:
    """years=None: 표준화 데이터가 있는 모든 연도. 영향 받은 연도만 지정해 재생성 가능."""
    ys = available_years(market)
    if years:
        ys = [y for y in ys if y in years]
    con = memory_connection()
    try:
        build_market_env(con, market)
        res = [build_year(con, market, y) for y in ys]
    finally:
        con.close()
    _write_manifest(market, res)
    return res


def _write_manifest(market: str, res: list[dict]) -> None:
    import json
    p = features_dir(market) / "_manifest.json"
    old = json.loads(p.read_text()) if p.exists() else {}
    for r in res:
        old[str(r["year"])] = r
    old["_features"] = FEATURES
    old["_labels"] = LABELS
    p.write_text(json.dumps(old, ensure_ascii=False, indent=1, default=str))


def feature_glob(market: str) -> str:
    return (features_dir(market) / "year=*" / "*.parquet").as_posix()


def stale_years(market: str) -> list[int]:
    """표준화 파일이 특징 파일보다 새로우면 재생성 대상 (과거 데이터 변경 시 영향 연도만)."""
    s = get_settings()
    out = []
    for y in available_years(market):
        src = s.paths.standardized / "daily_prices" / f"country={market}" / f"year={y}" / "part-0.parquet"
        dst = features_dir(market) / f"year={y}" / "part-0.parquet"
        # 다음 해 데이터가 라벨에 쓰이므로 y+1 변경도 영향
        nxt = s.paths.standardized / "daily_prices" / f"country={market}" / f"year={y + 1}" / "part-0.parquet"
        m = max(src.stat().st_mtime, nxt.stat().st_mtime if nxt.exists() else 0)
        if not dst.exists() or dst.stat().st_mtime < m:
            out.append(y)
    return out
