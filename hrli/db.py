"""SQLite 저장소. 원본 보존 영역(업로드 배치·스테이징)과 분석 영역(직원·이력·기준정보)을 분리한다.

SQLite를 쓰는 이유: 트랜잭션과 변경 이력 관리가 가능하고 별도 서버 없이 배포할 수 있다.
테이블 구조는 표준 SQL이므로 사내 DB(PostgreSQL 등)로 옮길 때 이 모듈만 바꾸면 된다.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config

# 직원 명부에서 추적하는 속성(표준 항목). 원본에 없는 컬럼은 NULL로 남긴다.
EMP_ATTRS = [
    "name", "status", "hire_date", "division", "grp", "team", "part",
    "job_family", "position", "title", "shift",
]
# 교육 이력의 표준 항목
REC_ATTRS = [
    "source_id", "emp_no", "course_name_raw", "start_date", "end_date",
    "hours_raw", "institution_raw", "type_raw", "completion",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,   -- 로그인 이메일(소문자)
    name TEXT,
    role TEXT NOT NULL,
    org_scope TEXT,              -- 조직 관리자: 조회 가능한 그룹(grp)
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS otp (
    username TEXT PRIMARY KEY,   -- 이메일당 유효한 인증 코드는 하나
    code_hash TEXT NOT NULL,     -- 코드는 저장하지 않고 해시만 보관
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS column_mapping (
    kind TEXT NOT NULL,          -- roster / training
    std_col TEXT NOT NULL,       -- 시스템 표준 항목
    source_col TEXT NOT NULL,    -- 원본 엑셀 컬럼명(여러 별칭 허용)
    PRIMARY KEY (kind, std_col, source_col)
);
CREATE TABLE IF NOT EXISTS upload_batch (
    batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,          -- roster / training
    file_type TEXT NOT NULL,     -- full / incremental / reprocess
    filename TEXT NOT NULL,
    stored_path TEXT,
    snapshot_month TEXT,         -- 명부 기준월(YYYY-MM)
    uploaded_by TEXT,
    uploaded_at TEXT,
    status TEXT NOT NULL,        -- staged / failed / approved / rejected / rolled_back
    decided_by TEXT,
    decided_at TEXT,
    summary TEXT,                -- 분류별 건수(JSON)
    notes TEXT
);
CREATE TABLE IF NOT EXISTS staging_row (
    batch_id INTEGER NOT NULL,
    row_no INTEGER NOT NULL,
    payload TEXT NOT NULL,       -- 표준 항목으로 변환한 원본 행(JSON)
    category TEXT NOT NULL,      -- new / changed / duplicate / suspect / unmatched / error / same / missing
    target_id TEXT,              -- 비교 대상 기존 키
    messages TEXT,
    decision TEXT NOT NULL,      -- accept / update / skip / pending
    PRIMARY KEY (batch_id, row_no)
);
CREATE TABLE IF NOT EXISTS employee (
    emp_no TEXT PRIMARY KEY,
    name TEXT, status TEXT, hire_date TEXT,
    division TEXT, grp TEXT, team TEXT, part TEXT,
    job_family TEXT, position TEXT, title TEXT, shift TEXT,
    in_scope INTEGER NOT NULL DEFAULT 1,      -- 분석 대상 여부
    roster_state TEXT NOT NULL DEFAULT '명부확인',  -- 명부확인 / 명부미포함(확인 필요, 퇴직 아님)
    last_seen_month TEXT
);
CREATE TABLE IF NOT EXISTS employee_snapshot (
    snapshot_month TEXT NOT NULL,
    emp_no TEXT NOT NULL,
    name TEXT, status TEXT, hire_date TEXT,
    division TEXT, grp TEXT, team TEXT, part TEXT,
    job_family TEXT, position TEXT, title TEXT, shift TEXT,
    batch_id INTEGER,
    PRIMARY KEY (snapshot_month, emp_no)
);
CREATE TABLE IF NOT EXISTS training_record (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT,              -- 원천 시스템 고유 ID(있을 때만)
    emp_no TEXT NOT NULL,
    course_name_raw TEXT NOT NULL,
    start_date TEXT, end_date TEXT,
    hours_raw REAL,              -- 원본 교육 시간(보존)
    hours_adj REAL,              -- 담당자 보정 시간(NULL이면 원본 사용)
    hours_included INTEGER NOT NULL DEFAULT 1,  -- 시간 통계 포함 여부
    institution_raw TEXT,
    type_raw TEXT,               -- 원본의 사내/사외 등 구분값
    completion TEXT,
    excluded INTEGER NOT NULL DEFAULT 0,        -- 통계 제외(삭제 대신)
    exclude_reason TEXT,
    batch_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_rec_emp ON training_record(emp_no);
CREATE INDEX IF NOT EXISTS ix_rec_src ON training_record(source_id);
CREATE TABLE IF NOT EXISTS course_master (
    course_name_raw TEXT PRIMARY KEY,  -- 원본 교육명은 그대로 보존
    std_name TEXT NOT NULL,            -- 표준 교육명
    field TEXT NOT NULL DEFAULT '미분류',      -- 교육 분야
    operation TEXT NOT NULL DEFAULT '미분류',  -- 운영 구분(사내/사외)
    method TEXT NOT NULL DEFAULT '미분류',     -- 교육 방식
    target TEXT,                       -- 교육 대상(전사/직종/직책/특정 조직)
    confirmed INTEGER NOT NULL DEFAULT 0,      -- 담당자 확정 여부
    rule_note TEXT                     -- 자동 분류 근거
);
CREATE TABLE IF NOT EXISTS institution_master (
    institution_raw TEXT PRIMARY KEY,
    institution_std TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS time_rule (
    rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,         -- field / institution / course
    value TEXT NOT NULL,
    reason TEXT, created_by TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS change_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, username TEXT,
    op TEXT NOT NULL,            -- insert / update / delete
    tbl TEXT NOT NULL, row_key TEXT NOT NULL,
    col TEXT, old_value TEXT, new_value TEXT,
    reason TEXT, batch_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_log_batch ON change_log(batch_id);
CREATE TABLE IF NOT EXISTS access_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, username TEXT, action TEXT NOT NULL, detail TEXT
);
"""

