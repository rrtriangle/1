"""분봉(1분봉) 수집: yfinance (Yahoo Finance 비공식 API). 한국·미국 공통, API 키 불필요.

왜 매일 쌓아야 하나
- Yahoo 는 1분봉을 최근 약 30일치만 보관하고, 한 번에 최대 8일 구간만 돌려준다.
- 과거 분봉은 무료로 구할 수 없으므로, 사라지기 전에 조금씩 받아 직접 쌓는 수밖에 없다.

동작 방식
- 종목마다 '어느 날짜까지 받았는지'(ingestion_items.last_success_date)를 기록하고 그 다음 날부터 받는다.
- 장이 끝난 날(확정된 세션)의 봉만 저장한다 → 같은 봉이 두 번 저장되지 않는다(용량 절약).
- 한 번 실행에 시간 예산(max_runtime_min)을 두고, 가장 오래 못 받은 종목부터 처리한다.
  전 종목을 하루에 다 못 돌아도 며칠에 걸쳐 순환하며, 30일 안에만 다시 방문하면 빠지는 봉이 없다.
- 거래가 없던 분(거래량 0)은 기본적으로 저장하지 않는다(drop_zero_volume). 직전 종가로 채워 복원할 수 있다.

한계 (중요)
- Yahoo 한국 분봉은 KRX 정규장 기준이며 약 15~20분 지연 데이터다. NXT(대체거래소) 체결은 포함되지 않는다.
- 비공식 원천이라 드물게 누락·오류 봉이 있다. 정밀한 체결 연구에는 증권사 API(예: 한국투자증권) 교차검증이 필요하다.
- 가격은 받은 시점의 값 그대로다. 수집 뒤에 액면분할이 생기면 그 이전 분봉은 분할 전 가격이다.
- 코넥스 종목은 Yahoo 에 없어 제외한다.
"""
from __future__ import annotations

import logging
import time as _t
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from ..config import get_settings
from ..logging_setup import get_logger, log_event
from ..net.http import SourceBlocked
from ..storage.catalog import catalog
from ..storage.diskguard import DiskGuard, StorageRisk
from .base import Run, StopRequested, stop_requested, upsert_items
from .yfinance_us import _is_network_failure, _is_rate_limited, yahoo_alive

log = get_logger("yfinance_minute")
SOURCE = "yfinance_minute"
INTERVAL = "1m"
EST_BARS_PER_SESSION = 300       # 종목·거래일당 평균 봉 수 추정 (거래 없는 분 제외)
EST_BYTES_PER_ROW = 16           # zstd Parquet 추정치
COLUMNS = ["security_id", "symbol", "ts", "trade_date", "open", "high", "low", "close", "volume"]

SESSIONS = {
    "KR": {"tz": "Asia/Seoul", "close": "15:30"},
    "US": {"tz": "America/New_York", "close": "16:00"},
}
KR_SUFFIX = {"KOSPI": ".KS", "KOSDAQ": ".KQ", "KOSDAQ GLOBAL": ".KQ"}


def _clock() -> float:
    return _t.monotonic()


# ---------------------------------------------------------------- 순수 함수 (테스트 대상)
def yahoo_symbol_kr(code: str, market: str | None) -> str | None:
    """KRX 종목코드 → Yahoo 심볼. 코넥스 등 Yahoo 에 없는 시장은 None."""
    suffix = KR_SUFFIX.get((market or "").strip().upper())
    return f"{code}{suffix}" if suffix and code else None


def finalized_through(country: str, now: datetime | None = None, delay_min: int = 60) -> tuple[date, date]:
    """(확정된 마지막 세션 날짜, 거래소 현지 오늘 날짜).

    장 마감 + delay_min 이 지나야 그날 분봉을 '확정'으로 본다. 그 전에는 전날까지만 저장한다.
    """
    sess = SESSIONS[country]
    tz = ZoneInfo(sess["tz"])
    local = (now or datetime.now(tz)).astimezone(tz)
    hh, mm = (int(x) for x in sess["close"].split(":"))
    cutoff = datetime.combine(local.date(), dtime(hh, mm), tzinfo=tz) + timedelta(minutes=delay_min)
    through = local.date() if local >= cutoff else local.date() - timedelta(days=1)
    return through, local.date()


def plan_windows(done_through: date | None, through: date, today: date,
                 backfill_days: int = 29, window_days: int = 7) -> list[tuple[date, date]]:
    """받아야 할 구간을 Yahoo 제한(최근 30일 이내, 요청당 8일 이하)에 맞춰 나눈다.

    반환: [(시작일, 끝일-제외)], 오래된 구간부터.
    """
    earliest = today - timedelta(days=backfill_days)
    start = earliest if done_through is None else max(done_through + timedelta(days=1), earliest)
    out: list[tuple[date, date]] = []
    while start <= through:
        end = min(start + timedelta(days=window_days), through + timedelta(days=1))
        out.append((start, end))
        start = end
    return out


