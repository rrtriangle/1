"""적재 규칙·지표 정의·로그인에 대한 테스트. 내장 unittest만 쓴다."""
import io
from datetime import date

import re
import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from hrli import analysis, auth, classify, config, db, flow, ingest, metrics


class raises:
    """with raises(ValueError): ... 형태로 예외 발생을 확인한다."""
    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, typ, val, tb):
        assert typ is not None and issubclass(typ, self.exc), f"{self.exc.__name__}가 발생해야 한다"
        return True


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


def test_roster_change_snapshot_and_missing_is_not_retirement():
    roster("2026-01", [emp("1"), emp("2")])
    b = roster("2026-02", [emp("1", grp="B그룹")])
    assert cats(b) == {"changed": 1, "missing": 1}
    e = db.read("SELECT * FROM employee").set_index("emp_no")
    assert e.at["1", "grp"] == "B그룹"
    assert e.at["2", "roster_state"] == "명부미포함" and e.at["2", "status"] == "재직"  # 퇴직 처리하지 않는다
    snap = db.read("SELECT snapshot_month, grp FROM employee_snapshot WHERE emp_no='1' ORDER BY 1")
    assert snap["grp"].tolist() == ["A그룹", "B그룹"]  # 과거 시점 조직 보존
    assert len(db.read("SELECT * FROM change_log WHERE col='grp'")) == 1


def test_backfilled_roster_does_not_overwrite_latest():
    roster("2026-03", [emp("1", grp="B그룹")])
    roster("2026-01", [emp("1", grp="A그룹")])
    assert db.read("SELECT grp FROM employee").iat[0, 0] == "B그룹"
    assert len(db.read("SELECT * FROM employee_snapshot")) == 2


def test_training_dedup_suspect_unmatched_error():
    roster("2026-01", [emp("1")])
    training([rec("1")])
    b = training([rec("1"),                         # 완전히 동일 → 중복
                  rec("1", start="2026-05-10"),     # 같은 교육 재수강 → 신규
                  rec("1", hours=16),               # 일부만 다름 → 중복 의심
                  rec("9"),                         # 명부에 없는 사번
                  rec("1", name=None)], approve=False)  # 교육명 누락
    assert cats(b) == {"duplicate": 1, "new": 1, "suspect": 1, "unmatched": 1, "error": 1}
    with raises(ValueError):
        ingest.approve(b, "t")                      # 검토 안 한 중복 의심 건이 있으면 승인 불가
    s = ingest.staging(b)
    ingest.set_decision(b, [int(s.loc[s["category"] == "suspect", "row_no"].iat[0])], "update")
    ingest.approve(b, "t")
    r = db.read("SELECT emp_no, start_date, hours_raw FROM training_record ORDER BY record_id")
    assert len(r) == 3 and r["hours_raw"].tolist() == [16.0, 8.0, 8.0]
    assert len(metrics.load_fact()) == 2            # 미매칭 이력은 보관하되 통계에는 넣지 않는다


def test_source_id_detects_change_and_full_file_never_deletes():
    roster("2026-01", [emp("1")])
    training([rec("1", 교육이력ID="T1"), rec("1", start="2026-03-01", 교육이력ID="T2")])
    b = training([rec("1", hours=4, 교육이력ID="T1")], approve=False)  # T2가 빠진 전체 파일
    assert cats(b) == {"changed": 1}
    ingest.approve(b, "t")
    r = db.read("SELECT source_id, hours_raw FROM training_record ORDER BY source_id")
    assert r["hours_raw"].tolist() == [4.0, 8.0]    # T1 수정, T2는 삭제되지 않음


def test_rollback_restores_previous_state():
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


def test_fatal_file_keeps_raw_and_changes_nothing():
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


def test_metric_definitions():
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


def test_org_basis_at_time_uses_snapshot():
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


def test_edit_keeps_original_and_logs():
    roster("2026-01", [emp("1")])
    training([rec("1", hours=300)])
    db.update_field("training_record", 1, "hours_adj", 8.0, "t", "입력 오류 보정")
    r = db.read("SELECT hours_raw, hours_adj FROM training_record")
    assert r.values.tolist() == [[300.0, 8.0]]
    assert metrics.load_fact()["hours"].iat[0] == 8.0
    lg = db.read("SELECT * FROM change_log WHERE tbl='training_record'").iloc[0]
    assert (lg["new_value"], lg["reason"], lg["username"]) == ("8.0", "입력 오류 보정", "t")
    with raises(ValueError):
        db.update_field("training_record", 1, "record_id", 5, "t")


