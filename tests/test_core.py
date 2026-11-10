"""적재 규칙과 지표 정의에 대한 테스트. 임시 DB에서 돈다."""
import io
from datetime import date

import pandas as pd
import pytest

from hrli import analysis, auth, classify, config, db, ingest, metrics


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "MIN_GROUP_SIZE", 1)
    db.init_db()
    return tmp_path


def xlsx(rows):
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    return buf.getvalue()


def roster(month, rows, file_type="full", approve=True):
    b = ingest.stage(xlsx(rows), f"명부_{month}.xlsx", "roster", file_type, "t", month)
    if approve:
        ingest.approve(b, "t")
    return b


def training(rows, file_type="full", approve=True):
    b = ingest.stage(xlsx(rows), "이력.xlsx", "training", file_type, "t")
    if approve:
        ingest.approve(b, "t")
    return b


def emp(no, grp="A그룹", job="사무", status="재직"):
    return {"사번": no, "성명": f"직원{no}", "재직상태": status, "그룹": grp, "직종": job, "입사일": "2020-01-01"}


def rec(no, name="문제해결 기본과정", start="2026-02-10", hours=8, inst="한국능률협회", typ="사외", **kw):
    return {"사번": no, "교육명": name, "시작일": start, "종료일": start, "교육시간": hours, "교육기관": inst,
            "교육유형": typ, **kw}


def cats(batch_id):
    return ingest.staging(batch_id)["category"].value_counts().to_dict()


def test_roster_change_snapshot_and_missing_is_not_retirement(env):
    roster("2026-01", [emp("1"), emp("2")])
    b = roster("2026-02", [emp("1", grp="B그룹")])
    assert cats(b) == {"changed": 1, "missing": 1}
    e = db.read("SELECT * FROM employee").set_index("emp_no")
    assert e.at["1", "grp"] == "B그룹"
    assert e.at["2", "roster_state"] == "명부미포함" and e.at["2", "status"] == "재직"  # 퇴직 처리하지 않는다
    snap = db.read("SELECT snapshot_month, grp FROM employee_snapshot WHERE emp_no='1' ORDER BY 1")
    assert snap["grp"].tolist() == ["A그룹", "B그룹"]  # 과거 시점 조직 보존
    assert len(db.read("SELECT * FROM change_log WHERE col='grp'")) == 1


def test_backfilled_roster_does_not_overwrite_latest(env):
    roster("2026-03", [emp("1", grp="B그룹")])
    roster("2026-01", [emp("1", grp="A그룹")])
    assert db.read("SELECT grp FROM employee").iat[0, 0] == "B그룹"
    assert len(db.read("SELECT * FROM employee_snapshot")) == 2


def test_training_dedup_suspect_unmatched_error(env):
    roster("2026-01", [emp("1")])
    training([rec("1")])
    b = training([rec("1"),                         # 완전히 동일 → 중복
                  rec("1", start="2026-05-10"),     # 같은 교육 재수강 → 신규
                  rec("1", hours=16),               # 일부만 다름 → 중복 의심
                  rec("9"),                         # 명부에 없는 사번
                  rec("1", name=None)], approve=False)  # 교육명 누락
    assert cats(b) == {"duplicate": 1, "new": 1, "suspect": 1, "unmatched": 1, "error": 1}
    with pytest.raises(ValueError):
        ingest.approve(b, "t")                      # 검토 안 한 중복 의심 건이 있으면 승인 불가
    s = ingest.staging(b)
    ingest.set_decision(b, [int(s.loc[s["category"] == "suspect", "row_no"].iat[0])], "update")
    ingest.approve(b, "t")
    r = db.read("SELECT emp_no, start_date, hours_raw FROM training_record ORDER BY record_id")
    assert len(r) == 3 and r["hours_raw"].tolist() == [16.0, 8.0, 8.0]
    assert len(metrics.load_fact()) == 2            # 미매칭 이력은 보관하되 통계에는 넣지 않는다


def test_source_id_detects_change_and_full_file_never_deletes(env):
    roster("2026-01", [emp("1")])
    training([rec("1", 교육이력ID="T1"), rec("1", start="2026-03-01", 교육이력ID="T2")])
    b = training([rec("1", hours=4, 교육이력ID="T1")], approve=False)  # T2가 빠진 전체 파일
    assert cats(b) == {"changed": 1}
    ingest.approve(b, "t")
    r = db.read("SELECT source_id, hours_raw FROM training_record ORDER BY source_id")
    assert r["hours_raw"].tolist() == [4.0, 8.0]    # T1 수정, T2는 삭제되지 않음


def test_rollback_restores_previous_state(env):
    roster("2026-01", [emp("1")])
    b1 = training([rec("1", 교육이력ID="T1")])
    b2 = training([rec("1", hours=99, 교육이력ID="T1"), rec("1", start="2026-04-01", 교육이력ID="T2")])
    assert len(db.read("SELECT * FROM training_record")) == 2
    ingest.rollback(b2, "t")
    r = db.read("SELECT hours_raw FROM training_record")
    assert r["hours_raw"].tolist() == [8.0]
    assert ingest.batches().set_index("batch_id").at[b2, "status"] == "rolled_back"
    b3 = roster("2026-02", [emp("1", grp="B그룹"), emp("2")])
    ingest.rollback(b3, "t")
    e = db.read("SELECT emp_no, grp FROM employee")
    assert e.values.tolist() == [["1", "A그룹"]]
    assert b1