def normalize(df: pd.DataFrame, sid: str, symbol: str, country: str, after: date | None, through: date,
              drop_zero_volume: bool = True) -> pd.DataFrame:
    """yfinance 응답 → 저장 형식. after 초과 ~ through 이하(거래소 현지 날짜)의 봉만 남긴다."""
    if df is None or df.empty:
        return pd.DataFrame(columns=COLUMNS)
    tz = SESSIONS[country]["tz"]
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize(tz) if idx.tz is None else idx.tz_convert(tz)
    out = pd.DataFrame({
        "ts": idx.tz_convert("UTC"),
        "trade_date": idx.date,
        "open": pd.to_numeric(df["Open"], errors="coerce").to_numpy(),
        "high": pd.to_numeric(df["High"], errors="coerce").to_numpy(),
        "low": pd.to_numeric(df["Low"], errors="coerce").to_numpy(),
        "close": pd.to_numeric(df["Close"], errors="coerce").to_numpy(),
        "volume": pd.to_numeric(df["Volume"], errors="coerce").fillna(0).to_numpy(),
    })
    out = out[out["close"].notna()]
    keep = out["trade_date"] <= through
    if after is not None:
        keep &= out["trade_date"] > after
    out = out[keep]
    if drop_zero_volume:
        out = out[out["volume"] > 0]
    out = out.drop_duplicates(subset="ts", keep="last").sort_values("ts")
    out["volume"] = out["volume"].astype("int64")
    out.insert(0, "symbol", symbol)
    out.insert(0, "security_id", sid)
    return out[COLUMNS].reset_index(drop=True)


def _is_no_data(err: str | None) -> bool:
    low = (err or "").lower()
    return any(k in low for k in ("no data", "no price data", "delisted", "no timezone"))


# ---------------------------------------------------------------- 원천 호출
def fetch_minutes(symbol: str, start: date, end: date, interval: str = INTERVAL) -> tuple[pd.DataFrame, str | None]:
    """[start, end) 구간의 분봉. 반환: (DataFrame, 오류문자열)."""
    import warnings

    import yfinance as yf

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    warnings.filterwarnings("ignore", message=".*raise_errors.*")
    try:
        df = yf.Ticker(symbol).history(start=start.isoformat(), end=end.isoformat(), interval=interval,
                                       auto_adjust=False, actions=False, prepost=False, raise_errors=True)
    except Exception as e:  # yfinance 는 오류 유형이 버전마다 달라 문자열로 판단
        return pd.DataFrame(), f"{type(e).__name__}: {e}"
    return df, None


# ---------------------------------------------------------------- 대상·상태
def raw_dir(country: str, interval: str = INTERVAL) -> Path:
    return get_settings().paths.raw / SOURCE / f"interval={interval}" / f"country={country}"


def targets(country: str, security_types: list[str] | None = None) -> list[tuple[str, str]]:
    """현재 상장 종목의 (security_id, yahoo_symbol). 종목 목록은 일봉 수집(ingest)이 만든 카탈로그에서 읽는다."""
    symbol_type = "krx_code" if country == "KR" else "yahoo"
    with catalog(read_only=True) as con:
        rows = con.execute("""
            SELECT s.security_id, h.symbol, s.market, s.security_type
            FROM securities s JOIN symbol_history h
              ON h.security_id = s.security_id AND h.symbol_type = ?
            WHERE s.country = ? AND s.status = 'listed'
            QUALIFY row_number() OVER (PARTITION BY s.security_id
                                       ORDER BY h.valid_to DESC NULLS FIRST, h.valid_from DESC NULLS LAST) = 1
            ORDER BY s.security_id
        """, [symbol_type, country]).fetchall()
    out = []
    for sid, sym, market, stype in rows:
        if security_types and (stype or "other") not in security_types:
            continue
        ysym = yahoo_symbol_kr(sym, market) if country == "KR" else sym
        if ysym:
            out.append((sid, ysym))
    return out


def item_states() -> dict[str, dict]:
    """종목별 진행 상태. age_sec = 마지막 시도 후 지난 시간(초)."""
    with catalog(read_only=True) as con:
        rows = con.execute(
            "SELECT item_key, status, last_success_date, date_diff('second', last_attempt_at, now()::TIMESTAMP) "
            "FROM ingestion_items WHERE source = ?", [SOURCE]).fetchall()
    return {r[0]: {"status": r[1], "done": r[2], "age_sec": r[3]} for r in rows}