def test_otp_login():
    sent = []
    mailer = lambda to, subject, body: sent.append((to, body))  # noqa: E731
    auth.save_user("boss@corp.com", "admin")
    auth.MAILER = mailer
    auth.set_otp_enabled(True, "boss@corp.com")
    auth.save_user("Manager@Corp.com", "org_manager", "A그룹")
    assert auth.request_code("nobody@corp.com", mailer) == auth.SENT_MSG and not sent  # 미등록: 같은 문구, 발송 없음
    assert auth.request_code("manager@corp.com", mailer) == auth.SENT_MSG and len(sent) == 1
    code = re.search(r"\b(\d{6})\b", sent[0][1]).group(1)
    assert code not in db.read("SELECT code_hash FROM otp").iat[0, 0]           # 코드 원문은 저장하지 않는다
    assert "다시 요청" in auth.request_code("manager@corp.com", mailer)            # 재발송 대기
    wrong = "000000" if code != "000000" else "111111"
    assert auth.verify_code("manager@corp.com", wrong) is None
    u = auth.verify_code(" MANAGER@corp.com ", code)
    assert u["role"] == "org_manager" and u["org_scope"] == "A그룹"
    assert auth.verify_code("manager@corp.com", code) is None                   # 1회용
    assert auth.can(u, "analysis") and not auth.can(u, "person") and not auth.can(u, "manage")


def test_otp_expiry_attempt_limit_and_inactive():
    sent = []
    mailer = lambda to, subject, body: sent.append(body)  # noqa: E731
    auth.save_user("a@corp.com", "admin")
    auth.MAILER = mailer
    auth.set_otp_enabled(True, "a@corp.com")
    auth.request_code("a@corp.com", mailer)
    code = re.search(r"\b(\d{6})\b", sent[-1]).group(1)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(config.OTP_MAX_ATTEMPTS):
        assert auth.verify_code("a@corp.com", wrong) is None
    assert auth.verify_code("a@corp.com", code) is None                         # 횟수 초과 후에는 맞는 코드도 거부
    with db.tx() as con:
        con.execute("DELETE FROM otp")
    auth.request_code("a@corp.com", mailer)
    code = re.search(r"\b(\d{6})\b", sent[-1]).group(1)
    with db.tx() as con:
        con.execute("UPDATE otp SET expires_at='2000-01-01T00:00:00'")
    assert auth.verify_code("a@corp.com", code) is None                         # 만료
    assert "보내지 못했습니다" in auth.request_code("a@corp.com", None)             # 메일 설정 없음 → 코드 폐기
    assert db.read("SELECT COUNT(*) FROM otp").iat[0, 0] == 0
    with raises(ValueError):
        auth.save_user("a@corp.com", "admin", active=False)                     # 마지막 관리자는 중지 불가
    auth.save_user("b@corp.com", "analyst")
    auth.save_user("b@corp.com", "analyst", active=False)
    assert auth.request_code("b@corp.com", mailer) == auth.SENT_MSG and len(sent) == 2  # 중지된 사용자에게는 발송 없음



def test_otp_is_off_by_default_and_cannot_turn_on_without_mail():
    auth.save_user("a@corp.com", "admin")
    sent = []
    assert not auth.otp_enabled()
    assert auth.request_code("a@corp.com", lambda *x: sent.append(x)) == auth.OTP_OFF_MSG and not sent
    auth.MAILER = None
    with raises(ValueError):
        auth.set_otp_enabled(True, "a@corp.com")
    auth.MAILER = lambda *x: sent.append(x)
    auth.set_otp_enabled(True, "a@corp.com")
    assert auth.request_code("a@corp.com", auth.MAILER) == auth.SENT_MSG and len(sent) == 1
    auth.set_otp_enabled(False, "a@corp.com")                                  # 끄면 발송된 코드도 폐기
    assert db.read("SELECT COUNT(*) FROM otp").iat[0, 0] == 0
    log = db.read("SELECT action FROM access_log")["action"].tolist()
    assert "이메일 인증 로그인 켜기" in log and "이메일 인증 로그인 끄기" in log


