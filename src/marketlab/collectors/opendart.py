"""OpenDART (금융감독원 전자공시) — 한국 기업 재무정보 공식 원천.

API 키(DART_API_KEY)가 필요하다. 키가 없으면 수집을 건너뛰고 보고만 하며, 주가 수집은 계속된다.
현재 구현: corpCode.xml (고유번호 ↔ 종목코드 ↔ 회사명 ↔ 최종변경일) → metadata.
재무제표(fnlttSinglAcntAll 등)는 기업·연도·보고서별 호출이 필요하고 일일 호출 한도(약 20,000회)가 있어
별도 승인 후 단계적으로 확장한다 (docs/data_source_matrix.md 참고).
"""
from __future__ import annotations

import io
import os
import xml.etree.ElementTree as ET
import zipfile

import pandas as pd

from ..config import get_settings
from ..net.http import HttpClient
from .base import Run, record_source_check


def collect_corp_codes() -> dict:
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        record_source_check("opendart", False, None, "DART_API_KEY 없음 - 건너뜀")
        return {"status": "skipped", "reason": "DART_API_KEY 미설정"}
    s = get_settings()
    run = Run("opendart", "KR", "corp_codes").start()
    r = HttpClient("opendart", min_interval_sec=1.0).get(s.source("opendart")["corpcode_url"], params={"crtfc_key": key})
    if r.status_code != 200 or not r.content.startswith(b"PK"):
        run.finish("failed", f"HTTP {r.status_code}: {r.text[:200]}")
        return {"status": "failed", "detail": r.text[:200]}
    raw = s.paths.raw / "opendart" / "corpCode.zip"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(r.content)
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        xml = zf.read(zf.namelist()[0])
    rows = [{c.tag: (c.text or "").strip() for c in el} for el in ET.fromstring(xml).iter("list")]
    df = pd.DataFrame(rows)
    df.to_parquet(s.paths.metadata / "dart_corp_codes.parquet", index=False)
    run.rows_written = len(df)
    run.finish("success")
    return {"status": "ok", "companies": len(df), "listed": int((df.get("stock_code", "") != "").sum())}
