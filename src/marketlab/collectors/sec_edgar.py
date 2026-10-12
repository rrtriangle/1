"""SEC EDGAR: 티커↔CIK 매핑, submissions.zip(공시 이력), companyfacts.zip(XBRL 재무 사실).

정책 준수
- 연락 가능한 User-Agent 필수 (MARKETLAB_SEC_USER_AGENT). 없으면 실행하지 않는다.
- 초당 10회 한도보다 낮게 호출. 대량 파일은 공식 bulk zip 만 사용.
- 대용량 zip 은 디스크 예산 확인 후 사용자 승인(--approve-large) 없이는 받지 않는다.

시점 처리
- financial_facts 의 known_from = filed(제출일) 다음 날. 공시 이전 값을 과거에 알았던 것처럼 쓰지 않도록 한다.
- CIK 는 기업 단위 ID 다. 증권(security_id)과 1:1 이 아니므로 매핑은 'ticker_match_current' 신뢰도로만 기록.
- 원본 XBRL 개념(taxonomy/concept), 단위, 기간, 제출일, 접수번호를 그대로 보존한다.
"""
from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa

from ..config import get_settings
from ..logging_setup import get_logger, log_event
from ..storage.catalog import catalog
from ..storage.diskguard import DiskGuard, StorageRisk
from ..storage.parquet_io import sha256_file, write_table_atomic
from ..net.http import HttpClient
from .base import Run, StopRequested, record_manifest, stop_requested

log = get_logger("sec")
SOURCE = "sec"

FACT_SCHEMA = pa.schema([
    ("cik", pa.string()), ("entity_name", pa.string()), ("taxonomy", pa.string()), ("concept", pa.string()),
    ("unit", pa.string()), ("period_start", pa.date32()), ("period_end", pa.date32()), ("value", pa.float64()),
    ("accn", pa.string()), ("fy", pa.int32()), ("fp", pa.string()), ("form", pa.string()),
    ("filed", pa.date32()), ("frame", pa.string()), ("known_from", pa.date32()),
])

FILING_SCHEMA = pa.schema([
    ("cik", pa.string()), ("accession_number", pa.string()), ("form", pa.string()),
    ("filing_date", pa.date32()), ("report_date", pa.date32()), ("acceptance_datetime", pa.string()),
    ("primary_document", pa.string()), ("is_xbrl", pa.bool_()),
])


class SecUserAgentMissing(RuntimeError):
    pass


def _client() -> HttpClient:
    ua = os.environ.get("MARKETLAB_SEC_USER_AGENT", "").strip()
    if not ua or "@" not in ua:
        raise SecUserAgentMissing("SEC 정책상 연락처가 포함된 User-Agent 가 필요합니다. "
                                  ".env 에 MARKETLAB_SEC_USER_AGENT='이름 email@example.com' 을 설정하세요.")
    return HttpClient(SOURCE, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})


def raw_dir() -> Path:
    return get_settings().paths.raw / "sec"


def bulk_sizes() -> dict[str, int | None]:
    cfg = get_settings().source(SOURCE)
    c = _client()
    out = {}
    for key in ("submissions_zip", "companyfacts_zip"):
        try:
            r = c.head(cfg[key])
            out[key] = int(r.headers["Content-Length"]) if r.status_code == 200 and r.headers.get("Content-Length") else None
        except Exception:
            out[key] = None
    return out


