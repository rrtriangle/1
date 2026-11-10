"""월별 업로드 처리: 원본 보관 → 검증 → 임시 적재 → 기존 데이터와 비교 → 검토·승인 → 반영 → 이력 기록.

원칙
- 원본 파일은 반영 여부와 관계없이 보관한다.
- 파일에서 사라진 직원·이력을 자동 삭제하거나 퇴직 처리하지 않는다.
- 승인과 복구는 배치 단위 트랜잭션으로 처리한다.
"""
from __future__ import annotations

import io
import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import classify, config, db

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
EMP_RE = re.compile(r"^[A-Za-z0-9\-]{1,20}$")


class FatalUploadError(Exception):
    """파일 전체를 처리할 수 없는 치명적 오류(형식·필수 컬럼 누락 등)."""


# ---------------------------------------------------------------- 파싱·정규화

def _mapping(con, kind: str) -> dict[str, list[str]]:
    m: dict[str, list[str]] = {}
    for r in con.execute("SELECT std_col, source_col FROM column_mapping WHERE kind=?", (kind,)):
        m.setdefault(r["std_col"], []).append(r["source_col"])
    return m


def _clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in ("nan", "nat", "none") else None


def _emp_no(v):
    s = _clean(v)
    if s and re.fullmatch(r"\d+\.0", s):  # 엑셀이 숫자로 읽은 사번
        s = s[:-2]
    return s


def _date(v):
    """ISO 날짜 문자열 또는 None. 해석할 수 없으면 ValueError."""
    if _clean(v) is None:
        return None
    ts = pd.to_datetime(v, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"날짜 형식 오류: {v}")
    return ts.date().isoformat()


def _hours(v):
    if _clean(v) is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"교육 시간 형식 오류: {v}") from None


def parse_file(con, content: bytes, filename: str, kind: str) -> tuple[pd.DataFrame, list[str]]:
    """원본을 읽어 표준 항목 컬럼으로 바꾼다. 원본에 없는 표준 항목은 만들지 않는다."""
    try:
        if filename.lower().endswith(".csv"):
            raw = pd.read_csv(io.BytesIO(content), dtype=object)
        else:
            raw = pd.read_excel(io.BytesIO(content), dtype=object)
    except Exception as e:  # noqa: BLE001 - 어떤 파싱 오류든 치명적 오류로 보고
        raise FatalUploadError(f"파일을 읽을 수 없습니다: {e}") from e
    raw.columns = [str(c).strip() for c in raw.columns]
    rename, present = {}, []
    for std, sources in _mapping(con, kind).items():
        hit = next((s for s in sources if s in raw.columns), None)
        if hit:
            rename[hit] = std
            present.append(std)
    missing = [c for c in db.REQUIRED[kind] if c not in present]
    if missing:
        raise FatalUploadError(f"필수 항목에 해당하는 컬럼이 없습니다: {missing} (원본 컬럼: {list(raw.columns)})")
    if raw.empty:
        raise FatalUploadError("데이터 행이 없습니다.")
    return raw[list(rename)].rename(columns=rename), present


# ---------------------------------------------------------------- 비교(스테이징)

def _stage_roster(con, df: pd.DataFrame, present: list[str], file_type: str, month: str) -> list[dict]:
    existing = {r["emp_no"]: dict(r) for r in con.execute("SELECT * FROM employee")}
    attrs = [a for a in db.EMP_ATTRS if a in present]
    rows, seen = [], set()
    for rec in df.to_dict("records"):
        msgs, cat = [], None
        p = {"emp_no": _emp_no(rec.get("emp_no"))}
        for a in attrs:
            try:
                p[a] = _date(rec.get(a)) if a == "hire_date" else _clean(rec.get(a))
            except ValueError as e:
                p[a] = None
                msgs.append(str(e))
        if not p["emp_no"]:
            cat, msgs = "error", msgs + ["사번 누락"]
        elif not EMP_RE.match(p["emp_no"]):
            cat, msgs = "error", msgs + ["사번 형식 오류"]
        elif p["emp_no"] in seen:
            cat, msgs = "error", msgs + ["파일 내 사번 중복"]
        else:
            seen.add(p["emp_no"])
            if "grp" in attrs and not p.get("grp") and "team" in attrs and not p.get("team"):
                msgs.append("조직 정보 누락")
            old = existing.get(p["emp_no"])
            if old is None:
                cat = "new"
            else:
                diffs = [f"{a}: {old[a]} → {p[a]}" for a in attrs if old[a] != p[a]]
                cat = "changed" if diffs else "same"
                msgs += diffs
        rows.append({"payload": p, "category": cat, "target_id": p["emp_no"], "messages": msgs,
                     "decision": "skip" if cat == "error" else "accept"})
    latest = con.execute("SELECT MAX(snapshot_month) FROM employee_snapshot").fetchone()[0]
    if file_type != "incremental" and (latest is None or month >= latest):
        # 전체 명부에서 사라진 사번: 퇴직 처리하지 않고 '명부미포함' 확인 대상으로만 분류한다.
        for emp_no, old in existing.items():
            if emp_no not in seen and old["roster_state"] == "명부확인":
                rows.append({"payload": {"emp_no": emp_no, "name": old["name"]}, "category": "missing",
                             "target_id": emp_no, "messages": ["이번 명부에 없음(퇴직 아님, 확인 필요)"],
                             "decision": "accept"})
    return rows