def build_todo(all_targets: list[tuple[str, str]], states: dict[str, dict], through: date,
               retry_empty_hours: float = 72.0) -> list[tuple[str, str, date | None]]:
    """받을 것이 남은 종목을 '가장 오래 시도하지 않은 순'으로 정렬한다 (한 번도 안 받은 종목이 맨 앞)."""
    todo = []
    for sid, sym in all_targets:
        st = states.get(sid)
        done = st["done"] if st else None
        if done is not None and done >= through:
            continue  # 이미 최신
        age = st["age_sec"] if st and st["age_sec"] is not None else float("inf")
        if st and st["status"] == "empty" and age < retry_empty_hours * 3600:
            continue  # Yahoo 에 데이터가 없는 종목은 가끔씩만 다시 확인
        todo.append((age, sid, sym, done))
    todo.sort(key=lambda x: (-x[0], x[1]))
    return [(sid, sym, done) for _, sid, sym, done in todo]


# ---------------------------------------------------------------- 저장
def _flush(buf: list[pd.DataFrame], run: Run, country: str, part_no: int) -> int:
    buf = [b for b in buf if len(b)]
    if not buf:
        return 0
    import pyarrow as pa

    from ..storage.parquet_io import write_table_atomic

    schema = pa.schema([
        ("security_id", pa.string()), ("symbol", pa.string()), ("ts", pa.timestamp("us", tz="UTC")),
        ("trade_date", pa.date32()), ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()),
        ("close", pa.float64()), ("volume", pa.int64()),
    ])
    df = pd.concat(buf, ignore_index=True).sort_values(["security_id", "ts"], kind="stable")
    table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    out = raw_dir(country) / f"fetch_date={date.today().isoformat()}" / f"part-{run.run_id}-{part_no:05d}.parquet"
    n = write_table_atomic(table, out)
    run.bytes_written += n
    run.rows_written += len(df)
    return n