def collect_tickers() -> dict:
    """company_tickers_exchange.json → metadata 및 미국 증권의 cik 컬럼(현재 티커 일치 기준)."""
    s = get_settings()
    cfg = s.source(SOURCE)
    c = _client()
    run = Run(SOURCE, "US", "tickers").start()
    r = c.get(cfg["tickers_url"])
    if r.status_code != 200:
        run.finish("failed", f"HTTP {r.status_code}")
        raise RuntimeError(f"SEC tickers HTTP {r.status_code}")
    raw = raw_dir() / f"company_tickers_exchange_{datetime.now():%Y%m%d}.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(r.content)
    record_manifest(SOURCE, raw.name, cfg["tickers_url"], None, raw, raw.stat().st_size, sha256_file(raw),
                    None, None, "ok")
    js = r.json()
    df = pd.DataFrame(js["data"], columns=js["fields"])
    df["cik"] = df["cik"].astype(str).str.zfill(10)
    out = s.paths.metadata / "sec_company_tickers.parquet"
    df.to_parquet(out, index=False)
    with catalog() as cat:
        cat.register("t", df)
        cat.execute("""
            UPDATE securities SET cik = t.cik, updated_at = now()
            FROM t, symbol_history h
            WHERE securities.security_id = h.security_id AND h.symbol_type = 'ticker' AND h.valid_to IS NULL
              AND upper(replace(h.symbol, '.', '-')) = upper(replace(t.ticker, '.', '-'))
              AND securities.country = 'US' AND securities.status = 'listed'
        """)
        n = cat.execute("SELECT count(*) FROM securities WHERE country='US' AND cik IS NOT NULL").fetchone()[0]
    run.rows_written = len(df)
    run.finish("success", f"{len(df)} tickers, {n} securities mapped to CIK (ticker_match_current)")
    return {"tickers": len(df), "mapped": n}


def _download_bulk(key: str, approve_large: bool) -> Path:
    s = get_settings()
    cfg = s.source(SOURCE)
    url = cfg[key]
    c = _client()
    dest = raw_dir() / Path(url).name
    size = bulk_sizes().get(key)
    if size is None:
        raise RuntimeError(f"{url} 크기를 확인할 수 없음")
    # zip 보존 + 변환 결과(경험치: companyfacts ≈ zip 의 3배, submissions ≈ 0.5배) 를 예산에 반영
    factor = 3.0 if key == "companyfacts_zip" else 0.6
    planned = int(size * (1 + factor))
    if not approve_large:
        raise PermissionError(f"{Path(url).name} 는 약 {size / 1024 ** 3:.1f}GB(변환 포함 약 {planned / 1024 ** 3:.1f}GB). "
                              "disk-plan 확인 후 --approve-large 로 승인해야 다운로드합니다.")
    guard = DiskGuard(f"SEC {Path(url).name}", planned)
    guard.preflight()
    if dest.exists() and dest.stat().st_size == size:
        return dest
    n = c.download(url, dest, checkpoint=lambda b: guard.checkpoint(b))
    record_manifest(SOURCE, dest.name, url, None, dest, n, sha256_file(dest), None,
                    datetime.now(timezone.utc).date().isoformat(), "ok")
    return dest