def _nkey(p: dict) -> tuple:
    return (p["emp_no"], p["course_name_raw"], p.get("start_date"), p.get("end_date"),
            p.get("institution_raw"), p.get("hours_raw"))


def _stage_training(con, df: pd.DataFrame, present: list[str]) -> list[dict]:
    attrs = [a for a in db.REC_ATTRS if a in present]
    known = {r[0] for r in con.execute("SELECT emp_no FROM employee")}
    existing = [dict(r) for r in con.execute("SELECT * FROM training_record")]
    by_src = {r["source_id"]: r for r in existing if r["source_id"]}
    by_key = {_nkey(r): r for r in existing}
    by_loose: dict[tuple, dict] = {}
    for r in existing:
        by_loose.setdefault((r["emp_no"], r["course_name_raw"], r["start_date"]), r)
    rows, seen_keys, seen_src = [], set(), set()
    for rec in df.to_dict("records"):
        msgs, errors = [], []
        p = {a: None for a in db.REC_ATTRS if a in attrs}
        for a in attrs:
            try:
                v = rec.get(a)
                p[a] = (_emp_no(v) if a == "emp_no" else _date(v) if a in ("start_date", "end_date")
                        else _hours(v) if a == "hours_raw" else _clean(v))
            except ValueError as e:
                errors.append(str(e))
        if not p.get("emp_no"):
            errors.append("사번 누락")
        elif not EMP_RE.match(p["emp_no"]):
            errors.append("사번 형식 오류")
        if not p.get("course_name_raw"):
            errors.append("교육명 누락")
        if not p.get("start_date") and not any("날짜" in e for e in errors):
            errors.append("교육 시작일 누락")
        if p.get("start_date") and p.get("end_date") and p["end_date"] < p["start_date"]:
            errors.append("종료일이 시작일보다 빠름")
        h = p.get("hours_raw")
        if h is not None and h < 0:
            errors.append("교육 시간이 음수")
        if errors:
            rows.append({"payload": p, "category": "error", "target_id": None, "messages": errors, "decision": "skip"})
            continue
        if h is None and "hours_raw" in attrs:
            msgs.append("교육 시간 누락")
        elif h is not None and (h == 0 or h > config.MAX_REASONABLE_HOURS):
            msgs.append(f"비정상 교육 시간({h})")
        if "institution_raw" in attrs and not p.get("institution_raw"):
            msgs.append("교육기관 누락")

        key, src = _nkey(p), p.get("source_id")
        cat, target, decision = "new", None, "accept"
        if src and src in by_src:  # 1순위: 원천 시스템 고유 ID
            old = by_src[src]
            diffs = [f"{a}: {old[a]} → {p[a]}" for a in attrs if old[a] != p[a]]
            cat, target = ("changed", old["record_id"]) if diffs else ("duplicate", old["record_id"])
            decision = "update" if diffs else "skip"
            msgs += diffs
        elif (src and src in seen_src) or (not src and key in seen_keys):
            cat, decision = "duplicate", "skip"
            msgs.append("파일 내 동일 이력 중복")
        elif not src and key in by_key:  # 2순위: 항목 조합이 완전히 동일
            cat, target, decision = "duplicate", by_key[key]["record_id"], "skip"
        elif not src and (p["emp_no"], p["course_name_raw"], p["start_date"]) in by_loose:
            # 사번·교육명·시작일은 같고 일부 항목만 다름 → 수정인지 별개 이력인지 담당자가 판단
            old = by_loose[(p["emp_no"], p["course_name_raw"], p["start_date"])]
            cat, target, decision = "suspect", old["record_id"], "pending"
            msgs += [f"{a}: {old[a]} → {p[a]}" for a in attrs if old[a] != p[a]]
        elif p["emp_no"] not in known:
            cat = "unmatched"  # 명부에 없는 사번: 보관하되 명부와 매칭되기 전에는 통계에 넣지 않는다
            msgs.append("직원 명부에 없는 사번")
        seen_keys.add(key)
        if src:
            seen_src.add(src)
        rows.append({"payload": p, "category": cat, "target_id": target, "messages": msgs, "decision": decision})
    return rows