# ---------------------------------------------------------------- 수집
def collect(market: str = "KR", symbols: list[str] | None = None, limit: int | None = None,
            max_minutes: float | None = None, now: datetime | None = None) -> dict:
    """분봉 증분 수집. 중간에 멈춰도 다음 실행이 종목별 기록부터 이어서 받는다."""
    s = get_settings()
    cfg = s.source(SOURCE)
    if not cfg.get("enabled", True):
        return {"status": "disabled"}
    if market not in SESSIONS:
        raise ValueError(f"지원하지 않는 시장: {market}")
    min_interval = float(cfg.get("min_interval_sec", 1.2))
    backfill_days = min(int(cfg.get("backfill_days", 29)), 29)
    window_days = min(int(cfg.get("window_days", 7)), 8)
    flush_rows = int(cfg.get("flush_rows", 500_000))
    drop_zero = bool(cfg.get("drop_zero_volume", True))
    budget_min = float(max_minutes if max_minutes is not None else cfg.get("max_runtime_min", 60))
    breaker = int(s.get("http.circuit_breaker_threshold", 8))
    through, today = finalized_through(market, now, int(cfg.get("finalize_delay_min", 60)))

    all_targets = targets(market, (cfg.get("security_types") or {}).get(market))
    if symbols:
        wanted = {x.upper() for x in symbols}
        all_targets = [t for t in all_targets if t[1].upper() in wanted or t[1].split(".")[0].upper() in wanted]
    todo = build_todo(all_targets, item_states(), through, float(cfg.get("retry_empty_hours", 72)))
    if limit:
        todo = todo[:limit]

    run = Run(SOURCE, market, "incremental").start()
    run.items_total = len(todo)
    est_rows = sum(len(plan_windows(done, through, today, backfill_days, window_days)) * 5 * EST_BARS_PER_SESSION
                   for _, _, done in todo)
    guard = DiskGuard(f"yfinance {market} 분봉", int(est_rows * EST_BYTES_PER_ROW))
    buf: list[pd.DataFrame] = []
    item_updates: list[tuple] = []
    empty_streak: list[str] = []
    part_no = written = buffered = consecutive_rl = consecutive_net = 0
    started = _clock()
    remaining = 0
    last_call = 0.0

    def save() -> None:
        nonlocal buf, item_updates, part_no, written, buffered
        if buffered:
            guard.checkpoint(written)
            written += _flush(buf, run, market, part_no)
            part_no += 1
        buf, buffered = [], 0
        upsert_items(SOURCE, item_updates)   # 파일을 쓴 뒤에만 '받았다'고 기록한다
        item_updates = []
        run.progress()

    try:
        guard.preflight()
        if todo:
            alive, why = yahoo_alive()
            if not alive:
                raise SourceBlocked(f"Yahoo 응답 없음 (SPY 조회 실패: {why[:200]}) - 접속 차단/네트워크 확인 후 재개하세요")
        for i, (sid, sym, done) in enumerate(todo):
            if stop_requested():
                raise StopRequested("사용자 중지 요청")
            if budget_min > 0 and (_clock() - started) > budget_min * 60:
                remaining = len(todo) - i
                break
            frames: list[pd.DataFrame] = []
            covered: date | None = None
            err: str | None = None
            hard_err: str | None = None
            for w_start, w_end in plan_windows(done, through, today, backfill_days, window_days):
                wait = min_interval - (_clock() - last_call)
                if wait > 0:
                    _t.sleep(wait)
                last_call = _clock()
                df, err = fetch_minutes(sym, w_start, w_end)
                if err is None or _is_no_data(err):
                    # 그 구간에 거래가 없었을 수 있다(신규 상장 전, 거래정지) → 다음 구간 계속
                    if err is None and not df.empty:
                        frames.append(df)
                        covered = w_end - timedelta(days=1)
                    continue
                hard_err = err
                break

            if _is_rate_limited(hard_err):
                consecutive_rl += 1
                item_updates.append((sid, "blocked", None, None, 0, hard_err[:300]))
                run.items_failed += 1
                wait = min(60 * consecutive_rl, 900)
                log_event(log, "rate limited", symbol=sym, wait=wait, n=consecutive_rl)
                if consecutive_rl >= breaker:
                    raise SourceBlocked(f"Yahoo 호출 제한이 지속됨 ({consecutive_rl}회) - 안전 중단, 다음 실행에서 이어받습니다")
                _t.sleep(wait)
                continue
            consecutive_rl = 0
            if _is_network_failure(hard_err):
                consecutive_net += 1
                if consecutive_net >= breaker:
                    item_updates.append((sid, "blocked", None, None, 0, hard_err[:300]))
                    run.items_failed += 1
                    raise SourceBlocked(f"Yahoo 접속 불가가 {consecutive_net}회 연속 - 네트워크 확인 후 재개하세요: {hard_err[:200]}")
            else:
                consecutive_net = 0

            if not frames:
                empty_streak.append(sid)
                if len(empty_streak) % 20 == 0:
                    alive, why = yahoo_alive()
                    if not alive:
                        raise SourceBlocked(f"연속 {len(empty_streak)}건 빈 응답 + SPY 조회 실패 - 차단으로 판단, 중단")
                status = "failed" if hard_err else "empty"
                item_updates.append((sid, status, None, None, 0, (hard_err or err or "no rows")[:300]))
                run.items_failed += 1
            else:
                empty_streak = []
                rows = normalize(pd.concat(frames), sid, sym, market, done, min(covered, through), drop_zero)
                buf.append(rows)
                buffered += len(rows)
                first = min(rows["trade_date"]) if len(rows) else None
                item_updates.append((sid, "ok", first, min(covered, through), len(rows), None))
                run.items_ok += 1
            if buffered >= flush_rows or len(item_updates) >= 1000:
                save()
            elif i % 50 == 0:
                run.message = f"{i + 1}/{len(todo)} ({sym})"
                run.progress()
        save()
        msg = f"ok {run.items_ok}, 실패/빈응답 {run.items_failed}, {through} 까지"
        if remaining:
            msg += f", 시간 예산({budget_min:g}분) 도달 - 남은 {remaining}종목은 다음 실행에서"
        run.finish("success" if run.items_failed == 0 and not remaining else "partial", msg)
    except (StopRequested, SourceBlocked, StorageRisk) as e:
        try:
            if isinstance(e, SourceBlocked) and empty_streak:
                # 차단 중의 빈 응답은 '데이터 없는 종목'으로 기록하지 않는다 (다음 실행에서 재시도)
                skip = set(empty_streak)
                item_updates = [u for u in item_updates if u[0] not in skip]
            if not isinstance(e, StorageRisk):
                save()
        finally:
            run.finish("stopped" if isinstance(e, (StopRequested, StorageRisk)) else "blocked", str(e))
        if isinstance(e, StorageRisk):
            raise
    except Exception as e:
        try:
            save()
        finally:
            run.finish("failed", str(e))
        raise
    return {"run_id": run.run_id, "status": run.status, "ok": run.items_ok, "failed": run.items_failed,
            "rows": run.rows_written, "through": through.isoformat(), "remaining": remaining}
