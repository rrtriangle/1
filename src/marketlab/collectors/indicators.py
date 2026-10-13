"""시장 지수·환율·금리·변동성 (market_indicators).

원천
- KRX 지수 (fdr_krx_data_cache): KOSPI(ks11), KOSDAQ(kq11), KOSPI200(ks200), KRX 1001 — 1995~ (OHLCV, 거래대금, 시총)
- FRED CSV (API 키 불필요): 미국 국채금리, 금리차, VIX, 원/달러, S&P500(최근 10년만 제공) 등
- Yahoo (yfinance): ^GSPC, ^IXIC, ^DJI, ^VIX, ^KS11, ^KQ11, KRW=X 등 장기 일봉

각 관측치에 실제 사용 가능 시점(available_at, UTC)을 붙인다.
- 거래소 일봉: 해당 거래일 마감 + 지연시간 (현지 시간대 → UTC, 서머타임 반영)
- FRED: 관측일 다음 영업일 18:00 America/New_York (발표 시차를 보수적으로 가정, 빈티지 미사용)
"""
from __future__ import annotations

import io
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow as pa

from ..config import get_settings
from ..logging_setup import get_logger, log_event
from ..net.http import HttpClient, SourceBlocked
from ..storage.catalog import memory_connection
from ..storage.parquet_io import sha256_file, write_table_atomic
from . import krx_cache
from .base import Run, record_manifest, record_source_check

log = get_logger("indicators")

SCHEMA = pa.schema([
    ("indicator_id", pa.string()), ("name", pa.string()), ("source", pa.string()), ("market", pa.string()),
    ("currency", pa.string()), ("unit", pa.string()), ("obs_date", pa.date32()),
    ("value", pa.float64()), ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()),
    ("volume", pa.float64()), ("amount", pa.float64()), ("market_cap", pa.float64()),
    ("available_at", pa.timestamp("us", tz="UTC")), ("ingested_at", pa.timestamp("us", tz="UTC")),
])

KRX_INDEX_NAMES = {"ks11": "KOSPI", "kq11": "KOSDAQ", "ks200": "KOSPI 200", "krx_index_1001": "KRX 1001 (코스피)"}
FRED_META = {
    "DGS3MO": ("US Treasury 3M yield", "percent"), "DGS2": ("US Treasury 2Y yield", "percent"),
    "DGS10": ("US Treasury 10Y yield", "percent"), "DGS30": ("US Treasury 30Y yield", "percent"),
    "T10Y2Y": ("10Y-2Y spread", "percent"), "T10Y3M": ("10Y-3M spread", "percent"),
    "DEXKOUS": ("KRW per USD (noon NY)", "KRW"), "VIXCLS": ("CBOE VIX close", "index"),
    "SP500": ("S&P 500 (FRED, 최근 10년)", "index"), "NASDAQCOM": ("NASDAQ Composite", "index"),
    "DJIA": ("Dow Jones (FRED, 최근 10년)", "index"), "BAMLH0A0HYM2": ("US HY OAS", "percent"),
    "DFF": ("Fed Funds effective", "percent"),
}


def out_dir() -> Path:
    return get_settings().paths.standardized / "market_indicators"


def _close_available(d: pd.Series, tz: str, close: str, delay_min: int) -> pd.Series:
    hh, mm = map(int, close.split(":"))
    z = ZoneInfo(tz)
    return pd.Series([datetime.combine(x, dtime(hh, mm), z).astimezone(timezone.utc) + timedelta(minutes=delay_min)
                      for x in d], index=d.index)


def _write(df: pd.DataFrame, name: str) -> int:
    df = df.copy()
    df["ingested_at"] = datetime.now(timezone.utc)
    for c in SCHEMA.names:
        if c not in df:
            df[c] = None
    tbl = pa.Table.from_pandas(df[SCHEMA.names], schema=SCHEMA, preserve_index=False)
    return write_table_atomic(tbl, out_dir() / f"{name}.parquet")


def build_krx_indices() -> dict:
    """원본 CSV(연도별) → 지수별 Parquet. 다운로드는 krx_cache.collect 가 담당."""
    base = krx_cache.raw_dir() / "index"
    res = {}
    con = memory_connection()
    cfg = get_settings().get("update.finalize_after.KR", {}) or {}
    for d in sorted(base.glob("year_*")):
        idx = d.name.removeprefix("year_")
        files = sorted(d.glob("*.csv"))
        if not files:
            continue
        df = con.execute(f"""
            SELECT CAST(Date AS DATE) AS obs_date, CAST(Close AS DOUBLE) AS value, CAST(Open AS DOUBLE) AS open,
                   CAST(High AS DOUBLE) AS high, CAST(Low AS DOUBLE) AS low, CAST(Volume AS DOUBLE) AS volume,
                   CAST(Amount AS DOUBLE) AS amount, try_cast(MarCap AS DOUBLE) AS market_cap
            FROM read_csv([{",".join(repr(f.as_posix()) for f in files)}], header=true, union_by_name=true)
            QUALIFY row_number() OVER (PARTITION BY CAST(Date AS DATE)) = 1
            ORDER BY 1
        """).df()
        df["obs_date"] = pd.to_datetime(df["obs_date"]).dt.date
        df["indicator_id"] = f"KRX:{idx.upper()}"
        df["name"] = KRX_INDEX_NAMES.get(idx, idx)
        df["source"], df["market"], df["currency"], df["unit"] = "krx_cache", "KR", "KRW", "index"
        df["available_at"] = _close_available(df["obs_date"], cfg.get("tz", "Asia/Seoul"),
                                              cfg.get("close", "15:30"), int(cfg.get("delay_min", 180)))
        _write(df, f"krx_{idx}")
        res[idx] = (len(df), str(df["obs_date"].min()), str(df["obs_date"].max()))
    con.close()
    return res