def stage(content: bytes, filename: str, kind: str, file_type: str, user: str,
          snapshot_month: str | None = None, path=None) -> int:
    """파일을 보관·검증하고 기존 데이터와 비교해 검토 대기 배치를 만든다. 분석 데이터는 바꾸지 않는다."""
    if kind not in ("roster", "training"):
        raise ValueError("kind는 roster 또는 training")
    if file_type not in ("full", "incremental", "reprocess"):
        raise ValueError("file_type은 full / incremental / reprocess")
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-가-힣]", "_", Path(filename).name)
    stored = config.RAW_DIR / f"{datetime.now():%Y%m%d_%H%M%S_%f}_{safe}"
    stored.write_bytes(content)  # 원본은 무조건 보관

    with db.tx(path) as con:
        cur = con.execute(
            "INSERT INTO upload_batch (kind, file_type, filename, stored_path, snapshot_month, uploaded_by,"
            " uploaded_at, status) VALUES (?,?,?,?,?,?,?, 'staged')",
            (kind, file_type, filename, str(stored), snapshot_month, user, db.now()))
        batch_id = cur.lastrowid
        try:
            if kind == "roster" and not (snapshot_month and MONTH_RE.match(snapshot_month)):
                raise FatalUploadError("직원 명부는 기준월(YYYY-MM)이 필요합니다.")
            df, present = parse_file(con, content, filename, kind)
            rows = (_stage_roster(con, df, present, file_type, snapshot_month) if kind == "roster"
                    else _stage_training(con, df, present))
        except FatalUploadError as e:
            con.execute("UPDATE upload_batch SET status='failed', notes=? WHERE batch_id=?", (str(e), batch_id))
            return batch_id
        con.executemany(
            "INSERT INTO staging_row VALUES (?,?,?,?,?,?,?)",
            [(batch_id, i, json.dumps(r["payload"], ensure_ascii=False), r["category"],
              None if r["target_id"] is None else str(r["target_id"]), "; ".join(r["messages"]), r["decision"])
             for i, r in enumerate(rows, 1)])
        summary = pd.Series([r["category"] for r in rows]).value_counts().to_dict()
        summary["total"] = len(df)
        summary["warnings"] = sum(1 for r in rows if r["messages"] and r["category"] in ("new", "unmatched"))
        summary["present_cols"] = present
        con.execute("UPDATE upload_batch SET summary=? WHERE batch_id=?",
                    (json.dumps(summary, ensure_ascii=False), batch_id))
    return batch_id


# ---------------------------------------------------------------- 검토·승인·복구

def staging(batch_id: int, path=None) -> pd.DataFrame:
    return db.read("SELECT row_no, category, decision, target_id, messages, payload FROM staging_row"
                   " WHERE batch_id=? ORDER BY row_no", (batch_id,), path)


def set_decision(batch_id: int, row_nos: list[int], decision: str, path=None) -> None:
    """중복 의심 건 등에 대한 담당자 판단. accept=신규로 추가, update=기존 이력 수정, skip=반영 안 함."""
    if decision not in ("accept", "update", "skip"):
        raise ValueError(decision)
    with db.tx(path) as con:
        _require_status(con, batch_id, "staged")
        for n in row_nos:
            r = con.execute("SELECT category, target_id FROM staging_row WHERE batch_id=? AND row_no=?",
                            (batch_id, n)).fetchone()
            if r is None or r["category"] == "error" and decision != "skip":
                raise ValueError(f"행 {n}: 오류 행은 반영할 수 없습니다.")
            if decision == "update" and not r["target_id"]:
                raise ValueError(f"행 {n}: 수정 대상 기존 이력이 없습니다.")
            con.execute("UPDATE staging_row SET decision=? WHERE batch_id=? AND row_no=?", (decision, batch_id, n))


def _require_status(con, batch_id: int, status: str):
    b = con.execute("SELECT * FROM upload_batch WHERE batch_id=?", (batch_id,)).fetchone()
    if b is None:
        raise KeyError(f"배치 {batch_id} 없음")
    if b["status"] != status:
        raise ValueError(f"배치 {batch_id} 상태가 '{b['status']}'이라 처리할 수 없습니다(필요: {status}).")
    return b


