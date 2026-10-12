"""미국 일봉: yfinance (Yahoo Finance 비공식 API).

가격 개념 (중요)
- Yahoo 의 Open/High/Low/Close(auto_adjust=False)는 '주식분할 반영' 가격이다. 당시 실제 체결가가 아니다.
- Adj Close 는 분할 + 배당 반영 (총수익률 비교용).
- 원본에는 받은 값을 그대로 저장하고, 표준화 단계에서 분할 이력으로 당시 거래가격을 역산한다.

증분 갱신
- 마지막 성공일 - N 거래일부터 다시 받아 최근 수정을 반영한다.
- 그 사이 새 분할이 있으면 과거 전체 값이 다시 조정되므로 해당 종목은 전체 이력을 다시 받는다.
- Yahoo 는 현재 티커 보유 기업의 이력만 돌려준다. 상장폐지 종목은 대부분 조회되지 않는다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa

from ..config import get_settings
from ..logging_setup import get_logger, log_event
from ..net.http import SourceBlocked
from ..storage.catalog import catalog
from ..storage.diskguard import DiskGuard, StorageRisk
from ..storage.parquet_io import write_table_atomic
from .base import (Run, StopRequested, get_item_states, stop_requested, upsert_items)

log = get_logger("yfinance")
SOURCE = "yfinance"
DEFAULT_BYTES_PER_ROW = 24       # 실측 전 추정치 (zstd Parquet). 첫 배치 후 실측값으로 대체
DEFAULT_ROWS_PER_SYMBOL = 4500

RAW_SCHEMA = pa.schema([
    ("security_id", pa.string()), ("yahoo_symbol", pa.string()), ("trade_date", pa.date32()),
    ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()), ("close", pa.float64()),
    ("adj_close", pa.float64()), ("volume", pa.float64()), ("dividends", pa.float64()),
    ("stock_splits", pa.float64()), ("fetched_at", pa.timestamp("us", tz="UTC")), ("run_id", pa.string()),
    ("fetch_kind", pa.string()),
])


def raw_dir() -> Path:
    return get_settings().paths.raw / "yfinance" / "daily"


def measured_bytes_per_row() -> float:
    p = get_settings().paths.state_dir / "yfinance_bytes_per_row.txt"
    try:
        return float(p.read_text())
    except Exception:
        return DEFAULT_BYTES_PER_ROW


def _save_bytes_per_row(v: float) -> None:
    p = get_settings().paths.state_dir / "yfinance_bytes_per_row.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{v:.2f}")


def targets(include_types: list[str] | None = None) -> list[tuple[str, str]]:
    """(security_id, yahoo_symbol) 목록: 현재 상장 + 과거에 수집 이력이 있는 미국 증권."""
    with catalog(read_only=True) as con:
        rows = con.execute("""
            SELECT s.security_id, h.symbol, s.security_type
            FROM securities s JOIN symbol_history h
              ON h.security_id = s.security_id AND h.symbol_type = 'yahoo'
            WHERE s.country = 'US'
            QUALIFY row_number() OVER (PARTITION BY s.security_id ORDER BY h.valid_to DESC NULLS FIRST) = 1
            ORDER BY s.security_id
        """).fetchall()
    if include_types:
        rows = [r for r in rows if (r[2] or "other") in include_types or r[2] == "fund_other"]
    return [(r[0], r[1]) for r in rows]


def fetch_history(symbol: str, start: date | None) -> tuple[pd.DataFrame, str | None]:
    """반환: (DataFrame, 오류문자열). 빈 결과는 상장폐지/미지원일 수 있다."""
    import yfinance as yf

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    t = yf.Ticker(symbol)
    try:
        if start is None:
            df = t.history(period="max", interval="1d", auto_adjust=False, actions=True, raise_errors=True)
        else:
            df = t.history(start=start.isoformat(), interval="1d", auto_adjust=False, actions=True,
                           raise_errors=True)
    except Exception as e:  # yfinance 는 오류 유형이 버전마다 달라 문자열로 판단
        msg = f"{type(e).__name__}: {e}"
        return pd.DataFrame(), msg
    return df, None


def _is_rate_limited(err: str | None) -> bool:
    return bool(err) and any(k in err for k in ("RateLimit", "Too Many Requests", "429", "rate limit"))


def _to_rows(df: pd.DataFrame, sid: str, symbol: str, run_id: str, kind: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=RAW_SCHEMA.names)
    idx = df.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("America/New_York")
    out = pd.DataFrame({
        "security_id": sid, "yahoo_symbol": symbol,
        "trade_date": pd.to_datetime(idx).date,
        "open": df.get("Open"), "high": df.get("High"), "low": df.get("Low"), "close": df.get("Close"),
        "adj_close": df.get("Adj Close"), "volume": df.get("Volume"),
        "dividends": df.get("Dividends", 0.0), "stock_splits": df.get("Stock Splits", 0.0),
    }).reset_index(drop=True)
    out["fetched_at"] = datetime.now(timezone.utc)
    out["run_id"] = run_id
    out["fetch_kind"] = kind
    return out


def _record_actions(rows: pd.DataFrame) -> None:
    acts = []
    for r in rows.itertuples(index=False):
        if r.stock_splits and r.stock_splits not in (0.0, 1.0):
            kind = "split" if r.stock_splits > 1 else "reverse_split"
            acts.append((r.security_id, r.trade_date, kind, float(r.stock_splits), None, "USD", r.trade_date,
                         "yahoo", "reported", f"{r.yahoo_symbol} split ratio {r.stock_splits}"))
        if r.dividends and r.dividends > 0:
            acts.append((r.security_id, r.trade_date, "dividend", None, float(r.dividends), "USD", r.trade_date,
                         "yahoo", "reported", "Yahoo 배당 (분할 조정 금액, 공시일 미제공 → known_from=배당락일)"))
    if acts:
        with catalog() as cat:
            cat.executemany("INSERT OR REPLACE INTO corporate_actions VALUES (?,?,?,?,?,?,?,?,?,?)", acts)


def _flush(buf: list[pd.DataFrame], run: Run, part_no: int) -> int:
    if not buf:
        return 0
    df = pd.concat(buf, ignore_index=True)
    table = pa.Table.from_pandas(df, schema=RAW_SCHEMA, preserve_index=False)
    out = raw_dir() / f"fetch_date={date.today().isoformat()}" / f"part-{run.run_id}-{part_no:05d}.parquet"
    n = write_table_atomic(table, out)
    run.bytes_written += n
    run.rows_written += len(df)
    if len(df) > 1000:
        _save_bytes_per_row(n / len(df))
    _record_actions(df)
    return n


def _revalidate_today(sid: str, cycle: int) -> bool:
    """주기적 재검증: 종목별로 cycle 일에 한 번 전체 이력을 다시 받아 과거 수정 여부를 확인 (상태 없음)."""
    import zlib
    return zlib.crc32(sid.encode()) % cycle == date.today().toordinal() % cycle


def _compare_with_existing(sid: str, rows: pd.DataFrame, tol: float) -> int:
    """다시 받은 이력과 기존 표준화 데이터(분할 반영 종가)를 비교해 과거 수정 건수를 기록."""
    from ..storage.catalog import memory_connection

    std = get_settings().paths.standardized / "daily_prices" / "country=US"
    if not any(std.glob("year=*/*.parquet")) or rows.empty:
        return 0
    con = memory_connection()
    try:
        con.register("new_rows", rows[["trade_date", "close"]])
        diff = con.execute(f"""
            SELECT o.trade_date, o.split_adj_close, n.close
            FROM read_parquet('{(std / "year=*" / "*.parquet").as_posix()}', hive_partitioning=true) o
            JOIN new_rows n ON n.trade_date = o.trade_date
            WHERE o.security_id = ? AND abs(o.split_adj_close / nullif(n.close, 0) - 1) > ?
        """, [sid, tol]).fetchall()
    finally:
        con.close()
    if diff:
        with catalog() as cat:
            cat.execute("INSERT INTO data_quality_issues VALUES (?, 'source_revision', 'info', 'US', ?, NULL, ?, 'yfinance', ?, 'supplemented', now())",
                        ["revalidate", sid, diff[0][0], f"{len(diff)}일 값 변경 (예: {diff[0][1]} → {diff[0][2]}) - 최신 수집본으로 대체, 원본 보존"])
    return len(diff)


def collect(mode: str = "incremental", symbols: list[str] | None = None, limit: int | None = None,
            retry_failed_only: bool = False, revalidate: bool = False) -> dict:
    s = get_settings()
    cfg = s.source(SOURCE)
    recheck = int(s.get("update.recheck_recent_trading_days", 5))
    cycle = max(1, int(s.get("update.revalidate_every_days", 30)))
    tol = float(s.get("validation.cross_source_tolerance", 0.005))
    batch_size = int(cfg.get("batch_size", 200))
    min_interval = float(cfg.get("min_interval_sec", 1.2))
    all_targets = targets(cfg.get("security_types"))
    if symbols:
        wanted = {x.upper() for x in symbols}
        all_targets = [t for t in all_targets if t[1].upper() in wanted]
    states = get_item_states(SOURCE)
    todo = []
    for sid, sym in all_targets:
        st = states.get(sid)
        if retry_failed_only and not (st and st["status"] in ("failed", "blocked")):
            continue
        if mode == "full" and st and st["status"] == "ok":
            continue  # 전체 수집 재개: 이미 전체 이력을 받은 종목은 건너뜀 (이후는 증분·재검증이 담당)
        todo.append((sid, sym, st))
    if limit:
        todo = todo[:limit]

    run = Run(SOURCE, "US", mode).start()
    run.items_total = len(todo)
    est_rows = sum(DEFAULT_ROWS_PER_SYMBOL if not (st and st["status"] == "ok") else 30 for _, _, st in todo)
    planned = int(est_rows * measured_bytes_per_row() * 2.2)  # 원본 + 표준화
    guard = DiskGuard("yfinance 미국 일봉", planned)
    buf: list[pd.DataFrame] = []
    part_no, written, consecutive_rl = 0, 0, 0
    item_updates: list[tuple] = []
    import time as _t
    try:
        guard.preflight()
        for i, (sid, sym, st) in enumerate(todo):
            if stop_requested():
                raise StopRequested("사용자 중지 요청")
            t0 = _t.monotonic()
            incremental = mode != "full" and st and st["status"] == "ok" and st["last_success_date"]
            reval = bool(incremental and revalidate and _revalidate_today(sid, cycle))
            if reval:
                incremental = False
            start = (st["last_success_date"] - timedelta(days=int(recheck * 1.6) + 3)) if incremental else None
            df, err = fetch_history(sym, start)
            kind = "incremental" if start else ("revalidate" if reval else "full")
            if incremental and err is None and not df.empty and "Stock Splits" in df \
                    and (df["Stock Splits"].fillna(0).replace(1.0, 0.0) != 0).any():
                # 새 분할 → 과거 전체 가격이 다시 조정됨 → 전체 이력 재수집
                df, err = fetch_history(sym, None)
                kind = "full_resplit"
            if _is_rate_limited(err):
                consecutive_rl += 1
                item_updates.append((sid, "blocked", None, None, 0, err[:300]))
                run.items_failed += 1
                wait = min(60 * consecutive_rl, 900)
                log_event(log, "rate limited", symbol=sym, wait=wait, n=consecutive_rl)
                if consecutive_rl >= int(s.get("http.circuit_breaker_threshold", 8)):
                    raise SourceBlocked(f"Yahoo 호출 제한이 지속됨 ({consecutive_rl}회) - 안전 중단, 나중에 재개하세요")
                _t.sleep(wait)
                continue
            consecutive_rl = 0
            if err or df.empty:
                status = "empty" if (not err or "No data" in err or "delisted" in err.lower()) else "failed"
                item_updates.append((sid, status, None, None, 0, (err or "no rows")[:300]))
                run.items_failed += 1
            else:
                rows = _to_rows(df, sid, sym, run.run_id, kind)
                if kind in ("revalidate", "full_resplit"):
                    _compare_with_existing(sid, rows, tol)
                buf.append(rows)
                item_updates.append((sid, "ok", min(rows["trade_date"]), max(rows["trade_date"]), len(rows), None))
                run.items_ok += 1
            if sum(len(b) for b in buf) and (len(buf) >= batch_size):
                guard.checkpoint(written)
                written += _flush(buf, run, part_no)
                part_no += 1
                buf = []
                upsert_items(SOURCE, item_updates)
                item_updates = []
                run.progress()
            if i % 50 == 0:
                run.message = f"{i + 1}/{len(todo)} ({sym})"
                run.progress()
            wait = min_interval - (_t.monotonic() - t0)
            if wait > 0:
                _t.sleep(wait)
        written += _flush(buf, run, part_no)
        buf = []
        upsert_items(SOURCE, item_updates)
        item_updates = []
        run.finish("success" if run.items_failed == 0 else "partial",
                   f"ok {run.items_ok}, failed/empty {run.items_failed}")
    except (StopRequested, SourceBlocked, StorageRisk) as e:
        # 지금까지 받은 데이터와 상태를 안전하게 저장한 뒤 중단
        try:
            _flush(buf, run, part_no)
            upsert_items(SOURCE, item_updates)
        finally:
            status = "stopped" if isinstance(e, (StopRequested, StorageRisk)) else "blocked"
            run.finish(status, str(e))
        if isinstance(e, StorageRisk):
            raise
    except Exception as e:
        try:
            _flush(buf, run, part_no)
            upsert_items(SOURCE, item_updates)
        finally:
            run.finish("failed", str(e))
        raise
    return {"run_id": run.run_id, "status": run.status, "ok": run.items_ok, "failed": run.items_failed,
            "rows": run.rows_written}