def collect_fred() -> dict:
    s = get_settings()
    cfg = s.source("fred")
    client = HttpClient("fred")
    run = Run("fred", "US", "full").start()
    res = {}
    try:
        for sid in cfg.get("series", []):
            url = cfg["csv_url"].format(series=sid)
            try:
                r = client.get(url)
                if r.status_code != 200 or "," not in r.text[:100]:
                    raise RuntimeError(f"HTTP {r.status_code}")
                raw = s.paths.raw / "fred" / f"{sid}.csv"
                raw.parent.mkdir(parents=True, exist_ok=True)
                raw.write_text(r.text, encoding="utf-8")
                record_manifest("fred", sid, url, None, raw, raw.stat().st_size, sha256_file(raw), None, None, "ok")
                df = pd.read_csv(io.StringIO(r.text), na_values=["."])
                df.columns = ["obs_date", "value"]
                df = df.dropna(subset=["value"])
                df["obs_date"] = pd.to_datetime(df["obs_date"]).dt.date
                name, unit = FRED_META.get(sid, (sid, ""))
                df["indicator_id"], df["name"], df["source"] = f"FRED:{sid}", name, "fred"
                df["market"], df["currency"], df["unit"] = "US", "USD" if unit != "KRW" else "KRW", unit
                nxt = pd.to_datetime(df["obs_date"]) + pd.offsets.BDay(1)
                df["available_at"] = _close_available(nxt.dt.date, "America/New_York", "18:00", 0)
                _write(df, f"fred_{sid}")
                res[sid] = (len(df), str(df["obs_date"].min()), str(df["obs_date"].max()))
                run.items_ok += 1
                run.rows_written += len(df)
            except SourceBlocked:
                raise
            except Exception as e:
                run.items_failed += 1
                log_event(log, "fred failed", series=sid, error=str(e))
        record_source_check("fred", run.items_ok > 0, None, f"{run.items_ok} ok / {run.items_failed} failed")
        run.finish("success" if not run.items_failed else ("partial" if run.items_ok else "failed"))
    except SourceBlocked as e:
        run.finish("blocked", str(e))
    return res


def collect_yahoo_indices() -> dict:
    from .yfinance_us import fetch_history

    s = get_settings()
    cfg = s.source("yahoo_indices")
    run = Run("yahoo_indices", "ALL", "full").start()
    res = {}
    import time as _t
    for sym, meta in (cfg.get("symbols") or {}).items():
        df, err = fetch_history(sym, None)
        if err or df.empty:
            run.items_failed += 1
            log_event(log, "yahoo index failed", symbol=sym, error=err)
            if err and ("RateLimit" in err or "429" in err):
                break
            _t.sleep(2)
            continue
        idx = df.index.tz_convert("America/New_York" if meta.get("market") != "KR" else "Asia/Seoul") \
            if df.index.tz is not None else df.index
        out = pd.DataFrame({
            "obs_date": pd.to_datetime(idx).date, "value": df["Close"].values, "open": df["Open"].values,
            "high": df["High"].values, "low": df["Low"].values, "volume": df["Volume"].values,
        })
        out["indicator_id"], out["name"], out["source"] = f"YAHOO:{sym}", meta.get("name", sym), "yahoo"
        out["market"], out["currency"], out["unit"] = meta.get("market", "US"), meta.get("currency", "USD"), "index"
        if meta.get("market") == "KR":
            kcfg = s.get("update.finalize_after.KR", {}) or {}
            out["available_at"] = _close_available(out["obs_date"], kcfg.get("tz", "Asia/Seoul"),
                                                   kcfg.get("close", "15:30"), int(kcfg.get("delay_min", 180)))
        else:
            ucfg = s.get("update.finalize_after.US", {}) or {}
            out["available_at"] = _close_available(out["obs_date"], ucfg.get("tz", "America/New_York"),
                                                   ucfg.get("close", "16:00"), int(ucfg.get("delay_min", 240)))
        safe = sym.replace("^", "").replace("=", "_")
        raw = s.paths.raw / "yahoo_indices" / f"{safe}.parquet"
        raw.parent.mkdir(parents=True, exist_ok=True)
        df.reset_index().to_parquet(raw, index=False)
        _write(out, f"yahoo_{safe}")
        res[sym] = (len(out), str(out["obs_date"].min()), str(out["obs_date"].max()))
        run.items_ok += 1
        run.rows_written += len(out)
        _t.sleep(float(s.source("yfinance").get("min_interval_sec", 1.2)))
    record_source_check("yahoo", run.items_ok > 0, None, f"{run.items_ok} ok / {run.items_failed} failed")
    run.finish("success" if not run.items_failed else ("partial" if run.items_ok else "failed"))
    return res