def ensure_masters(con, name: str, institution, type_raw) -> None:
    """새 원본 교육명·교육기관에 대한 기준정보 초안을 만든다(미확정 상태)."""
    if not con.execute("SELECT 1 FROM course_master WHERE course_name_raw=?", (name,)).fetchone():
        c = classify.classify_course(name, institution, type_raw)
        con.execute("INSERT INTO course_master (course_name_raw, std_name, field, operation, method, rule_note)"
                    " VALUES (?,?,?,?,?,?)",
                    (name, c["std_name"], c["field"], c["operation"], c["method"], c["rule_note"]))
    if institution and not con.execute("SELECT 1 FROM institution_master WHERE institution_raw=?",
                                       (institution,)).fetchone():
        con.execute("INSERT INTO institution_master VALUES (?,?)", (institution, classify.std_institution(institution)))


def approve(batch_id: int, user: str, path=None) -> dict:
    """검토가 끝난 배치를 최종 데이터에 반영한다. 한 트랜잭션이므로 중간 오류 시 전부 취소된다."""
    with db.tx(path) as con:
        b = _require_status(con, batch_id, "staged")
        rows = [dict(r) for r in con.execute("SELECT * FROM staging_row WHERE batch_id=? ORDER BY row_no", (batch_id,))]
        pending = [r["row_no"] for r in rows if r["decision"] == "pending"]
        if pending:
            raise ValueError(f"검토하지 않은 건이 {len(pending)}건 있습니다. 먼저 반영 여부를 정하세요.")
        applied = {"inserted": 0, "updated": 0, "skipped": 0}
        if b["kind"] == "roster":
            month = b["snapshot_month"]
            latest = con.execute("SELECT MAX(snapshot_month) FROM employee_snapshot").fetchone()[0]
            is_latest = latest is None or month >= latest  # 과거 월 소급 업로드는 최신 정보를 덮어쓰지 않는다
            for r in rows:
                p = json.loads(r["payload"])
                if r["decision"] == "skip":
                    applied["skipped"] += 1
                    continue
                emp = p["emp_no"]
                if r["category"] == "missing":
                    con.execute("UPDATE employee SET roster_state='명부미포함' WHERE emp_no=?", (emp,))
                    db.log_change(con, user, "update", "employee", emp, "roster_state", "명부확인", "명부미포함",
                                  "명부에서 확인되지 않음", batch_id)
                    continue
                attrs = [a for a in db.EMP_ATTRS if a in p]
                old = con.execute("SELECT * FROM employee WHERE emp_no=?", (emp,)).fetchone()
                if old is None:
                    con.execute(f"INSERT INTO employee (emp_no, {', '.join(attrs)}, last_seen_month) VALUES"
                                f" (?, {', '.join('?' * len(attrs))}, ?)" if attrs else
                                "INSERT INTO employee (emp_no, last_seen_month) VALUES (?, ?)",
                                [emp, *[p[a] for a in attrs], month])
                    db.log_change(con, user, "insert", "employee", emp, batch_id=batch_id)
                    applied["inserted"] += 1
                elif is_latest:
                    changed = False
                    for a in attrs:
                        if old[a] != p[a]:
                            con.execute(f"UPDATE employee SET {a}=? WHERE emp_no=?", (p[a], emp))
                            db.log_change(con, user, "update", "employee", emp, a, old[a], p[a], "명부 갱신", batch_id)
                            changed = True
                    if old["roster_state"] != "명부확인":
                        con.execute("UPDATE employee SET roster_state='명부확인' WHERE emp_no=?", (emp,))
                        db.log_change(con, user, "update", "employee", emp, "roster_state", old["roster_state"],
                                      "명부확인", "명부 재확인", batch_id)
                    applied["updated"] += changed
                # 기준월 스냅샷: 스냅샷에 없는 항목은 직원 최신 정보가 아니라 NULL로 둔다(추정 금지)
                con.execute("DELETE FROM employee_snapshot WHERE snapshot_month=? AND emp_no=?", (month, emp))
                con.execute(f"INSERT INTO employee_snapshot (snapshot_month, emp_no, batch_id"
                            f"{''.join(', ' + a for a in attrs)}) VALUES (?,?,?{', ?' * len(attrs)})",
                            [month, emp, batch_id, *[p[a] for a in attrs]])
            con.execute("UPDATE employee SET last_seen_month=(SELECT MAX(snapshot_month) FROM employee_snapshot s"
                        " WHERE s.emp_no=employee.emp_no)")
        else:
            for r in rows:
                p = json.loads(r["payload"])
                if r["decision"] == "skip":
                    applied["skipped"] += 1
                    continue
                attrs = [a for a in db.REC_ATTRS if a in p]
                if r["decision"] == "update":
                    rid = int(r["target_id"])
                    old = con.execute("SELECT * FROM training_record WHERE record_id=?", (rid,)).fetchone()
                    for a in attrs:
                        if old[a] != p[a]:
                            con.execute(f"UPDATE training_record SET {a}=? WHERE record_id=?", (p[a], rid))
                            db.log_change(con, user, "update", "training_record", rid, a, old[a], p[a],
                                          "원본 변경 반영", batch_id)
                    applied["updated"] += 1
                else:
                    con.execute(f"INSERT INTO training_record ({', '.join(attrs)}, batch_id) VALUES"
                                f" ({', '.join('?' * len(attrs))}, ?)", [*[p[a] for a in attrs], batch_id])
                    applied["inserted"] += 1
                ensure_masters(con, p["course_name_raw"], p.get("institution_raw"), p.get("type_raw"))
        summary = json.loads(b["summary"] or "{}")
        summary["applied"] = applied
        con.execute("UPDATE upload_batch SET status='approved', decided_by=?, decided_at=?, summary=? WHERE batch_id=?",
                    (user, db.now(), json.dumps(summary, ensure_ascii=False), batch_id))
    return applied