def test_fatal_file_keeps_raw_and_changes_nothing(env):
    b = ingest.stage(xlsx([{"이름": "x"}]), "잘못된파일.xlsx", "training", "full", "t")
    row = ingest.batches().iloc[0]
    assert row["status"] == "failed" and "필수 항목" in row["notes"]
    assert len(list(config.RAW_DIR.iterdir())) == 1 and b
    assert db.read("SELECT COUNT(*) FROM training_record").iat[0, 0] == 0


def test_hunet_rule_and_unknown_stays_unclassified():
    c = classify.classify_course("비즈니스 영어 회화", "(주)휴넷", "사외")
    assert (c["operation"], c["method"], c["field"]) == ("사내", "온라인", "어학")
    assert classify.classify_course("마음챙김 특강", "휴넷")["field"] == "미분류"   # 기관명만으로 분야를 추정하지 않는다
    assert classify.classify_course("알 수 없는 과정", "어딘가")["operation"] == "미분류"
    sim = classify.similar_courses(["문제해결 기본과정", "문제해결 과정 기본", "산업안전 법정교육"])
    assert len(sim) == 1


def test_metric_definitions(env):
    roster("2026-01", [emp("1"), emp("2"), emp("3", grp="B그룹"), emp("4", grp="B그룹", status="퇴직")])
    training([rec("1"), rec("2"),                                    # 같은 과정 1회 운영에 2명
              rec("1", name="비즈니스 영어 회화", start="2026-03-02", hours=100, inst="휴넷", typ="사내"),
              rec("1", name="예전 과정", start="2024-03-02")])
    with db.tx() as con:
        con.execute("INSERT INTO time_rule (scope, value) VALUES ('field','어학')")
    fact = metrics.load_fact()
    p = metrics.make_period("year", 2026)
    pop, _ = metrics.population(p)
    k = metrics.kpis(metrics.in_period(fact, p), pop)
    assert k["참여이력 건수"] == 3 and k["과정 운영 횟수"] == 2 and k["참여 인원"] == 2
    assert k["총 교육 시간"] == 16.0                # 어학 100시간은 시간 통계에서만 제외
    assert k["1인당 평균 교육 시간"] == 8.0
    assert k["분석 대상 인원"] == 3 and k["미참여 인원"] == 1 and k["참여율"] == round(2 / 3, 4)  # 퇴직자는 분모 제외
    g = metrics.group_summary(metrics.in_period(fact, p), pop, "grp")
    assert g.at["A그룹", "참여율"] == 1.0 and g.at["B그룹", "참여율"] == 0.0
    assert analysis.non_participants(pop, metrics.in_period(fact, p))["emp_no"].tolist() == ["3"]
    t = analysis.find_targets(pop, fact, date(2026, 12, 31), not_field="어학")
    assert sorted(t["emp_no"]) == ["2", "3"]


def test_org_basis_at_time_uses_snapshot(env):
    roster("2026-01", [emp("1", grp="A그룹")])
    roster("2026-02", [emp("1", grp="B그룹")])
    training([rec("1", start="2026-01-15"), rec("1", start="2025-06-01")])
    fact = metrics.load_fact()
    assert set(fact["grp"]) == {"B그룹"}
    at = metrics.with_org(fact, "at_time").set_index("start_date")["grp"]
    assert at["2026-01-15"] == "A그룹" and at["2025-06-01"] == metrics.NO_SNAPSHOT  # 자료가 없으면 추정하지 않는다


def test_period_comparison_uses_same_length():
    p = metrics.make_period("year", 2026).clip(date(2026, 9, 12))
    assert (p.start, p.end) == (date(2026, 1, 1), date(2026, 9, 30))
    y = p.shift_years(1)
    assert (y.start, y.end) == (date(2025, 1, 1), date(2025, 9, 30))
    q = metrics.make_period("quarter", 2026, 1).previous()
    assert (q.start, q.end) == (date(2025, 10, 1), date(2025, 12, 31))


def test_edit_keeps_original_and_logs(env):
    roster("2026-01", [emp("1")])
    training([rec("1", hours=300)])
    db.update_field("training_record", 1, "hours_adj", 8.0, "t", "입력 오류 보정")
    r = db.read("SELECT hours_raw, hours_adj FROM training_record")
    assert r.values.tolist() == [[300.0, 8.0]]
    assert metrics.load_fact()["hours"].iat[0] == 8.0
    lg = db.read("SELECT * FROM change_log WHERE tbl='training_record'").iloc[0]
    assert (lg["new_value"], lg["reason"], lg["username"]) == ("8.0", "입력 오류 보정", "t")
    with pytest.raises(ValueError):
        db.update_field("training_record", 1, "record_id", 5, "t")


def test_auth_roles(env):
    auth.create_user("m", "password-1", "org_manager", "A그룹")
    assert auth.verify("m", "wrong") is None
    u = auth.verify("m", "password-1")
    assert auth.can(u, "analysis") and not auth.can(u, "person") and not auth.can(u, "manage")
    with pytest.raises(ValueError):
        auth.create_user("x", "short", "admin")