def build_companyfacts(approve_large: bool = False, batch_rows: int = 1_500_000) -> dict:
    """companyfacts.zip → financial_facts Parquet (압축 해제 없이 엔트리 단위 스트리밍)."""
    s = get_settings()
    z = _download_bulk("companyfacts_zip", approve_large)
    out_dir = s.paths.standardized / "financial_facts" / "source=sec"
    run = Run(SOURCE, "US", "companyfacts").start()
    guard = DiskGuard("SEC companyfacts 변환", int(z.stat().st_size * 3))
    rows: dict[str, list] = {k: [] for k in FACT_SCHEMA.names}
    part, n_rows = 0, 0

    def flush():
        nonlocal part, rows, n_rows
        if not rows["cik"]:
            return
        tbl = pa.table({k: pa.array(v, type=FACT_SCHEMA.field(k).type) for k, v in rows.items()}, schema=FACT_SCHEMA)
        guard.checkpoint(run.bytes_written)
        run.bytes_written += write_table_atomic(tbl, out_dir / f"part-{part:05d}.parquet")
        n_rows += tbl.num_rows
        part += 1
        rows = {k: [] for k in FACT_SCHEMA.names}

    def d(x):
        return datetime.strptime(x, "%Y-%m-%d").date() if x else None

    try:
        with zipfile.ZipFile(z) as zf:
            names = [n for n in zf.namelist() if n.endswith(".json")]
            run.items_total = len(names)
            for i, name in enumerate(names):
                if stop_requested():
                    raise StopRequested("중지 요청")
                with zf.open(name) as fh:
                    js = json.load(fh)
                cik = str(js.get("cik", "")).zfill(10)
                ent = js.get("entityName")
                for tax, concepts in (js.get("facts") or {}).items():
                    for concept, body in concepts.items():
                        for unit, facts in (body.get("units") or {}).items():
                            for f in facts:
                                filed = d(f.get("filed"))
                                rows["cik"].append(cik); rows["entity_name"].append(ent)
                                rows["taxonomy"].append(tax); rows["concept"].append(concept)
                                rows["unit"].append(unit); rows["period_start"].append(d(f.get("start")))
                                rows["period_end"].append(d(f.get("end")))
                                v = f.get("val")
                                rows["value"].append(float(v) if isinstance(v, (int, float)) else None)
                                rows["accn"].append(f.get("accn")); rows["fy"].append(f.get("fy"))
                                rows["fp"].append(f.get("fp")); rows["form"].append(f.get("form"))
                                rows["filed"].append(filed); rows["frame"].append(f.get("frame"))
                                rows["known_from"].append(filed + pd.Timedelta(days=1) if filed else None)
                run.items_ok += 1
                if len(rows["cik"]) >= batch_rows:
                    flush()
                    run.rows_written = n_rows
                    run.message = f"{i + 1}/{len(names)}"
                    run.progress()
        flush()
        run.rows_written = n_rows
        run.finish("success")
    except (StopRequested, StorageRisk) as e:
        flush()
        run.finish("stopped", str(e))
        raise
    return {"companies": run.items_ok, "rows": n_rows, "bytes": run.bytes_written}


def build_submissions(approve_large: bool = False) -> dict:
    """submissions.zip → sec_filings Parquet (접수 시각 = 재무정보 공개 시점 근거)."""
    s = get_settings()
    z = _download_bulk("submissions_zip", approve_large)
    out_dir = s.paths.standardized / "sec_filings"
    run = Run(SOURCE, "US", "submissions").start()
    rows: dict[str, list] = {k: [] for k in FILING_SCHEMA.names}
    part, n = 0, 0

    def d(x):
        try:
            return datetime.strptime(x, "%Y-%m-%d").date() if x else None
        except ValueError:
            return None

    def flush():
        nonlocal part, rows, n
        if rows["cik"]:
            tbl = pa.table({k: pa.array(v, type=FILING_SCHEMA.field(k).type) for k, v in rows.items()}, schema=FILING_SCHEMA)
            run.bytes_written += write_table_atomic(tbl, out_dir / f"part-{part:05d}.parquet")
            n += tbl.num_rows
            part += 1
            rows = {k: [] for k in FILING_SCHEMA.names}

    with zipfile.ZipFile(z) as zf:
        for name in zf.namelist():
            if not name.endswith(".json"):
                continue
            with zf.open(name) as fh:
                js = json.load(fh)
            recent = (js.get("filings") or {}).get("recent") or js  # 보조 파일(CIK..-submissions-001.json)은 평면 구조
            cik = str(js.get("cik") or name[3:13]).zfill(10)
            accn = recent.get("accessionNumber") or []
            for j in range(len(accn)):
                rows["cik"].append(cik); rows["accession_number"].append(accn[j])
                rows["form"].append(recent["form"][j]); rows["filing_date"].append(d(recent["filingDate"][j]))
                rows["report_date"].append(d(recent.get("reportDate", [""] * len(accn))[j]))
                rows["acceptance_datetime"].append(recent.get("acceptanceDateTime", [""] * len(accn))[j])
                rows["primary_document"].append(recent.get("primaryDocument", [""] * len(accn))[j])
                rows["is_xbrl"].append(bool(recent.get("isXBRL", [0] * len(accn))[j]))
            if len(rows["cik"]) >= 2_000_000:
                flush()
    flush()
    run.rows_written = n
    run.finish("success")
    return {"rows": n, "bytes": run.bytes_written}
