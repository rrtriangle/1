"""Dataiku Flow 연결: 업로드 데이터셋 → 검토 대기 배치, 승인된 데이터 → 분석용 데이터셋.

레시피에서는 이 모듈의 함수만 호출한다(dataiku/recipe_stage.py, dataiku/recipe_export.py).
dataiku 패키지에 의존하지 않으므로 로컬에서도 시험할 수 있다.
"""
from __future__ import annotations

import hashlib
import re

import pandas as pd

from . import db, ingest, metrics

MONTH_COLS = ("기준월", "명부기준월", "snapshot_month")
# dataiku.Dataset.get_dataframe 옵션. 기본값(infer_with_pandas=True)은 pandas가 형식을 다시 추정해서,
# 스키마에서 문자열로 둔 사번도 숫자로 바뀌어 앞자리 0이 사라질 수 있다. 스키마 형식을 그대로 쓰게 한다.
READ_OPTS = {"infer_with_pandas": False, "use_nullable_integers": True}


def _month(v) -> str | None:
    """'2026-01', '202601', '2026.1', 날짜 등을 'YYYY-MM'으로 바꾼다."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (pd.Timestamp,)) or hasattr(v, "year"):
        return f"{v.year:04d}-{v.month:02d}"
    m = re.match(r"^\s*(\d{4})\D?(\d{1,2})", str(v))
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}"


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    """데이터셋을 업로드 파일과 같은 형태(CSV)로 바꾼다. 날짜는 날짜만 남긴다(시간대 때문에 하루 밀리지 않게)."""
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[c]):
            col = d[c]
            if getattr(col.dt, "tz", None) is not None:
                col = col.dt.tz_convert(None)
            d[c] = col.dt.strftime("%Y-%m-%d")
    return d.to_csv(index=False).encode("utf-8")


def stage_dataframe(df: pd.DataFrame, source: str, kind: str, file_type: str, user: str,
                    month: str | None = None, path=None) -> list[dict]:
    """데이터셋 하나를 검토 대기 배치로 올린다. 같은 내용을 이미 올렸으면 다시 올리지 않는다.

    직원 명부에 기준월 컬럼이 있으면 기준월별로 나눠 오래된 달부터 따로 올린다(과거 명부 일괄 적재용).
    """
    parts: list[tuple[str | None, pd.DataFrame]]
    mcol = next((c for c in MONTH_COLS if c in df.columns), None)
    if kind == "roster" and mcol:
        months = df[mcol].map(_month)
        if months.isna().any():
            bad = df.loc[months.isna(), mcol].astype(str).unique()[:3]
            raise ValueError(f"기준월 값을 해석할 수 없습니다: {list(bad)}")
        parts = [(m, df[months == m].drop(columns=mcol)) for m in sorted(months.unique())]
    else:
        parts = [(_month(month) if month else None, df)]
    out = []
    for m, part in parts:
        content = to_csv_bytes(part)
        digest = hashlib.sha256(content).hexdigest()[:12]
        filename = f"dataset_{source}{'_' + m if m else ''}_{digest}.csv"
        prev = db.read("SELECT batch_id, status FROM upload_batch WHERE filename=? AND status IN ('staged','approved')"
                       " ORDER BY batch_id DESC LIMIT 1", (filename,), path)
        if not prev.empty:
            out.append({"source": source, "kind": kind, "month": m, "rows": len(part), "batch_id": int(prev.iat[0, 0]),
                        "status": "skipped", "note": f"같은 내용이 이미 배치 #{int(prev.iat[0, 0])}({prev.iat[0, 1]})로 올라가 있음"})
            continue
        bid = ingest.stage(content, filename, kind, file_type, user, m if kind == "roster" else None, path)
        b = ingest.batches(path).set_index("batch_id").loc[bid]
        out.append({"source": source, "kind": kind, "month": m, "rows": len(part), "batch_id": bid,
                    "status": b["status"], "note": b["notes"] if pd.notna(b["notes"]) else (b["summary"] or "")})
    return out


# ---------------------------------------------------------------- 내보내기

EMP_KO = {"emp_no": "사번", "name": "성명", "status": "재직상태", "hire_date": "입사일", "division": "본부", "grp": "그룹",
          "team": "팀", "part": "파트", "job_family": "직종", "position": "직위", "title": "직책", "shift": "근무조"}


def export_tables(path=None) -> dict[str, pd.DataFrame]:
    """승인되어 반영된 데이터를 분석용 표로 만든다. 열 이름은 한글이다."""
    emp = db.read("SELECT * FROM employee", path=path).rename(columns=EMP_KO | {
        "in_scope": "분석대상", "roster_state": "명부상태", "last_seen_month": "마지막확인월"})
    snap = db.read("SELECT * FROM employee_snapshot", path=path).rename(
        columns=EMP_KO | {"snapshot_month": "기준월", "batch_id": "배치"})
    fact = metrics.load_fact(path)
    fact = fact.drop(columns=["date", "hours_counted"]).rename(columns=EMP_KO | {
        "record_id": "이력ID", "course_name_raw": "원본교육명", "std_name": "표준교육명", "start_date": "시작일",
        "end_date": "종료일", "year": "연도", "hours_raw": "원본시간", "hours_adj": "보정시간", "hours": "분석용시간",
        "hours_included": "시간통계포함", "institution_raw": "원본교육기관", "institution": "교육기관",
        "operation": "운영구분", "method": "교육방식", "field": "교육분야", "completion": "수료여부",
        "tenure_band": "근속구간"})
    raw = db.read(
        "SELECT r.record_id AS 이력ID, r.source_id AS 원천이력ID, r.emp_no AS 사번, r.course_name_raw AS 원본교육명,"
        " r.start_date AS 시작일, r.end_date AS 종료일, r.hours_raw AS 원본시간, r.hours_adj AS 보정시간,"
        " r.hours_included AS 시간통계포함, r.institution_raw AS 원본교육기관, r.type_raw AS 원본유형,"
        " r.completion AS 수료여부, r.excluded AS 통계제외, r.exclude_reason AS 제외사유, r.batch_id AS 배치,"
        " CASE WHEN e.emp_no IS NULL THEN '미매칭' WHEN e.in_scope=0 THEN '분석대상외' WHEN r.excluded=1 THEN '통계제외'"
        " ELSE '반영' END AS 통계반영"
        " FROM training_record r LEFT JOIN employee e ON e.emp_no=r.emp_no", path=path)
    course = db.read("SELECT course_name_raw AS 원본교육명, std_name AS 표준교육명, field AS 교육분야, operation AS 운영구분,"
                     " method AS 교육방식, target AS 교육대상, confirmed AS 확정, rule_note AS 자동분류근거"
                     " FROM course_master", path=path)
    batches = db.read("SELECT batch_id AS 배치, kind AS 종류, file_type AS 유형, filename AS 파일명, snapshot_month AS 기준월,"
                      " uploaded_by AS 업로드, uploaded_at AS 업로드일시, status AS 상태, decided_by AS 처리자,"
                      " decided_at AS 처리일시, summary AS 건수, notes AS 비고 FROM upload_batch", path=path)
    changes = db.read("SELECT ts AS 일시, username AS 사용자, op AS 작업, tbl AS 대상, row_key AS 키, col AS 항목,"
                      " old_value AS 이전값, new_value AS 새값, reason AS 사유, batch_id AS 배치 FROM change_log", path=path)
    return {"hrli_employee": emp, "hrli_snapshot": snap, "hrli_training": fact, "hrli_training_all": raw,
            "hrli_course": course, "hrli_batches": batches, "hrli_change_log": changes}
