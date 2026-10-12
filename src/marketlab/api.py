"""머신러닝·백테스트용 조회 API (데이터 복제 없이 Parquet 를 DuckDB 로 직접 조회).

핵심 원칙: 미래정보 누출 방지
- 모든 함수는 as_of(그 시점 투자자가 알 수 있었던 정보 기준일)를 받는다.
- 수정주가는 as_of 이전에 알려진 기업행위만 반영해 그때그때 계산한다 (미래 분할·배당 미반영).
- 지표는 available_at ≤ as_of 장 마감 시각인 값만, 재무정보는 known_from ≤ as_of 인 값만 돌려준다.
- 유니버스는 해당 일자에 실제 거래 기록이 있는 증권(상장폐지 종목 포함)으로 만든다 → 생존편향 방지.

사용 예
    from marketlab.api import MarketData
    md = MarketData()
    md.universe("2008-10-24", country="KR")
    md.prices(["KR-005930-19950502"], "2020-01-01", "2020-12-31", adjust="split")
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timezone
from typing import Iterable, Literal, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from .config import Settings, get_settings
from .storage.catalog import catalog, memory_connection

Adjust = Literal["raw", "split", "all_corporate", "total_return"]

PRICE_COLUMNS = ["security_id", "country", "market", "symbol", "name", "trade_date", "currency", "source",
                 "price_basis", "open", "high", "low", "close", "volume", "amount", "market_cap",
                 "shares_outstanding", "is_traded", "is_final", "quality_grade", "quality_flags", "usable"]


def _d(x) -> date:
    if x is None:
        return date.today()
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


class MarketData:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.con = memory_connection(self.s)
        self._views()

    def close(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ------------------------------------------------------------------ setup
    def _views(self) -> None:
        std = self.s.paths.standardized
        parts = []
        for c in ("KR", "US"):
            if any((std / "daily_prices" / f"country={c}").glob("year=*/*.parquet")):
                parts.append(f"SELECT {', '.join(PRICE_COLUMNS)} FROM read_parquet("
                             f"'{(std / 'daily_prices' / f'country={c}' / 'year=*' / '*.parquet').as_posix()}', "
                             f"hive_partitioning=true, union_by_name=true)")
        if parts:
            self.con.execute("CREATE OR REPLACE VIEW prices AS " + " UNION ALL ".join(parts))
        else:
            cols = ", ".join(f"NULL AS {c}" for c in PRICE_COLUMNS)
            self.con.execute(f"CREATE OR REPLACE VIEW prices AS SELECT {cols} WHERE false")
        mi = list((std / "market_indicators").glob("*.parquet"))
        if mi:
            self.con.execute(f"CREATE OR REPLACE VIEW indicators AS SELECT * FROM read_parquet("
                             f"'{(std / 'market_indicators' / '*.parquet').as_posix()}', union_by_name=true)")
        ff = list((std / "financial_facts").glob("source=*/*.parquet"))
        if ff:
            self.con.execute(f"CREATE OR REPLACE VIEW financial_facts AS SELECT * FROM read_parquet("
                             f"'{(std / 'financial_facts' / 'source=*' / '*.parquet').as_posix()}', hive_partitioning=true)")
        # 카탈로그 메타데이터는 작으므로 스냅샷으로 복사 (파일 잠금 회피)
        with catalog(read_only=True, settings=self.s) as cat:
            for t in ("securities", "symbol_history", "listing_events", "corporate_actions", "trading_calendars"):
                self.con.register(f"_{t}_df", cat.execute(f"SELECT * FROM {t}").df())
                self.con.execute(f"CREATE OR REPLACE TABLE {t} AS SELECT * FROM _{t}_df")
                self.con.unregister(f"_{t}_df")

    def sql(self, query: str, params: Sequence | None = None) -> pd.DataFrame:
        """자유 SQL (views: prices, indicators, financial_facts / tables: securities 등)."""
        return self.con.execute(query, params or []).df()

    # ------------------------------------------------------------------ securities
    def search(self, text: str, limit: int = 50) -> pd.DataFrame:
        q = f"%{text}%"
        return self.con.execute("""
            SELECT DISTINCT s.security_id, s.country, s.market, s.security_type, s.name, s.status,
                   s.first_trade_date, s.last_trade_date, s.delisting_date
            FROM securities s LEFT JOIN symbol_history h USING (security_id)
            WHERE s.name ILIKE ? OR h.name ILIKE ? OR h.symbol ILIKE ? OR s.security_id ILIKE ?
            ORDER BY s.status = 'listed' DESC, s.last_trade_date DESC NULLS LAST LIMIT ?
        """, [q, q, q, q, limit]).df()

    def resolve(self, symbol: str, on: str | date | None = None, country: str | None = None) -> str | None:
        """특정 일자 기준 종목코드/티커 → security_id (코드 재사용을 구분)."""
        d = _d(on)
        row = self.con.execute("""
            SELECT h.security_id FROM symbol_history h JOIN securities s USING (security_id)
            WHERE upper(h.symbol) = upper(?) AND h.symbol_type IN ('krx_code', 'ticker', 'yahoo')
              AND (? IS NULL OR s.country = ?)
              AND coalesce(h.valid_from, DATE '1900-01-01') <= ? AND coalesce(h.valid_to, DATE '2999-12-31') >= ?
            ORDER BY h.valid_from DESC NULLS LAST LIMIT 1
        """, [symbol, country, country, d, d]).fetchone()
        if row:
            return row[0]
        row = self.con.execute("""SELECT h.security_id FROM symbol_history h JOIN securities s USING (security_id)
                                  WHERE upper(h.symbol)=upper(?) AND (? IS NULL OR s.country = ?)
                                  ORDER BY h.valid_to DESC NULLS FIRST LIMIT 1""", [symbol, country, country]).fetchone()
        return row[0] if row else None

    def universe(self, on: str | date, country: str | None = None, markets: Iterable[str] | None = None,
                 security_types: Iterable[str] | None = None, usable_only: bool = True) -> pd.DataFrame:
        """해당 거래일에 실제 시세가 있는 증권 (이후 상장폐지 종목 포함, 그날의 이름·시장 사용)."""
        d = _d(on)
        df = self.con.execute(f"""
            SELECT p.security_id, p.country, p.market, p.symbol, p.name, s.security_type, p.close, p.volume,
                   p.market_cap, p.quality_grade
            FROM prices p JOIN securities s USING (security_id)
            WHERE p.trade_date = ? {"AND p.usable" if usable_only else ""}
              AND (? IS NULL OR p.country = ?)
        """, [d, country, country]).df()
        if markets:
            df = df[df["market"].isin(list(markets))]
        if security_types:
            df = df[df["security_type"].isin(list(security_types))]
        return df.reset_index(drop=True)

    def listing_status(self, security_id: str, on: str | date | None = None) -> dict:
        """as_of 시점 기준 상태. as_of 이후의 상장폐지는 알려주지 않는다."""
        d = _d(on)
        s = self.con.execute("SELECT * FROM securities WHERE security_id=?", [security_id]).df()
        if s.empty:
            return {}
        r = s.iloc[0]
        ev = self.con.execute("SELECT * FROM listing_events WHERE security_id=? AND event_date <= ? ORDER BY event_date",
                              [security_id, d]).df()
        first, last = r["first_trade_date"], r["last_trade_date"]
        if pd.notna(first) and d < pd.Timestamp(first).date():
            st = "not_yet_listed"
        elif pd.notna(r["delisting_date"]) and d >= pd.Timestamp(r["delisting_date"]).date():
            st = "delisted"
        elif pd.notna(last) and d > pd.Timestamp(last).date():
            st = "no_longer_trading"
        else:
            st = "listed"
        return {"security_id": security_id, "status_as_of": st, "events_known": ev.to_dict("records")}

    # ------------------------------------------------------------------ prices
    def _factor_table(self, ids: list[str], adjust: Adjust, as_of: date) -> None:
        """조정계수: 이 날짜 '이전' 가격에 곱할 값. as_of 까지 알려진 기업행위만 사용."""
        types = {"split": ("split", "reverse_split"),
                 "all_corporate": ("split", "reverse_split", "base_price_adjustment"),
                 "total_return": ("split", "reverse_split", "base_price_adjustment", "dividend")}[adjust]
        self.con.execute("CREATE OR REPLACE TEMP TABLE _ids AS SELECT unnest(?::VARCHAR[]) AS security_id", [ids])
        self.con.execute(f"""
            CREATE OR REPLACE TEMP TABLE _ev AS
            WITH ca AS (
                SELECT c.* FROM corporate_actions c JOIN _ids USING (security_id)
                WHERE c.action_type IN ({",".join(repr(t) for t in types)}) AND c.known_from <= ? AND c.ex_date <= ?
            ), pc AS (
                SELECT ca.*, p.close AS prev_close FROM ca ASOF LEFT JOIN
                    (SELECT security_id, trade_date, close FROM prices JOIN _ids USING (security_id)) p
                  ON ca.security_id = p.security_id AND p.trade_date < ca.ex_date
            )
            SELECT security_id, ex_date,
                   CASE WHEN source = 'yahoo' AND action_type IN ('split', 'reverse_split') THEN 1.0 / ratio
                        WHEN action_type = 'dividend' AND prev_close > 0 THEN 1.0 - cash_amount / prev_close
                        ELSE ratio END AS f
            FROM pc WHERE (ratio IS NOT NULL AND ratio > 0) OR action_type = 'dividend'
        """, [as_of, as_of])
        self.con.execute("""
            CREATE OR REPLACE TEMP TABLE _fac AS
            SELECT security_id, ex_date,
                   exp(sum(ln(f)) OVER (PARTITION BY security_id ORDER BY ex_date DESC
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)) AS cum
            FROM (SELECT security_id, ex_date, exp(sum(ln(f))) AS f FROM _ev WHERE f > 0 GROUP BY ALL)
        """)

    def prices(self, ids: str | Sequence[str], start: str | date | None = None, end: str | date | None = None,
               adjust: Adjust = "raw", as_of: str | date | None = None, usable_only: bool = False,
               columns: Sequence[str] | None = None) -> pd.DataFrame:
        """일봉 조회.

        adjust
          raw            당시 실제 거래가격 (KR marcap 원본, US 는 분할 이력으로 역산)
          split          분할·병합만 반영한 비교용 가격
          all_corporate  분할·무상/유상증자 등 거래소 기준가 조정 전부 반영 (KRX 수정주가 개념)
          total_return   + 배당 재투자 (배당 데이터가 있는 원천만; KR marcap 은 배당 정보 없음)
        as_of: 기본값 = end. as_of 이후 거래일은 반환하지 않고, as_of 이후 알려진 기업행위는 반영하지 않는다.
        주의: 조정 가격은 비교·수익률용이다. 실제 주문·체결 시뮬레이션에는 adjust='raw' 를 사용한다.
        """
        ids = [ids] if isinstance(ids, str) else list(ids)
        end_d = _d(end)
        as_of_d = _d(as_of) if as_of is not None else end_d
        last = min(end_d, as_of_d)
        start_d = _d(start) if start else date(1900, 1, 1)
        cols = list(columns) if columns else list(PRICE_COLUMNS)
        for need in ("trade_date", "security_id"):
            if need not in cols:
                cols.insert(0, need)
        base = f"""
            SELECT {", ".join("p." + c for c in cols if c in PRICE_COLUMNS)}
            FROM prices p WHERE p.security_id IN (SELECT unnest(?::VARCHAR[]))
              AND p.trade_date BETWEEN ? AND ? {"AND p.usable" if usable_only else ""}
        """
        if adjust == "raw":
            return self.con.execute(base + " ORDER BY p.security_id, p.trade_date", [ids, start_d, last]).df()
        self._factor_table(ids, adjust, as_of_d)
        df = self.con.execute(f"""
            WITH b AS ({base})
            SELECT b.*, coalesce(f.cum, 1.0) AS adj_factor
            FROM b ASOF LEFT JOIN _fac f ON b.security_id = f.security_id AND f.ex_date > b.trade_date
            ORDER BY b.security_id, b.trade_date
        """, [ids, start_d, last]).df()
        for c in ("open", "high", "low", "close"):
            if c in df:
                df[c] = df[c] * df["adj_factor"]
        if "volume" in df:
            df["volume"] = df["volume"] / df["adj_factor"]
        df["price_basis"] = f"adjusted:{adjust}@{as_of_d.isoformat()}"
        return df

    def returns(self, ids: str | Sequence[str], start=None, end=None, as_of=None, total_return: bool = False) -> pd.DataFrame:
        adj: Adjust = "total_return" if total_return else "all_corporate"
        df = self.prices(ids, start, end, adjust=adj, as_of=as_of, columns=["security_id", "trade_date", "close", "usable"])
        df["ret"] = df.groupby("security_id")["close"].pct_change()
        return df

    def panel(self, start, end, country: str | None = None, field: str = "close", usable_only: bool = True) -> pd.DataFrame:
        """날짜 × 종목 피벗 (원본 가격). 큰 기간은 메모리에 주의 — 필요한 기간만 요청."""
        assert field in PRICE_COLUMNS
        df = self.con.execute(f"""
            SELECT trade_date, security_id, {field} FROM prices
            WHERE trade_date BETWEEN ? AND ? AND (? IS NULL OR country = ?) {"AND usable" if usable_only else ""}
        """, [_d(start), _d(end), country, country]).df()
        return df.pivot(index="trade_date", columns="security_id", values=field)

    # ------------------------------------------------------------------ others
    def corporate_actions(self, ids: str | Sequence[str], as_of=None) -> pd.DataFrame:
        ids = [ids] if isinstance(ids, str) else list(ids)
        return self.con.execute("""SELECT * FROM corporate_actions WHERE security_id IN (SELECT unnest(?::VARCHAR[]))
                                   AND known_from <= ? ORDER BY security_id, ex_date""", [ids, _d(as_of)]).df()

    def indicators(self, ids: str | Sequence[str] | None = None, start=None, end=None, as_of: str | datetime | None = None,
                   as_of_tz: str = "Asia/Seoul") -> pd.DataFrame:
        """지표 조회. as_of 가 날짜면 그 날짜 23:59(as_of_tz) 까지 실제로 이용 가능했던 값만."""
        try:
            self.con.execute("SELECT 1 FROM indicators LIMIT 1")
        except Exception:
            return pd.DataFrame()
        ids_l = None if ids is None else ([ids] if isinstance(ids, str) else list(ids))
        if as_of is None:
            cutoff = datetime.now(timezone.utc)
        elif isinstance(as_of, datetime):
            cutoff = as_of if as_of.tzinfo else as_of.replace(tzinfo=ZoneInfo(as_of_tz))
        else:
            cutoff = datetime.combine(_d(as_of), dtime(23, 59), ZoneInfo(as_of_tz))
        return self.con.execute("""
            SELECT * FROM indicators
            WHERE (? IS NULL OR indicator_id IN (SELECT unnest(?::VARCHAR[])))
              AND obs_date BETWEEN ? AND ? AND available_at <= ?
            ORDER BY indicator_id, obs_date
        """, [ids_l, ids_l, _d(start) if start else date(1900, 1, 1), _d(end), cutoff]).df()

    def indicator_list(self) -> pd.DataFrame:
        try:
            return self.con.execute("""SELECT indicator_id, any_value(name) name, any_value(source) source,
                                       min(obs_date) first, max(obs_date) last, count(*) n
                                       FROM indicators GROUP BY 1 ORDER BY 1""").df()
        except Exception:
            return pd.DataFrame()

    def financial_facts(self, cik: str, concepts: Sequence[str] | None = None, as_of=None) -> pd.DataFrame:
        """as_of 에 알려진 재무 사실. 같은 (개념, 단위, 기간)이 정정되면 as_of 이전 최신 제출본만."""
        try:
            self.con.execute("SELECT 1 FROM financial_facts LIMIT 1")
        except Exception:
            return pd.DataFrame()
        return self.con.execute("""
            SELECT * FROM financial_facts
            WHERE cik = ? AND known_from <= ? AND (? IS NULL OR concept IN (SELECT unnest(?::VARCHAR[])))
            QUALIFY row_number() OVER (PARTITION BY taxonomy, concept, unit, period_start, period_end
                                       ORDER BY filed DESC, accn DESC) = 1
            ORDER BY concept, period_end
        """, [str(cik).zfill(10), _d(as_of), concepts and list(concepts), concepts and list(concepts)]).df()

    def calendar(self, market: str, start=None, end=None) -> list[date]:
        rows = self.con.execute("SELECT trade_date FROM trading_calendars WHERE market=? AND trade_date BETWEEN ? AND ? ORDER BY 1",
                                [market, _d(start) if start else date(1900, 1, 1), _d(end)]).fetchall()
        return [r[0] for r in rows]