# 원본 엑셀 컬럼 → 표준 항목 기본 매핑. 실제 파일의 컬럼이 확정되면 column_mapping 테이블에서 수정한다.
DEFAULT_MAPPING = {
    "roster": {
        "emp_no": ["사번", "사원번호"], "name": ["성명", "이름"], "status": ["재직상태", "재직구분"],
        "hire_date": ["입사일", "입사일자"], "division": ["본부", "사업본부", "센터"],
        "grp": ["그룹", "공장"], "team": ["팀"], "part": ["파트"], "job_family": ["직종"],
        "position": ["직위"], "title": ["직책"], "shift": ["근무조"],
    },
    "training": {
        "source_id": ["교육이력ID", "이력ID"], "emp_no": ["사번", "사원번호"],
        "course_name_raw": ["교육명", "과정명"], "start_date": ["시작일", "교육시작일"],
        "end_date": ["종료일", "교육종료일"], "hours_raw": ["교육시간", "시간"],
        "institution_raw": ["교육기관", "기관"], "type_raw": ["교육유형", "교육구분"],
        "completion": ["수료여부", "수료"],
    },
}
REQUIRED = {"roster": ["emp_no"], "training": ["emp_no", "course_name_raw", "start_date"]}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)  # 트랜잭션은 tx()로 명시 관리
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(path: Path | str | None = None) -> None:
    con = connect(path)
    try:
        con.executescript(SCHEMA)
        if not con.execute("SELECT 1 FROM column_mapping LIMIT 1").fetchone():
            rows = [(k, std, src) for k, m in DEFAULT_MAPPING.items() for std, srcs in m.items() for src in srcs]
            con.executemany("INSERT INTO column_mapping VALUES (?,?,?)", rows)
    finally:
        con.close()


@contextmanager
def tx(path: Path | str | None = None):
    """배치 단위 트랜잭션. 오류가 나면 전부 되돌려 일부만 반영된 상태를 남기지 않는다."""
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def read(sql: str, params: tuple | list = (), path: Path | str | None = None) -> pd.DataFrame:
    con = connect(path)
    try:
        return pd.read_sql_query(sql, con, params=list(params))
    finally:
        con.close()


def log_change(con, user, op, tbl, key, col=None, old=None, new=None, reason=None, batch_id=None) -> None:
    con.execute(
        "INSERT INTO change_log (ts, username, op, tbl, row_key, col, old_value, new_value, reason, batch_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now(), user, op, tbl, str(key), col, None if old is None else str(old),
         None if new is None else str(new), reason, batch_id),
    )


def log_access(user: str | None, action: str, detail: str = "", path=None) -> None:
    con = connect(path)
    try:
        con.execute("INSERT INTO access_log (ts, username, action, detail) VALUES (?,?,?,?)",
                    (now(), user, action, detail))
    finally:
        con.close()


PK = {"employee": "emp_no", "training_record": "record_id", "course_master": "course_name_raw",
      "institution_master": "institution_raw"}


def update_field(tbl: str, key, col: str, new, user: str, reason: str | None = None, path=None) -> None:
    """담당자 개별 수정. 수정 전 값을 change_log에 남긴다."""
    if tbl not in PK:
        raise ValueError(f"수정할 수 없는 테이블: {tbl}")
    with tx(path) as con:
        cols = [r["name"] for r in con.execute(f"PRAGMA table_info({tbl})")]
        if col not in cols or col == PK[tbl]:
            raise ValueError(f"수정할 수 없는 항목: {col}")
        row = con.execute(f"SELECT {col} FROM {tbl} WHERE {PK[tbl]}=?", (key,)).fetchone()
        if row is None:
            raise KeyError(f"{tbl} {key} 없음")
        if row[0] == new:
            return
        con.execute(f"UPDATE {tbl} SET {col}=? WHERE {PK[tbl]}=?", (new, key))
        log_change(con, user, "update", tbl, key, col, row[0], new, reason)