def reject(batch_id: int, user: str, reason: str = "", path=None) -> None:
    """반영하지 않고 종료. 원본 파일과 스테이징 내역은 남는다."""
    with db.tx(path) as con:
        _require_status(con, batch_id, "staged")
        con.execute("UPDATE upload_batch SET status='rejected', decided_by=?, decided_at=?, notes=? WHERE batch_id=?",
                    (user, db.now(), reason, batch_id))


def rollback(batch_id: int, user: str, path=None) -> dict:
    """승인된 배치의 반영 내용을 되돌린다. 이후 다른 수정이 덮어쓴 항목은 건드리지 않고 보고한다."""
    result = {"reverted": 0, "removed": 0, "conflicts": []}
    with db.tx(path) as con:
        b = _require_status(con, batch_id, "approved")
        logs = [dict(r) for r in con.execute("SELECT * FROM change_log WHERE batch_id=? ORDER BY log_id DESC", (batch_id,))]
        for lg in logs:
            tbl, pk = lg["tbl"], db.PK[lg["tbl"]]
            if lg["op"] == "update":
                cur = con.execute(f"SELECT {lg['col']} FROM {tbl} WHERE {pk}=?", (lg["row_key"],)).fetchone()
                if cur is None:
                    continue
                if (None if cur[0] is None else str(cur[0])) != lg["new_value"]:
                    result["conflicts"].append(f"{tbl} {lg['row_key']}.{lg['col']}")
                    continue
                con.execute(f"UPDATE {tbl} SET {lg['col']}=? WHERE {pk}=?", (lg["old_value"], lg["row_key"]))
                result["reverted"] += 1
            elif lg["op"] == "insert" and tbl == "employee":
                other = con.execute("SELECT 1 FROM employee_snapshot WHERE emp_no=? AND batch_id!=?",
                                    (lg["row_key"], batch_id)).fetchone()
                if other:
                    result["conflicts"].append(f"employee {lg['row_key']}(다른 월 명부에도 있어 유지)")
                else:
                    con.execute("DELETE FROM employee WHERE emp_no=?", (lg["row_key"],))
                    result["removed"] += 1
        if b["kind"] == "roster":
            con.execute("DELETE FROM employee_snapshot WHERE batch_id=?", (batch_id,))
            con.execute("UPDATE employee SET last_seen_month=(SELECT MAX(snapshot_month) FROM employee_snapshot s"
                        " WHERE s.emp_no=employee.emp_no)")
        else:
            result["removed"] += con.execute("DELETE FROM training_record WHERE batch_id=?", (batch_id,)).rowcount
        db.log_change(con, user, "delete", "upload_batch", batch_id, reason="배치 복구(롤백)")
        con.execute("UPDATE upload_batch SET status='rolled_back', decided_by=?, decided_at=?, notes=? WHERE batch_id=?",
                    (user, db.now(), json.dumps(result, ensure_ascii=False), batch_id))
    return result


def batches(path=None) -> pd.DataFrame:
    return db.read("SELECT * FROM upload_batch ORDER BY batch_id DESC", path=path)
