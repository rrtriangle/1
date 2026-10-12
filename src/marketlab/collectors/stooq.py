"""Stooq: 종목별 CSV 및 사용자가 직접 내려받은 대량 DB(zip) 가져오기.

- stooq.com 은 일괄 DB(https://stooq.com/db/h/) 다운로드에 사람 확인(CAPTCHA)을 요구한다.
  자동 우회하지 않는다. 사용자가 브라우저로 받은 d_us_txt.zip 등을 `import-stooq` 로 가져온다.
- 종목별 CSV 엔드포인트가 API 키 요구·HTML 응답 등으로 막히면 원천을 'blocked' 로 기록하고 멈춘다.
- Stooq 가격은 분할(및 배당) 조정값으로 알려져 있으며 원본 체결가가 아니다 → price_basis='stooq_adjusted'.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv

from ..config import get_settings
from ..logging_setup import get_logger, log_event
from ..net.http import HttpClient, SourceBlocked
from ..storage.diskguard import DiskGuard, StorageRisk
from ..storage.parquet_io import sha256_file, write_table_atomic
from .base import Run, StopRequested, record_manifest, record_source_check, stop_requested, upsert_items

log = get_logger("stooq")
SOURCE = "stooq"

SCHEMA = pa.schema([
    ("stooq_symbol", pa.string()), ("market_folder", pa.string()), ("trade_date", pa.date32()),
    ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()), ("close", pa.float64()),
    ("volume", pa.float64()), ("open_interest", pa.float64()), ("source_file", pa.string()),
    ("imported_at", pa.timestamp("us", tz="UTC")),
])


def raw_dir() -> Path:
    return get_settings().paths.raw / "stooq"


def probe(symbol: str = "aapl.us") -> tuple[bool, str]:
    """종목별 CSV 접근 가능 여부 확인. 우회 시도 없음."""
    cfg = get_settings().source(SOURCE)
    client = HttpClient(SOURCE)
    try:
        r = client.get(cfg["csv_url"].format(symbol=symbol))
    except Exception as e:
        record_source_check(SOURCE, False, None, str(e))
        return False, str(e)
    head = r.text[:300]
    ok = r.status_code == 200 and head.lower().startswith("date,open")
    detail = "csv ok" if ok else f"HTTP {r.status_code}: {head[:200]!r}"
    record_source_check(SOURCE, ok, r.status_code, detail)
    return ok, detail


def to_stooq_symbol(ticker: str) -> str:
    return ticker.lower().replace(".", "-").replace("$", "-") + ".us"


def collect_symbols(tickers: list[str]) -> dict:
    """종목별 CSV (현재 상장 종목 교차검증용). 접근이 막히면 즉시 안전 중단."""
    cfg = get_settings().source(SOURCE)
    client = HttpClient(SOURCE)
    ok, detail = probe()
    run = Run(SOURCE, "US", "per_symbol").start()
    if not ok:
        run.finish("blocked", f"Stooq CSV 접근 불가: {detail}")
        return {"status": "blocked", "detail": detail}
    frames, updates = [], []
    try:
        for t in tickers:
            if stop_requested():
                raise StopRequested("중지 요청")
            sym = to_stooq_symbol(t)
            r = client.get(cfg["csv_url"].format(symbol=sym))
            if r.status_code != 200 or not r.text.lower().startswith("date,open"):
                updates.append((sym, "failed", None, None, 0, r.text[:200]))
                run.items_failed += 1
                if "apikey" in r.text.lower() or "captcha" in r.text.lower():
                    raise SourceBlocked("Stooq 가 API 키/사람 확인을 요구함 - 우회하지 않고 중단")
                continue
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [c.lower() for c in df.columns]
            out = pd.DataFrame({
                "stooq_symbol": sym, "market_folder": "per_symbol",
                "trade_date": pd.to_datetime(df["date"]).dt.date,
                "open": df.get("open"), "high": df.get("high"), "low": df.get("low"), "close": df.get("close"),
                "volume": df.get("volume"), "open_interest": None, "source_file": "csv_endpoint",
                "imported_at": datetime.now(timezone.utc)})
            frames.append(out)
            updates.append((sym, "ok", out["trade_date"].min(), out["trade_date"].max(), len(out), None))
            run.items_ok += 1
        if frames:
            df = pd.concat(frames, ignore_index=True)
            path = raw_dir() / "per_symbol" / f"{date.today().isoformat()}-{run.run_id}.parquet"
            run.bytes_written = write_table_atomic(pa.Table.from_pandas(df, schema=SCHEMA, preserve_index=False), path)
            run.rows_written = len(df)
        upsert_items(SOURCE, updates)
        run.finish("success" if not run.items_failed else "partial")
    except (StopRequested, SourceBlocked) as e:
        upsert_items(SOURCE, updates)
        run.finish("blocked" if isinstance(e, SourceBlocked) else "stopped", str(e))
    return {"status": run.status, "ok": run.items_ok, "failed": run.items_failed}


_NAME_RE = re.compile(r"(?P<sym>[^/]+)\.txt$", re.I)


def import_zip(zip_path: Path, batch_rows: int = 2_000_000) -> dict:
    """사용자가 받은 Stooq 대량 zip 을 압축 해제 없이 스트리밍으로 Parquet 변환.

    zip 원본은 data/raw/stooq/bulk/ 로 복사(보존)하고, 변환 결과는 같은 폴더의 parts/ 에 쓴다.
    """
    s = get_settings()
    zip_path = Path(zip_path).expanduser()
    size = zip_path.stat().st_size
    with zipfile.ZipFile(zip_path) as zf:
        uncompressed = sum(i.file_size for i in zf.infolist())
    planned = size + int(uncompressed * 0.18)  # 실측: 텍스트 대비 zstd Parquet 약 15~20%
    guard = DiskGuard(f"Stooq 대량 파일 가져오기 ({zip_path.name})", planned, peak_temp_bytes=0)
    guard.preflight()
    run = Run(SOURCE, "US", "bulk_import").start()
    dest_dir = raw_dir() / "bulk" / zip_path.stem
    dest_dir.mkdir(parents=True, exist_ok=True)
    kept = dest_dir / zip_path.name
    if not kept.exists():
        import shutil
        shutil.copy2(zip_path, kept)
    record_manifest(SOURCE, f"bulk/{zip_path.name}", "user_download", None, kept, size, sha256_file(kept),
                    None, None, "ok")
    buf: list[pa.Table] = []
    buf_rows, part = 0, 0
    read_opts = pacsv.ReadOptions(autogenerate_column_names=False)
    try:
        with zipfile.ZipFile(kept) as zf:
            members = [i for i in zf.infolist() if i.filename.lower().endswith(".txt") and not i.is_dir()]
            run.items_total = len(members)
            for i, info in enumerate(members):
                if stop_requested():
                    raise StopRequested("중지 요청")
                m = _NAME_RE.search(info.filename)
                if not m or info.file_size == 0:
                    continue
                folder = "/".join(info.filename.split("/")[1:-1])
                try:
                    with zf.open(info) as fh:
                        tbl = pacsv.read_csv(fh, read_options=read_opts)
                    cols = {c.strip("<>").lower(): c for c in tbl.column_names}
                    d = pa.compute.strptime(tbl[cols["date"]].cast(pa.string()), format="%Y%m%d", unit="s")
                    n = tbl.num_rows
                    out = pa.table({
                        "stooq_symbol": pa.array([m.group("sym").lower()] * n),
                        "market_folder": pa.array([folder] * n),
                        "trade_date": d.cast(pa.date32()),
                        "open": tbl[cols["open"]].cast(pa.float64()), "high": tbl[cols["high"]].cast(pa.float64()),
                        "low": tbl[cols["low"]].cast(pa.float64()), "close": tbl[cols["close"]].cast(pa.float64()),
                        "volume": tbl[cols["vol"]].cast(pa.float64()) if "vol" in cols else pa.nulls(n, pa.float64()),
                        "open_interest": tbl[cols["openint"]].cast(pa.float64()) if "openint" in cols else pa.nulls(n, pa.float64()),
                        "source_file": pa.array([info.filename] * n),
                        "imported_at": pa.array([datetime.now(timezone.utc)] * n, pa.timestamp("us", tz="UTC")),
                    }, schema=SCHEMA)
                    buf.append(out)
                    buf_rows += n
                    run.items_ok += 1
                except Exception as e:
                    run.items_failed += 1
                    log_event(log, "parse failed", file=info.filename, error=str(e))
                if buf_rows >= batch_rows:
                    guard.checkpoint(run.bytes_written)
                    run.bytes_written += write_table_atomic(pa.concat_tables(buf), dest_dir / "parts" / f"part-{part:05d}.parquet")
                    run.rows_written += buf_rows
                    buf, buf_rows, part = [], 0, part + 1
                    run.message = f"{i + 1}/{len(members)}"
                    run.progress()
        if buf:
            run.bytes_written += write_table_atomic(pa.concat_tables(buf), dest_dir / "parts" / f"part-{part:05d}.parquet")
            run.rows_written += buf_rows
        run.finish("success" if not run.items_failed else "partial")
    except (StopRequested, StorageRisk) as e:
        run.finish("stopped", str(e))
        raise
    return {"files": run.items_ok, "failed": run.items_failed, "rows": run.rows_written, "bytes": run.bytes_written}