def test_dataiku_login_mapping():
    auth.save_user("me@corp.com", "admin", dss_login="HongGD")
    assert auth.get_user_by_login("honggd")["username"] == "me@corp.com"      # 대소문자 무시
    assert auth.get_user_by_login("someone") is None
    assert auth.get_user_by_login(None) is None
    with raises(ValueError):
        auth.save_user("other@corp.com", "analyst", dss_login="honggd")         # 한 Dataiku 계정은 한 사용자에게만
    auth.save_user("t@corp.com", "trainer", dss_login="kim")
    auth.save_user("t@corp.com", "trainer", dss_login="kim", active=False)
    assert auth.get_user_by_login("kim") is None                               # 중지된 사용자는 로그인 불가
    assert auth.dataiku_identify({}) is None                                   # Dataiku 밖에서는 확인 불가


def roster_df(month_rows):
    return pd.DataFrame([{**emp(no, grp=g), "기준월": m} for m, no, g in month_rows])


def test_flow_stage_splits_months_and_skips_same_content():
    df = roster_df([("2026.1", "1", "A그룹"), ("2026-01", "2", "A그룹"), ("202602", "1", "B그룹")])
    rows = flow.stage_dataframe(df, "roster_upload", "roster", "full", "t")
    assert [(r["month"], r["rows"], r["status"]) for r in rows] == [("2026-01", 2, "staged"), ("2026-02", 1, "staged")]
    again = flow.stage_dataframe(df, "roster_upload", "roster", "full", "t")
    assert [r["status"] for r in again] == ["skipped", "skipped"]                # 같은 내용은 다시 올리지 않는다
    r = ingest.approve_clean("t")
    assert r["approved"] == [1, 2] and r["waiting"] == []
    e = db.read("SELECT emp_no, grp, roster_state FROM employee ORDER BY emp_no").values.tolist()
    assert e == [["1", "B그룹", "명부확인"], ["2", "A그룹", "명부미포함"]]       # 월 순서대로 반영
    with raises(ValueError):
        flow.stage_dataframe(roster_df([("언젠가", "1", "A그룹")]), "x", "roster", "full", "t")


def test_flow_dates_from_dataset_do_not_shift():
    roster("2026-01", [emp("1")])
    df = pd.DataFrame([rec("1", start="2026-03-01")])
    df["시작일"] = pd.to_datetime(df["시작일"]).dt.tz_localize("UTC")            # Dataiku가 날짜를 UTC로 읽는 경우
    df["종료일"] = pd.to_datetime(df["종료일"])
    flow.stage_dataframe(df, "training_upload", "training", "full", "t")
    ingest.approve_clean("t")
    assert db.read("SELECT start_date, end_date FROM training_record").values.tolist() == [["2026-03-01", "2026-03-01"]]


def test_approve_clean_stops_at_batch_needing_review():
    roster("2026-01", [emp("1")])
    training([rec("1")])
    b1 = training([rec("1", hours=16)], approve=False)                         # 중복 의심 → 판단 필요
    b2 = training([rec("1", start="2026-06-01")], approve=False)
    r = ingest.approve_clean("t")
    assert r["approved"] == [] and r["waiting"] == [b1, b2]                    # 뒤 배치도 순서를 지키려고 남긴다


def test_export_tables():
    roster("2026-01", [emp("1"), emp("2")])
    training([rec("1"), rec("9")])
    t = flow.export_tables()
    assert set(t) == {"hrli_employee", "hrli_snapshot", "hrli_training", "hrli_training_all", "hrli_course",
                      "hrli_batches", "hrli_change_log"}
    assert len(t["hrli_training"]) == 1 and {"사번", "표준교육명", "교육분야", "분석용시간"} <= set(t["hrli_training"].columns)
    assert sorted(t["hrli_training_all"]["통계반영"]) == ["미매칭", "반영"]
    assert len(t["hrli_employee"]) == 2 and len(t["hrli_snapshot"]) == 2


class CoreTests(unittest.TestCase):
    """각 테스트를 새 임시 DB에서 실행한다. 실행: python -m unittest discover -s tests"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (config.DB_PATH, config.RAW_DIR, config.MIN_GROUP_SIZE, auth.MAILER)
        config.DB_PATH, config.RAW_DIR, config.MIN_GROUP_SIZE = self.tmp / "t.sqlite3", self.tmp / "raw", 1
        auth.MAILER = None
        db.init_db()

    def tearDown(self):
        config.DB_PATH, config.RAW_DIR, config.MIN_GROUP_SIZE, auth.MAILER = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)


for _name, _fn in list(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        setattr(CoreTests, _name, (lambda f: lambda self: f())(_fn))
        del globals()[_name]
del _name, _fn

if __name__ == "__main__":
    unittest.main()
