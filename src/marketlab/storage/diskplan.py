"""초기 수집 전 저장공간 계획 (disk-plan).

가능한 경우 원격 파일 크기를 실제 조회하고, 조회할 수 없으면 소규모 실측치로 추정하며 오차를 명시한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import get_settings
from ..logging_setup import get_logger
from .diskguard import GB, assess, dir_size, write_storage_advice

log = get_logger("diskplan")


@dataclass
class Item:
    source: str
    download_gb: float
    stored_raw_gb: float
    standardized_gb: float
    peak_temp_gb: float
    basis: str
    error: str
    already_gb: float = 0.0


def _marcap() -> Item:
    from ..collectors import marcap
    try:
        p = marcap.plan()
        total = p["remote_total_bytes"] / GB
        todo = p["todo_bytes"] / GB
        # 실측: 표준화 결과 ≈ 원본의 0.75배 (zstd, 컬럼 추가 포함)
        basis = f"원격 파일 크기 실조회 ({p['method']}, {len(p['years'])}개 연도)"
        return Item("marcap (KR 일봉)", todo, total, total * 0.75, 0.05, basis, "±5%",
                    already_gb=(dir_size(marcap.raw_dir())
                                + dir_size(get_settings().paths.standardized / "daily_prices" / "country=KR")) / GB)
    except Exception as e:
        return Item("marcap (KR 일봉)", 0.6, 0.6, 0.45, 0.05, f"추정 (조회 실패: {e})", "±20%")


def _yfinance(n_symbols: int | None) -> Item:
    from ..collectors import yfinance_us
    bpr = yfinance_us.measured_bytes_per_row()
    measured = bpr != yfinance_us.DEFAULT_BYTES_PER_ROW
    n = n_symbols or 12000
    rows = n * yfinance_us.DEFAULT_ROWS_PER_SYMBOL
    raw = rows * bpr / GB
    return Item("yfinance (US 일봉)", raw * 1.6, raw, raw * 1.4, 0.2,
                f"{n:,}종목 × 평균 {yfinance_us.DEFAULT_ROWS_PER_SYMBOL}행 × {bpr:.1f}B/행 "
                f"({'실측' if measured else '사전 추정'})", "±40%" if not measured else "±20%",
                already_gb=(dir_size(yfinance_us.raw_dir())
                            + dir_size(get_settings().paths.standardized / "daily_prices" / "country=US")) / GB)


def _sec(include: bool) -> list[Item]:
    if not include:
        return []
    from ..collectors import sec_edgar
    try:
        sz = sec_edgar.bulk_sizes()
    except Exception as e:
        sz = {"companyfacts_zip": None, "submissions_zip": None}
        err = str(e)
    else:
        err = ""
    out = []
    cf = sz.get("companyfacts_zip")
    out.append(Item("SEC companyfacts.zip (재무)", (cf or 1.4 * GB) / GB, (cf or 1.4 * GB) / GB,
                    (cf or 1.4 * GB) * 3 / GB, 0.3, "Content-Length 실조회" if cf else f"추정 1.4GB {err}",
                    "±5% (zip) / ±50% (변환)"))
    sb = sz.get("submissions_zip")
    out.append(Item("SEC submissions.zip (공시이력)", (sb or 1.5 * GB) / GB, (sb or 1.5 * GB) / GB,
                    (sb or 1.5 * GB) * 0.6 / GB, 0.2, "Content-Length 실조회" if sb else f"추정 1.5GB {err}", "±50% (변환)"))
    return out


def plan(include_sec: bool = False, include_stooq_bulk_gb: float = 0.0) -> dict:
    s = get_settings()
    n_us = None
    try:
        from ..storage.catalog import catalog
        with catalog(read_only=True) as con:
            n_us = con.execute("SELECT count(*) FROM securities WHERE country='US'").fetchone()[0] or None
    except Exception:
        pass
    items = [_marcap(),
             Item("KRX 캐시 (목록·지수)", 0.01, 0.01, 0.01, 0.0, "파일 수 × 평균 크기", "±30%"),
             _yfinance(n_us),
             Item("지수·환율·금리 (FRED/Yahoo)", 0.02, 0.02, 0.01, 0.0, "시리즈 수 × 실측 크기", "±50%"),
             Item("메타데이터 DB·로그·리포트", 0.0, 0.0, 0.3, 0.0, "증권 마스터·품질이슈 실측 기반", "±50%")]
    items += _sec(include_sec)
    if include_stooq_bulk_gb:
        items.append(Item("Stooq 대량 파일 (사용자 제공)", include_stooq_bulk_gb, include_stooq_bulk_gb,
                          include_stooq_bulk_gb * 1.2, 0.0, "사용자 입력 zip 크기", "±50%"))
    duckdb_tmp = float(str(s.get("duckdb.max_temp_directory_size", "4GB")).rstrip("GB"))
    total_new = sum(max(0.0, i.stored_raw_gb + i.standardized_gb - i.already_gb) for i in items)
    peak_temp = max([i.peak_temp_gb for i in items] + [0]) + duckdb_tmp
    a = assess(int(total_new * GB), int(peak_temp * GB))
    result = {"items": [i.__dict__ for i in items], "total_new_gb": total_new, "peak_temp_gb": peak_temp,
              "assessment": a.as_dict()}
    _write_report(result, a)
    if a.risk:
        write_storage_advice(a, "disk-plan")
    return result


def _write_report(r: dict, a) -> Path:
    s = get_settings()
    L = ["# 저장공간 계획 (disk-plan)", "", f"생성: {datetime.now().isoformat(timespec='seconds')}", "",
         f"- 데이터 경로: `{a.data_dir}` ({'외장' if a.external else '내장'})",
         f"- 디스크 전체 {a.disk_total_gb:.1f}GB, 현재 여유 {a.disk_free_gb:.1f}GB",
         f"- 현재 프로젝트 사용량 {a.project_used_gb:.2f}GB, 유효 예산 {a.budget_gb:.1f}GB (최소 여유 {a.min_free_gb:.0f}GB 유지)", "",
         "| 원천 | 다운로드 | 원본 저장 | 표준화 | 최대 임시 | 이미 보유 | 근거 | 오차 |", "|---|---:|---:|---:|---:|---:|---|---|"]
    for i in r["items"]:
        L.append(f"| {i['source']} | {i['download_gb']:.2f}GB | {i['stored_raw_gb']:.2f}GB | {i['standardized_gb']:.2f}GB | "
                 f"{i['peak_temp_gb']:.2f}GB | {i['already_gb']:.2f}GB | {i['basis']} | {i['error']} |")
    L += ["", f"- 추가로 필요한 저장공간(예상): **{r['total_new_gb']:.2f}GB**",
          f"- 처리 중 최대 임시공간(DuckDB 임시 한도 포함): {r['peak_temp_gb']:.2f}GB",
          f"- 수집 완료 후 예상 여유: {a.free_after_gb:.1f}GB / 프로젝트 예상 총량 {a.project_after_gb:.2f}GB", "",
          "## 판정", ""]
    if a.risk:
        L += [f"- 위험: {x}" for x in a.reasons] + [f"- 외장 SSD 권장: {a.recommendation} (reports/storage_advice.md 참고)"]
    else:
        L += ["- 예산과 최소 여유 공간 조건을 만족합니다. 외장 SSD 가 필요하지 않습니다."]
    p = s.paths.reports / "disk_plan.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return p
