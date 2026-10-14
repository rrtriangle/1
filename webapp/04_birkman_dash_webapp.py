# -*- coding: utf-8 -*-
"""동우화인켐 버크만 팀 리포트 / Dataiku Dash Webapp
Dataiku > Code > Webapps > Dash의 Python 편집기에 파일 전체를 붙여 넣습니다.
Python 레시피에 넣지 마세요. Dataiku가 만든 app 객체를 그대로 사용합니다.
실제 데이터를 읽습니다. 기본 preview 모드는 별도 로그인 없이 전체 인사 화면을 엽니다.
본인만 접근 가능한 비공개 웹앱에서 사용하세요. 직원 공개 전 email_otp 또는 password로 전환하세요.
"""
# =====================================================================
# 1. 회사에서 수정할 설정 (메일 비밀번호를 여기에 넣지 않습니다)
# =====================================================================
# [현재 테스트 설정] 접속자를 인증하지 않고 최초 관리자의 인사 화면을 엽니다.
# 주소에 접근할 수 있는 모든 사람이 같은 화면을 봅니다. 개인별 권한은 적용되지 않습니다.
# 아래 테스트 설정 한 줄을 삭제/주석 처리하고, 운영 방식 한 줄만 주석 해제하세요.
AUTH_MODE = "preview"
# AUTH_MODE = "email_otp"  # 운영 A: 회사 메일 채널을 설정한 뒤 이메일 인증번호 로그인
# AUTH_MODE = "password"   # 운영 B: 본인 확인 후 일회용 등록코드 → 본인이 비밀번호 설정
# password 전환 전: 테스트 화면 관리자 메뉴에서 본인의 등록코드를 발급해 보관하세요.
# 코드·DB를 공개 폴더에 두지 마세요. 운영 인증은 HTTPS 사내 주소에서만 사용합니다.
MAIL_CHANNEL_ID = ""           # 선배님께 확인한 Dataiku 메일 채널 ID
FIRST_ADMIN_EMAIL = ""         # 최초 관리자 본인의 회사 이메일
AUTH_FOLDER_ID = "birkman_webapp_state"  # 새 로컬 관리 폴더 이름 또는 ID
TEAM_CACHE_FOLDER = "birkman_team_cache" # 2단계 출력 폴더 이름 또는 ID
EXTRACT_CACHE_FOLDER = "birkman_cache"   # 1단계 출력 폴더 이름 또는 ID

PEOPLE_DATASET = "birkman_people"
SUMMARY_DATASET = "birkman_org_summary"
MEMBERS_DATASET = "birkman_org_members"
AI_INPUT_DATASET = "birkman_ai_inputs"
AI_REPORT_DATASET = "birkman_ai_reports_raw"
ALLOWED_DOMAIN = "dwchem.co.kr"
APP_TITLE = "버크만 팀 리포트"
APP_VERSION = "2026-10-13.1"
DATA_CACHE_SECONDS = 60
# 테스트 중에는 보고서를 읽다가 페이지가 재배치되지 않도록 자동 갱신을 끕니다.
# 인증 모드의 주기적 세션 확인은 유지합니다. 테스트 자료 갱신은 '새로 읽기'를 누르세요.
SHOW_LEADER_ONLY_GROUPS = False  # 팀원이 없는 그룹 직속 조직은 선택 목록에서 숨김
OTP_LIFETIME_SECONDS = 300
SESSION_LIFETIME_SECONDS = 8 * 60 * 60
ENROLLMENT_LIFETIME_SECONDS = 30 * 60  # 등록코드는 30분·1회만 사용
PASSWORD_MIN_LENGTH = 15             # 단일 비밀번호 인증: 긴 문장형 비밀번호 허용
PASSWORD_HASH_METHOD = "pbkdf2:sha256:600000"  # Flask/Werkzeug 기본 기능, 평문 저장 안 함
# HTTPS 사내 URL에서 사용합니다. Secure 쿠키를 해제하는 우회 옵션은 없습니다.
# 상태 DB는 단일 DSS 서버의 로컬 디스크에 보관합니다. 컨테이너/NFS/다중 노드 불가.

import collections
import contextlib
import datetime as dt
import hashlib
import hmac
import html as escaping
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

import dataiku
from dash import dcc, html, Input, Output, State, callback_context, no_update
from dash.exceptions import PreventUpdate
import plotly.graph_objects as go
from flask import session, request, has_request_context
from werkzeug.security import generate_password_hash, check_password_hash

LOG = logging.getLogger("birkman_webapp")
MAPS = {"interest": "흥미", "usual": "평소행동", "needs": "욕구", "stress": "스트레스행동"}
INTERESTS = {"numerical": "숫자", "administrative": "관리", "scientific": "과학",
             "literary": "문학", "technical": "기술", "musical": "음악", "artistic": "예술",
             "outdoor": "야외", "persuasive": "설득", "social_service": "사회복지"}
PALETTE = {"빨강": "#cf655e", "초록": "#438777", "노랑": "#c09b3c", "파랑": "#527fae", "중앙선": "#7d8795"}
ORIENTATIONS = {"파랑": "기획/전략", "초록": "영업/마케팅", "빨강": "운영/기술", "노랑": "관리/회계"}
ROLE_LABELS = {"member": "팀원", "part_leader": "파트장", "team_leader": "팀장", "hr": "인사팀"}
STATUS_LABELS = {"complete": "진단 있음", "partial": "일부 항목 확인", "missing": "진단 없음",
                 "excluded_ta": "진단 대상 아님", "duplicate_review": "파일 확인 필요",
                 "name_review": "연결 확인 필요", "master_review": "명부 확인 필요", "failed": "추출 확인 필요"}
KST = dt.timezone(dt.timedelta(hours=9))


def txt(v):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return ""
    return str(v).strip()


def flag(v):
    return v is True or txt(v).lower() in {"true", "1", "y", "yes"}


def num(v):
    if isinstance(v, bool) or not txt(v):
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def js(v, default=None):
    try:
        return json.loads(v) if isinstance(v, str) else (v if v is not None else default)
    except (ValueError, TypeError):
        return default


def pack(v):
    return json.dumps(v, ensure_ascii=False, allow_nan=False, sort_keys=True)


def email_address(v):
    v = txt(v).lower()
    return v if re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@" + re.escape(ALLOWED_DOMAIN), v) and len(v) < 255 else ""


def stamp(t=None):
    return dt.datetime.fromtimestamp(t or time.time(), KST).strftime("%Y.%m.%d %H:%M")


def fail_log(where, exc):
    # PDF 내용, 메일 본문, 인증번호, 쿠키, 직원 정보는 로그에 쓰지 않습니다.
    LOG.error("%s: %s", where, type(exc).__name__)


# =====================================================================
# 2. 로그인/권한 저장소: SQLite 트랜잭션으로 동시 요청·일회용 처리를 보장
# =====================================================================
class AccessStore:
    def __init__(self, directory, owner):
        if not email_address(owner):
            raise ValueError("FIRST_ADMIN_EMAIL에 본인 회사 이메일을 입력하세요.")
        self.owner = email_address(owner)
        self.path = str(Path(directory) / "birkman_access.sqlite3")
        # 새 DB에만 적용. 기존 폴더의 권한이나 다른 파일을 바꾸지 않습니다.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS access(email TEXT PRIMARY KEY, role TEXT NOT NULL,
                scopes TEXT NOT NULL, active INTEGER NOT NULL, admin INTEGER NOT NULL,
                revision INTEGER NOT NULL, modified TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS otp(email TEXT PRIMARY KEY, challenge TEXT NOT NULL,
                digest TEXT NOT NULL, browser TEXT NOT NULL, expires REAL NOT NULL, attempts INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(digest TEXT PRIMARY KEY, email TEXT NOT NULL,
                employee TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS limits(k TEXT NOT NULL, ts REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS limits_key_time ON limits(k, ts);
            CREATE TABLE IF NOT EXISTS audit(ts TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS credentials(email TEXT PRIMARY KEY, employee TEXT NOT NULL,
                password_hash TEXT NOT NULL, modified TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS enrollment(email TEXT PRIMARY KEY, employee TEXT NOT NULL,
                digest TEXT NOT NULL, expires REAL NOT NULL, attempts INTEGER NOT NULL);
            """)
            c.execute("INSERT OR IGNORE INTO settings VALUES ('secret',?)", (secrets.token_hex(32),))
            c.execute("INSERT OR IGNORE INTO settings VALUES ('owner',?)", (self.owner,))
            saved_owner = c.execute("SELECT v FROM settings WHERE k='owner'").fetchone()[0]
            if saved_owner != self.owner:
                raise ValueError("기존 DB의 최초 관리자와 설정이 다릅니다. 최초 관리자 이메일을 원래 값으로 복원하세요.")
            c.execute("INSERT OR IGNORE INTO access VALUES (?,?,?,1,1,1,?)",
                      (self.owner, "hr", "[]", stamp()))
            self.secret = c.execute("SELECT v FROM settings WHERE k='secret'").fetchone()[0]
        self.dummy_password_hash=generate_password_hash(secrets.token_urlsafe(32),method=PASSWORD_HASH_METHOD)

    @contextlib.contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=15000")
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def digest(self, value):
        return hmac.new(self.secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def rule(self, email):
        with self.db() as c:
            r = c.execute("SELECT * FROM access WHERE email=?", (email,)).fetchone()
        return dict(r) if r else {"email": email, "role": "member", "scopes": "[]", "active": 1,
                                  "admin": 0, "revision": 0, "modified": ""}

    @staticmethod
    def hit(c, key, seconds, maximum, now):
        n = c.execute("SELECT COUNT(*) FROM limits WHERE k=? AND ts>?", (key, now - seconds)).fetchone()[0]
        if n >= maximum:
            return False
        c.execute("INSERT INTO limits VALUES (?,?)", (key, now))
        return True

    def challenge(self, email, browser, eligible):
        now = time.time()
        with self.db() as c:
            c.execute("DELETE FROM limits WHERE ts<?", (now - 86400,))
            c.execute("DELETE FROM sessions WHERE expires<?", (now,))
            c.execute("DELETE FROM otp WHERE expires<?", (now,))
            # 존재하지 않는 이메일도 같은 제한을 적용하여 계정 탐색/무제한 발송 방지.
            limits = [("send:global", 3600, 2000), ("send:browser:" + browser, 900, 10),
                      ("send:email:" + email, 900, 5), ("cooldown:" + email, 60, 1)]
            allowed = [self.hit(c, k, sec, n, now) for k, sec, n in limits]
            if not all(allowed):
                return None
            if not eligible:
                return None
            code = "%06d" % secrets.randbelow(1000000)
            challenge = secrets.token_urlsafe(24)
            digest = self.digest(challenge + ":" + code)
            c.execute("INSERT OR REPLACE INTO otp VALUES (?,?,?,?,?,0)",
                      (email, challenge, digest, self.digest(browser), now + OTP_LIFETIME_SECONDS))
            return challenge, code

    def cancel_challenge(self, email, challenge):
        with self.db() as c:
            c.execute("DELETE FROM otp WHERE email=? AND challenge=?", (email, challenge))

    def verify(self, email, browser, code, employee):
        now = time.time()
        token = None
        with self.db() as c:
            allowed = [self.hit(c, "verify:browser:" + browser, 900, 30, now),
                       self.hit(c, "verify:email:" + email, 900, 10, now)]
            r = c.execute("SELECT * FROM otp WHERE email=?", (email,)).fetchone()
            if not all(allowed) or not r or r["expires"] <= now or r["attempts"] >= 5:
                return None
            # 인증번호는 발급을 요청한 브라우저에 연결합니다.
            if not hmac.compare_digest(r["browser"], self.digest(browser)):
                return None
            c.execute("UPDATE otp SET attempts=attempts+1 WHERE email=?", (email,))
            valid = re.fullmatch(r"\d{6}", txt(code)) and hmac.compare_digest(
                r["digest"], self.digest(r["challenge"] + ":" + txt(code)))
            if valid and employee:
                c.execute("DELETE FROM otp WHERE email=?", (email,))
                token = secrets.token_urlsafe(48)
                c.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                          (self.digest(token), email, employee, now + SESSION_LIFETIME_SECONDS))
                c.execute("INSERT INTO audit VALUES (?,?,?,?,?)", (stamp(), email, "login", email, ""))
        return token

    def lookup_session(self, token):
        if not token:
            return None
        with self.db() as c:
            row = c.execute("SELECT * FROM sessions WHERE digest=? AND expires>?",
                            (self.digest(token), time.time())).fetchone()
        return dict(row) if row else None

    def logout(self, token):
        if token:
            with self.db() as c:
                c.execute("DELETE FROM sessions WHERE digest=?", (self.digest(token),))

    def issue_enrollment(self, actor, email, employee):
        """관리자가 대면 또는 기존 사내 연락수단으로 본인 확인한 뒤 발급합니다.
        사번·이메일을 안다는 것만으로 본인 인증이 되지는 않습니다.
        평문 코드는 발급한 관리자 화면에 한 번만 표시하고 DB/로그에는 남기지 않습니다.
        """
        code=secrets.token_urlsafe(24)
        with self.db() as c:
            ar=c.execute("SELECT * FROM access WHERE email=?",(actor,)).fetchone()
            target=c.execute("SELECT * FROM access WHERE email=?",(email,)).fetchone()
            if not ar or not ar["active"] or not ar["admin"]:
                raise PermissionError("관리 권한이 없습니다")
            if not email_address(email) or not employee or (target and not target["active"]):
                raise ValueError("대상자의 로그인 허용 여부를 확인하세요.")
            c.execute("DELETE FROM enrollment WHERE expires<?",(time.time(),))
            c.execute("INSERT OR REPLACE INTO enrollment VALUES (?,?,?,?,0)",
                      (email,employee,self.digest(code),time.time()+ENROLLMENT_LIFETIME_SECONDS))
            c.execute("INSERT INTO audit VALUES (?,?,?,?,?)",(stamp(),actor,"enrollment_issue",email,""))
        return code

    def enroll_password(self, email, employee, code, password, browser):
        if not isinstance(password,str) or not PASSWORD_MIN_LENGTH<=len(password)<=128:
            raise ValueError("비밀번호는 15~128자로 입력하세요. 회사에서 쓰는 비밀번호와 다르게 정하세요.")
        now=time.time()
        with self.db() as c:
            c.execute("DELETE FROM limits WHERE ts<?",(now-86400,))
            allowed=[self.hit(c,"enroll:browser:"+browser,900,10,now),
                     self.hit(c,"enroll:email:"+email,900,5,now)]
            row=c.execute("SELECT * FROM enrollment WHERE email=?",(email,)).fetchone()
            ar=c.execute("SELECT active FROM access WHERE email=?",(email,)).fetchone()
            if not all(allowed) or not row or row["expires"]<=now or row["attempts"]>=5 or (ar and not ar["active"]):
                return False
            c.execute("UPDATE enrollment SET attempts=attempts+1 WHERE email=?",(email,))
            if row["employee"]!=employee or not hmac.compare_digest(row["digest"],self.digest(txt(code))):
                return False
            # Werkzeug는 Flask에 포함된 라이브러리입니다. PBKDF2+개인별 salt로 해시만 저장합니다.
            value=generate_password_hash(password,method=PASSWORD_HASH_METHOD)
            c.execute("INSERT OR REPLACE INTO credentials VALUES (?,?,?,?)",(email,employee,value,stamp()))
            c.execute("DELETE FROM enrollment WHERE email=?",(email,))
            c.execute("DELETE FROM sessions WHERE email=?",(email,))
            c.execute("DELETE FROM otp WHERE email=?",(email,))
            c.execute("INSERT INTO audit VALUES (?,?,?,?,?)",(stamp(),email,"password_set",email,""))
        return True

    def verify_password(self, email, employee, password, browser):
        now=time.time()
        if not isinstance(password,str) or len(password)>128:return None
        with self.db() as c:
            c.execute("DELETE FROM limits WHERE ts<?",(now-86400,))
            c.execute("DELETE FROM sessions WHERE expires<?",(now,))
            allowed=[self.hit(c,"password:global",900,600,now),
                     self.hit(c,"password:browser:"+browser,900,30,now),
                     self.hit(c,"password:email:"+email,900,5,now)]
            row=c.execute("SELECT * FROM credentials WHERE email=?",(email,)).fetchone()
            # 없는 계정도 같은 해시 검사를 수행하여 계정 유무의 응답시간 차이를 줄입니다.
            saved=dict(row) if row else None
        if not all(allowed):return None
        hash_=saved["password_hash"] if saved else self.dummy_password_hash
        valid=check_password_hash(hash_,password)
        if not valid or not saved or saved["employee"]!=employee:return None
        token=secrets.token_urlsafe(48)
        with self.db() as c:
            current=c.execute("SELECT * FROM credentials WHERE email=?",(email,)).fetchone()
            ar=c.execute("SELECT active FROM access WHERE email=?",(email,)).fetchone()
            if not current or current["password_hash"]!=saved["password_hash"] or (ar and not ar["active"]):
                return None
            c.execute("INSERT INTO sessions VALUES (?,?,?,?)",(self.digest(token),email,employee,now+SESSION_LIFETIME_SECONDS))
            c.execute("INSERT INTO audit VALUES (?,?,?,?,?)",(stamp(),email,"login",email,""))
        return token

    def save_rule(self, actor, email, role, scopes, active, admin, expected_revision):
        with self.db() as c:
            ar = c.execute("SELECT * FROM access WHERE email=?", (actor,)).fetchone()
            if not ar or not ar["active"] or not ar["admin"]:
                raise PermissionError("관리 권한이 없습니다")
            if role not in ROLE_LABELS or not email_address(email):
                raise ValueError("권한과 이메일을 확인하세요.")
            if email == self.owner and (not active or not admin):
                raise ValueError("최초 관리자는 사용 중지하거나 관리자 권한을 해제할 수 없습니다.")
            old = c.execute("SELECT * FROM access WHERE email=?", (email,)).fetchone()
            actual = old["revision"] if old else 0
            if actual != expected_revision:
                raise ValueError("다른 관리자가 먼저 수정했습니다. 대상을 다시 선택한 뒤 저장하세요.")
            others = c.execute("SELECT COUNT(*) FROM access WHERE admin=1 AND active=1 AND email<>?", (email,)).fetchone()[0]
            if admin and active and others >= 3:
                raise ValueError("관리자는 최초 관리자 포함 최대 3명입니다.")
            c.execute("INSERT OR REPLACE INTO access VALUES (?,?,?,?,?,?,?)",
                      (email, role, pack(sorted(set(scopes))), int(active), int(admin), actual + 1, stamp()))
            c.execute("DELETE FROM sessions WHERE email=?", (email,))
            c.execute("DELETE FROM otp WHERE email=?", (email,))
            c.execute("DELETE FROM enrollment WHERE email=?", (email,))
            c.execute("INSERT INTO audit VALUES (?,?,?,?,?)", (stamp(), actor, "access_update", email,
                      pack({"role": role, "scopes": scopes, "active": active, "admin": admin})))

    def list_rules(self):
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM access ORDER BY email")]

    def recent_audit(self):
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT ts,actor,action,target FROM audit ORDER BY rowid DESC LIMIT 40")]


# =====================================================================
# 3. Dataiku 자료 읽기: 완료된 동일 실행의 표만 한 묶음으로 읽습니다.
# =====================================================================
def dataset_rows(name):
    ds = dataiku.Dataset(name)
    return list(ds.iter_rows())


def marker(folder, filename):
    with dataiku.Folder(folder).get_download_stream(filename) as s:
        value = json.loads(s.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("완료 기록 형식을 확인하세요.")
    return value


class Repository:
    def __init__(self):
        self.lock = threading.RLock()
        self.cached = None
        self.loaded = 0

    def get(self, force=False):
        with self.lock:
            if self.cached and not force and time.time() - self.loaded < DATA_CACHE_SECONDS:
                return self.cached
            # 새 자료를 읽지 못하면 기존 직원 명부로 계속 서비스하지 않습니다.
            self.cached = None
            before = marker(TEAM_CACHE_FOLDER, "latest_success.json")
            attempt = marker(TEAM_CACHE_FOLDER, "latest_attempt.json")
            rid = txt(before.get("aggregation_run_id"))
            if not rid or before.get("status") != "completed" or attempt != before:
                raise ValueError("팀 집계가 진행 중이거나 완료되지 않았습니다.")
            extract = marker(EXTRACT_CACHE_FOLDER, "latest_success.json")
            source_id = txt(before.get("source_run_id"))
            if extract.get("run_id") != source_id:
                raise ValueError("PDF 추출 후 팀 집계를 다시 실행하세요.")
            tables = {n: dataset_rows(n) for n in (SUMMARY_DATASET, MEMBERS_DATASET, AI_INPUT_DATASET)}
            for n, rows in tables.items():
                if len(rows) != before.get("row_counts", {}).get(n):
                    raise ValueError("집계 표의 행 수와 완료 기록이 다릅니다.")
                if any(txt(r.get("aggregation_run_id")) != rid or txt(r.get("source_run_id")) != source_id for r in rows):
                    raise ValueError("서로 다른 실행의 집계가 섞였습니다.")
            people = dataset_rows(PEOPLE_DATASET)
            if len(people) != extract.get("명부행수") or any(txt(r.get("run_id")) != source_id for r in people):
                raise ValueError("개인 결과와 팀 집계의 기준이 다릅니다.")
            # AI가 아직 없어도 수치/맵/명단 조회는 가능합니다.
            ai_problem = ""
            try:
                ai_rows = dataset_rows(AI_REPORT_DATASET)
            except Exception as e:
                ai_rows = []
                ai_problem = "AI 보고서가 아직 준비되지 않았습니다."
                fail_log("ai_read", e)
            if marker(TEAM_CACHE_FOLDER, "latest_success.json") != before or marker(TEAM_CACHE_FOLDER, "latest_attempt.json") != before:
                raise ValueError("자료가 갱신 중입니다. 잠시 후 다시 접속하세요.")
            if marker(EXTRACT_CACHE_FOLDER, "latest_success.json") != extract:
                raise ValueError("개인 결과가 갱신 중입니다.")
            snap = self.index(people, tables, ai_rows, before, ai_problem)
            self.cached, self.loaded = snap, time.time()
            return snap

    @staticmethod
    def index(people, tables, ai_rows, meta, ai_problem=""):
        by_id, emails = {}, collections.defaultdict(list)
        for p in people:
            eid = txt(p.get("employee_id"))
            if not eid or eid in by_id:
                raise ValueError("명부의 사번이 비어 있거나 중복됩니다.")
            by_id[eid] = p
            email = email_address(p.get("email_normalized") or p.get("이메일주소"))
            if email:
                emails[email].append(p)
        login_people = {e: rows[0] for e, rows in emails.items() if len(rows) == 1 and
                        txt(rows[0].get("재직상태")) in {"재직", "휴직"} and
                        txt(rows[0].get("직종명")) in {"사무직", "기술직", "연구직"} and
                        flag(rows[0].get("identity_ok"))}
        orgs = {}
        members = collections.defaultdict(list)
        for o in tables[SUMMARY_DATASET]:
            if o["org_key"] in orgs:
                raise ValueError("조직 키가 중복됩니다.")
            orgs[o["org_key"]] = o
        seen = set()
        for m in tables[MEMBERS_DATASET]:
            key = (m["org_key"], txt(m.get("employee_id")))
            if key in seen or key[0] not in orgs or key[1] not in by_id:
                raise ValueError("구성원 연결을 확인하세요.")
            seen.add(key)
            members[key[0]].append(m)
        for k, o in orgs.items():
            if len(members[k]) != int(o["member_count"]):
                raise ValueError("조직 인원수와 구성원 표가 다릅니다.")
            members[k].sort(key=lambda m: (txt(m.get("성명")), txt(m.get("employee_id"))))
        inputs = {}
        for r in tables[AI_INPUT_DATASET]:
            key = (r["org_key"], r["audience"])
            if key in inputs:
                raise ValueError("AI 입력 조직/독자가 중복됩니다.")
            inputs[key] = r
        responses = collections.defaultdict(list)
        for r in ai_rows:
            responses[(txt(r.get("org_key")), txt(r.get("audience")), txt(r.get("input_hash")))].append(r)
        return dict(people=by_id, login=login_people, orgs=orgs, members=members,
                    inputs=inputs, responses=responses, meta=meta, ai_problem=ai_problem)


# =====================================================================
# 4. 접근 범위: 브라우저에서 보낸 직책/이메일/조직값을 신뢰하지 않습니다.
# =====================================================================
def principal(snap):
    if AUTH_MODE == "preview":
        # 브라우저 입력을 받지 않고 설정한 최초 관리자의 화면을 바로 엽니다.
        # 실제 접속자를 인증하는 기능이 아니므로 DSS에서 비공개로 사용해야 합니다.
        email = email_address(FIRST_ADMIN_EMAIL)
        person = snap["login"].get(email)
        if not person:
            return None
        return {"email": email, "eid": txt(person.get("employee_id")),
                "person": person,
                "rule": {"role": "hr", "admin": True, "active": True, "scopes": "[]"}}
    s = ACCESS.lookup_session(session.get("birkman_sid"))
    if not s:
        return None
    person = snap["login"].get(s["email"])
    if not person or txt(person.get("employee_id")) != s["employee"]:
        return None
    rule = ACCESS.rule(s["email"])
    if not rule["active"]:
        return None
    return {"email": s["email"], "eid": s["employee"], "person": person, "rule": rule}


def own_orgs(p, snap):
    return {k for k, rows in snap["members"].items() if any(txt(r["employee_id"]) == p["eid"] for r in rows)}


def leader_scopes(p, snap):
    role = p["rule"]["role"]
    if role == "hr":
        return set(snap["orgs"])
    scopes = js(p["rule"]["scopes"], [])
    keys = set()
    for k in scopes:
        org = snap["orgs"].get(k)
        if not org:
            continue
        if role == "team_leader" and org["org_level"] == "team":
            keys.add(k)
            keys.update(x for x, o in snap["orgs"].items() if o.get("parent_team_key") == k)
        elif role == "part_leader" and org["org_level"] == "part":
            keys.add(k)
    return keys


def visible_orgs(p, snap):
    return own_orgs(p, snap) | leader_scopes(p, snap)


def selectable_report_orgs(p,snap):
    """명부·집계는 보존하고 조직 선택 목록에서만 그룹장 단독 조직을 숨깁니다."""
    keys=visible_orgs(p,snap)
    if SHOW_LEADER_ONLY_GROUPS:return keys
    leader_titles={"팀장","파트장","그룹장","실장","센터장","본부장","사장","부사장","공장장","TFT장"}
    return {key for key in keys if snap["orgs"][key].get("org_level")!="group_direct" or
            any(txt(r.get("직책명")) not in leader_titles for r in snap["members"].get(key,[]))}


def can_detail(p, snap, org):
    return org in leader_scopes(p, snap)


def allowed_person(p, snap, eid):
    if eid == p["eid"]:
        return True
    return any(any(txt(m["employee_id"]) == eid for m in snap["members"][k]) for k in leader_scopes(p, snap))


# =====================================================================
# 5. AI 응답: 현재 input_hash와 대상 독자가 일치하는 JSON만 표시
# =====================================================================
def collect_evidence(v, result):
    if isinstance(v, dict):
        if isinstance(v.get("evidence_id"), str):
            result[v["evidence_id"]] = v
        for a in v.values():
            collect_evidence(a, result)
    elif isinstance(v, list):
        for a in v:
            collect_evidence(a, result)


def validate_report(value, evidence, audience):
    required = {"headline", "overview", "work_patterns", "collaboration", "leader_actions", "discussion_questions", "coverage_note"}
    if not isinstance(value, dict) or set(value) != required:
        return False
    for k, limit in (("headline", 200), ("overview", 3000), ("coverage_note", 1000)):
        if not isinstance(value[k], str) or len(value[k]) > limit:
            return False
    specs = {"work_patterns": ("title", "observation", "action"),
             "collaboration": ("situation", "suggestion"), "leader_actions": ("situation", "action")}
    for section, fields in specs.items():
        items = value[section]
        if not isinstance(items, list) or len(items) > 3:
            return False
        for item in items:
            if not isinstance(item, dict) or set(item) != set(fields) | {"basis"}:
                return False
            if any(not isinstance(item[k], str) or len(item[k]) > 3000 for k in fields):
                return False
            basis = item["basis"]
            if not isinstance(basis, list) or not basis or len(basis) > 20 or any(not isinstance(k, str) or k not in evidence for k in basis):
                return False
    questions = value["discussion_questions"]
    if not isinstance(questions, list) or len(questions) > 2 or any(not isinstance(v, str) or len(v) > 1000 for v in questions):
        return False
    if audience == "shared" and value["leader_actions"]:
        return False
    return True


def report_for(snap, org, audience):
    inp = snap["inputs"].get((org, audience))
    if not inp:
        return None, {}, "이 조직은 AI 설명을 만들 진단 자료가 없습니다."
    rows = snap["responses"].get((org, audience, inp["input_hash"]), [])
    if len(rows) != 1:
        return None, {}, "AI 설명이 아직 없거나 현재 집계와 일치하지 않습니다."
    row = rows[0]
    if txt(row.get("llm_error_message")) or txt(row.get("prompt_version")) != txt(inp.get("prompt_version")):
        return None, {}, "AI 설명을 확인 중입니다. 수치와 맵은 아래에서 확인할 수 있습니다."
    status = txt(row.get("llm_validation_status")).lower()
    if any(x in status for x in ("invalid", "fail", "error")):
        return None, {}, "AI 응답 형식 확인이 필요합니다."
    raw = txt(row.get("llm_output"))
    if len(raw) > 60000:
        return None, {}, "AI 응답 길이 확인이 필요합니다."
    # fenced JSON은 JSON 본문만 분리. 앞뒤 설명문/복수 객체는 허용하지 않습니다.
    if raw.startswith("```") and raw.endswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)[:-3].strip()
    try:
        def unique_pairs(pairs):
            obj = {}
            for k, v in pairs:
                if k in obj:
                    raise ValueError("duplicate key")
                obj[k] = v
            return obj
        value = json.loads(raw, object_pairs_hook=unique_pairs,
                           parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (ValueError, TypeError):
        return None, {}, "AI 응답 형식 확인이 필요합니다."
    evidence = {}
    collect_evidence(js(inp.get("input_json"), {}), evidence)
    if not validate_report(value, evidence, audience):
        return None, {}, "AI 설명의 항목 또는 근거 연결을 확인 중입니다."
    return value, evidence, ""


# =====================================================================
# 6. 화면 구성 도구: 외부 CDN/폰트/스크립트 연결 없음
# =====================================================================
# 보고서 UI: SVG 맵은 viewBox와 정사각형 크기로 화면·인쇄 비율을 고정합니다.
# 회사 자료는 기존 검증·권한 함수를 거쳐 표시하며, 외부 리소스를 호출하지 않습니다.
CSS = r"""
:root{--ink:#202d38;--muted:#6c7b87;--line:#dbe2e7;--green:#234c49;--bg:#eef1f4}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:var(--ink);background:var(--bg);font:14px/1.65 'Malgun Gothic','Apple SD Gothic Neo',sans-serif}h1,h2,h3,p{margin-top:0}h1{font-size:32px;line-height:1.25;letter-spacing:-1px}h2{font-size:24px;line-height:1.35;letter-spacing:-.7px;margin-bottom:10px}h3{font-size:15px;margin:18px 0 7px}button,input,select{font:inherit}button{cursor:pointer;background:white;border:1px solid var(--line);border-radius:5px;padding:9px 14px;color:var(--ink)}button:hover{border-color:#638482}button.primary{background:var(--green);color:#fff}button:focus-visible,a:focus-visible,input:focus-visible{outline:3px solid #54a9ad;outline-offset:2px}input{padding:10px;border:1px solid var(--line);border-radius:4px;max-width:100%}.muted,.small{color:var(--muted)}.small{font-size:12px}.shell{max-width:1600px;margin:auto;display:grid;grid-template-columns:204px minmax(0,1fr);min-height:100vh}.sidebar{padding:30px 20px;background:#fff;border-right:1px solid var(--line);position:sticky;top:0;height:100vh}.wordmark{font-size:11px;letter-spacing:2px;color:var(--muted);font-weight:bold}.sidebar h2{font-size:19px;margin:10px 0 28px}.nav label{display:flex;align-items:center;gap:8px;padding:11px 9px;border-radius:5px;margin-bottom:4px;cursor:pointer;font-weight:600}.nav label:has(input:checked){background:#eaf1f0;color:#1c5852}.nav input{accent-color:var(--green);margin:0}.user{border-top:1px solid var(--line);margin-top:28px;padding-top:18px;font-size:13px}.user button{margin-top:9px}.main{min-width:0;padding:24px 30px 50px}.toolbar{display:flex;align-items:end;gap:20px;max-width:960px;margin:0 auto 20px}.toolbar .org{flex:1;min-width:0}.label{display:block;font-size:12px;font-weight:bold;margin-bottom:5px}.actions{display:flex;gap:8px;flex-wrap:wrap}.toolbar button{font-size:12px;white-space:nowrap}.report{max-width:960px;margin:0 auto;counter-reset:sheet}.report-sheet{background:#fff;padding:46px 50px 28px;margin-bottom:22px;border:1px solid var(--line);min-width:0;position:relative;counter-increment:sheet;scroll-margin-top:16px}.sheet-kicker{display:flex;justify-content:space-between;font-size:10px;letter-spacing:1px;color:var(--muted);margin-bottom:28px}.section-no{display:inline-block;color:#57787a;font-size:12px;letter-spacing:1.5px;margin-bottom:9px}.sheet-intro{font-size:13px;color:#4e5e6b;margin-bottom:16px;max-width:760px}.read-tip{background:#f2f5f7;padding:11px 14px;font-size:12px;margin:15px 0 23px;border-radius:3px}.read-tip strong{margin-right:9px}.sheet-footer{display:flex;justify-content:space-between;border-top:1px solid var(--line);padding-top:10px;margin-top:26px;color:var(--muted);font-size:10px}.sheet-footer .page-number:after{content:counter(sheet)}.cover h1{font-size:42px;margin:15px 0 12px}.cover-path{font-size:12px;color:#617180}.cover-meta{font-size:13px;margin-bottom:24px}.cover-meta b{color:#253f46}.cover-lead{font-size:19px;font-weight:600;line-height:1.8;letter-spacing:-.4px;margin:24px 0}.metrics{display:grid;grid-template-columns:repeat(4,1fr);border-top:2px solid #263746;border-bottom:1px solid var(--line);margin:26px 0}.metric{padding:14px 13px;border-right:1px solid var(--line);min-width:0}.metric:first-child{padding-left:0}.metric:last-child{border-right:0}.metric span{display:block;font-size:11px;line-height:1.5;color:#64727e;min-height:34px}.metric strong{display:block;font-size:19px;line-height:1.5;margin-top:9px;word-break:keep-all}.metric small{font-size:10px;color:var(--muted)}.cover-bottom{display:grid;grid-template-columns:1fr 1fr;gap:32px;margin-top:28px}.toc a{display:flex;gap:12px;text-decoration:none;color:var(--ink);padding:10px 0;border-bottom:1px solid var(--line);font-size:12px}.toc b{color:#6c8485;min-width:20px}.guide-colors{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}.guide-color{border-top:4px solid;padding-top:10px;font-size:12px}.guide-color strong{display:block;margin-bottom:5px}.symbols{display:grid;grid-template-columns:1fr 1fr;gap:13px 24px;font-size:12px}.symbols b{font-size:13px;display:block}.symbols .symbol{font-size:22px;float:left;width:31px;line-height:1.2}.map-grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(190px,1fr);gap:28px;align-items:start;margin:18px 0}.map-visual{width:100%;min-width:0}.birkman-map{display:block;width:100%;height:auto;aspect-ratio:1/1;max-width:520px;margin:0 auto;object-fit:contain}.map-caption{font-size:10px;color:var(--muted);text-align:center;margin-top:7px}.color-row{padding:10px 0;border-bottom:1px solid var(--line);font-size:11px}.color-heading{display:flex;align-items:center;gap:7px}.color-heading strong{margin-left:auto;white-space:nowrap;font-size:16px}.swatch{display:inline-block;width:9px;height:9px;flex-shrink:0;border-radius:1px;margin-right:4px}.color-desc{color:var(--muted);font-size:10px;margin:3px 0 4px 16px}.color-names{font-size:10px;line-height:1.8;margin-left:16px}.color-summary h3{margin-top:8px;border-bottom:2px solid #293b47;padding-bottom:9px}.map-pair{display:grid;grid-template-columns:1fr 1fr;gap:26px;align-items:start}.map-pair h3{margin-top:0}.map-pair .birkman-map{max-width:350px}.number-key{display:grid;grid-template-columns:repeat(3,1fr);gap:3px 12px;font-size:10px;margin-top:13px}.number-key b{color:#547c80;margin-right:5px}.twocol{display:grid;grid-template-columns:1fr 1fr;gap:26px}.tight-list{padding-left:18px;font-size:13px}.tight-list li{margin-bottom:9px}.table-wrap{overflow-x:auto;max-width:100%}table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;padding:9px 7px;border-bottom:1px solid var(--line);vertical-align:top}th{background:#f5f7f8;font-size:11px;white-space:nowrap}thead{display:table-header-group}.score-table{table-layout:fixed;font-size:11px}.score-table th,.score-table td{text-align:center;padding:5px 2px;border:2px solid white}.score-table .name-cell{width:80px;text-align:left;font-size:11px;font-weight:600}.score-table th{font-size:10px;white-space:normal}.score-table .top-score{box-shadow:inset 0 0 0 1.5px #273e4b;font-weight:bold}.score-table tfoot td{background:#f1f4f6;border-top:2px solid #30424f;font-weight:bold}.score-table .reference-row td{font-weight:normal;color:#657480}.compare-row{margin:22px 0}.compare-row h3{margin:0 0 9px}.stack-row{display:flex;gap:12px;align-items:center;font-size:11px;margin-bottom:8px}.stack-label{width:67px;flex-shrink:0}.stack{display:flex;flex:1;height:27px;background:#edf0f3;overflow:hidden}.stack.slim{height:8px}.stack span{height:100%;min-width:0;display:flex;justify-content:center;align-items:center;color:white;font-size:10px;white-space:nowrap}.stack-count{width:39px;text-align:right;flex-shrink:0;color:var(--muted)}.stack-legend{font-size:10px;display:flex;gap:12px;flex-wrap:wrap}.orientation-row{display:grid;grid-template-columns:88px 1fr 75px;gap:12px;align-items:center;font-size:12px;margin:13px 0}.orientation-track{height:22px;background:#f1f4f7}.orientation-fill{height:100%}.orientation-number{text-align:right;font-weight:bold}.coach{padding:17px 0;border-bottom:1px solid var(--line)}.coach h3{margin:0 0 7px;font-size:16px}.coach p{font-size:13px;margin-bottom:8px}.action{background:#edf4f3;padding:11px 14px;font-size:13px;border-left:3px solid #588c85;margin-top:9px}.evidence{font-size:11px;color:var(--muted);margin-top:7px}.evidence summary{cursor:pointer}.profile-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.profile{border:1px solid var(--line);padding:15px;break-inside:avoid}.profile-head{display:flex;justify-content:space-between;gap:12px;align-items:baseline;border-bottom:1px solid var(--line);padding-bottom:8px}.profile-head strong{font-size:16px}.profile-head span{font-size:10px;color:var(--muted)}.profile-map{display:grid;grid-template-columns:78px 1fr;gap:9px;align-items:center;margin:10px 0}.profile-map .birkman-map{width:78px;height:78px}.profile-colors{font-size:11px}.profile-colors span{white-space:nowrap}.profile-colors div{display:flex;justify-content:space-between;gap:6px}.profile p{font-size:11px;margin:6px 0;line-height:1.65}.profile b{margin-right:5px}.source-text{border-top:1px solid var(--line);padding-top:7px}.profile-status{background:#f6f7f9;padding:9px;font-size:11px}.empty{background:#f5f7f8;padding:16px;color:#6a7781;font-size:13px}.notice{font-size:12px;color:var(--muted);padding:10px 0}.footer{font-size:10px;color:var(--muted);max-width:960px;margin:15px auto}.page-head{display:flex;justify-content:space-between;gap:20px;margin-bottom:20px}.tag{font-size:11px;color:#537579}.card{background:white;border:1px solid var(--line);padding:24px;margin:18px 0}.card h2{font-size:20px}.admin-form{display:grid;grid-template-columns:1fr 1fr;gap:18px}.wide{grid-column:1/-1}.admin-note{font-size:12px}.login{background:white;max-width:460px;margin:10vh auto;padding:40px;border:1px solid var(--line)}.login input{width:100%;margin-bottom:12px}.login .actions{margin:10px 0 22px}.login h1{font-size:26px}.print-only{display:none}#map-controls{display:none!important}#person-control{max-width:960px;margin:0 auto 20px}.select-detail{font-size:11px;color:var(--muted)}
@media(max-width:1100px){.main{padding:20px 18px}.report-sheet{padding:35px 32px 23px}.map-grid{gap:16px}.metric strong{font-size:17px}.shell{grid-template-columns:180px minmax(0,1fr)}}
@media(max-width:760px){.shell{display:block}.sidebar{position:static;height:auto;border-right:0;padding:15px 18px}.sidebar h2{margin:5px 0 10px}.nav{display:flex;flex-wrap:wrap}.nav label{padding:7px 10px;font-size:12px}.user{display:none}.main{padding:16px 10px}.toolbar{flex-wrap:wrap;gap:10px}.toolbar .org{flex-basis:100%}.report-sheet{padding:25px 20px 20px;margin-bottom:14px}.cover h1{font-size:34px}.cover-lead{font-size:16px}.metrics{grid-template-columns:1fr 1fr}.metric{padding:12px!important}.metric:nth-child(2){border-right:0}.metric:nth-child(-n+2){border-bottom:1px solid var(--line)}.metric strong{font-size:17px}.cover-bottom,.map-grid,.map-pair,.twocol{grid-template-columns:1fr;gap:18px}.guide-colors{grid-template-columns:1fr 1fr}.profile-grid{grid-template-columns:1fr}.symbols{grid-template-columns:1fr}.map-pair .birkman-map{max-width:450px}.score-table{min-width:590px}.number-key{grid-template-columns:1fr 1fr}.sheet-kicker{margin-bottom:18px}.admin-form{grid-template-columns:1fr}.login{margin:5vh 14px;padding:28px}.report-sheet h2{font-size:23px}.sheet-footer{font-size:9px}}
/* A4 원판 하나를 화면에서는 통째로 축소하고 인쇄에서는 1:1로 사용합니다. */
.report{max-width:960px;position:relative;overflow-x:auto;counter-reset:none}
.report-source{display:none!important}.report-tools{display:flex;justify-content:flex-end;margin-bottom:10px}.report-zoom{font-size:11px}
.report-preview{display:flex;flex-direction:column;align-items:center;gap:22px;padding-bottom:8px}.report[data-zoom=actual] .report-preview{align-items:flex-start}.report-preview.measuring{visibility:hidden;position:absolute;left:0;top:0}.report-page-wrap{position:relative;flex:none;scroll-margin-top:12px}
.report-preview .report-sheet{box-sizing:border-box;width:210mm!important;height:297mm!important;min-height:297mm!important;max-height:297mm!important;padding:14mm 14mm 11mm!important;margin:0!important;border:0!important;background:white;box-shadow:0 1px 8px #273d4820;display:flex;flex-direction:column;transform-origin:top left;position:relative;overflow:visible;font:10pt/1.6 'Malgun Gothic','Apple SD Gothic Neo',sans-serif;counter-increment:none}
.report-sheet .sheet-content{flex:1;min-height:0;display:flex;flex-direction:column}.report-sheet .report-heading{flex:none}.report-sheet .report-body{flex:1;min-height:0;display:block}.report-sheet .sheet-footer{flex:none;display:flex;justify-content:space-between;border-top:1px solid #dbe2e7;margin-top:5mm;padding-top:2.5mm;font-size:8pt;line-height:1.4}.report-sheet .page-number:after{content:none!important}
.report-sheet h1{font-size:30pt;line-height:1.25}.report-sheet h2{font-size:20pt;line-height:1.4;margin:0 0 3mm}.report-sheet h3{font-size:12pt;line-height:1.5;margin:4mm 0 2mm}.report-sheet p{margin:0 0 3mm}.report-sheet .sheet-intro{font-size:9pt;color:#536673;margin-bottom:4mm}.report-sheet .section-no{font-size:8pt;letter-spacing:1.2px;margin-bottom:2mm}.report-sheet .read-tip{font-size:9pt;line-height:1.6;padding:3mm;margin:3mm 0 5mm}.report-sheet .small{font-size:8.5pt}.report-sheet .sheet-kicker{font-size:8pt;margin-bottom:8mm}.report-sheet.cover h1{font-size:34pt;margin:4mm 0}.report-sheet .cover-path{font-size:9pt}.report-sheet .cover-meta{font-size:10pt;margin-bottom:5mm}.report-sheet .cover-lead{font-size:13pt;line-height:1.8;margin:5mm 0}.report-sheet .metrics{grid-template-columns:repeat(4,1fr);margin:5mm 0}.report-sheet .metric{padding:3mm!important;border-right:1px solid var(--line)!important;border-bottom:0!important}.report-sheet .metric:last-child{border-right:0!important}.report-sheet .metric span{font-size:8pt;min-height:30px}.report-sheet .metric strong{font-size:13pt;line-height:1.5}.report-sheet .metric small{font-size:7.5pt}.report-sheet .cover-bottom{display:grid;grid-template-columns:1fr 1fr;gap:7mm;margin-top:5mm}.report-sheet .toc a{font-size:9pt;line-height:1.6;padding:2.4mm 0;justify-content:space-between}.report-sheet .toc b{min-width:0}.report-sheet .toc-page{font-weight:bold;color:#587c80;margin-left:5mm}.report-sheet .cover-actions{padding-left:4mm;font-size:9pt}.report-sheet .cover-actions li{margin-bottom:3mm}
.report-sheet .map-grid{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:6mm;align-items:start;margin:4mm 0}.report-sheet .birkman-map{display:block;width:100%;height:auto!important;aspect-ratio:1/1;max-width:100%;object-fit:contain}.report-sheet .map-caption{font-size:8pt;margin-top:2mm;line-height:1.5}.report-sheet .color-row{padding:2.5mm 0}.report-sheet .color-heading{font-size:9pt}.report-sheet .color-heading strong{font-size:12pt}.report-sheet .color-desc,.report-sheet .color-names{font-size:8pt;line-height:1.7}.report-sheet .color-summary h3{font-size:11pt;margin-top:0}.report-sheet .number-key{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1mm 3mm;font-size:8.5pt;margin-top:3mm}.report-sheet .twocol{display:grid;grid-template-columns:1fr 1fr;gap:5mm}.report-sheet .symbols{display:grid;grid-template-columns:1fr 1fr;gap:4mm 5mm;font-size:9pt}.report-sheet .symbols b{font-size:10pt}.report-sheet .symbol{font-size:18pt}.report-sheet .guide-colors{grid-template-columns:repeat(4,1fr)}
.report-sheet .table-wrap{overflow:visible;max-width:100%}.report-sheet table{font-size:9pt;line-height:1.6}.report-sheet th{font-size:8.5pt}.report-sheet td,.report-sheet th{padding:2.5mm 2mm}.report-sheet .score-table{min-width:0;table-layout:fixed;font-size:9pt}.report-sheet .score-table td{padding:2mm .6mm}.report-sheet .score-table th{font-size:8pt;padding:2mm .5mm}.report-sheet .score-table .name-cell{width:23mm;font-size:9pt}.report-sheet .score-table tfoot td{font-size:8.5pt}
.report-sheet .compare-row{margin:4mm 0}.report-sheet .compare-row h3{margin:0 0 2mm}.report-sheet .stack-row{font-size:8.5pt;margin:0 0 2mm;gap:3mm}.report-sheet .stack-label{width:20mm}.report-sheet .stack{height:6mm}.report-sheet .stack.slim{height:2mm}.report-sheet .stack span{font-size:8pt}.report-sheet .stack-legend{font-size:8pt;gap:3mm}.report-sheet .orientation-row{grid-template-columns:26mm 1fr 19mm;gap:3mm;font-size:9pt;margin:2.5mm 0}.report-sheet .orientation-track{height:5mm}
.report-sheet .coach{padding:3mm 0}.report-sheet .coach h3{font-size:12pt;margin:0 0 2mm}.report-sheet .coach p,.report-sheet .action{font-size:10pt;line-height:1.7}.report-sheet .action{padding:3mm;margin-top:2mm}.report-sheet .evidence{display:none!important}.report-sheet .tight-list{font-size:10pt;padding-left:5mm}.report-sheet .tight-list li{margin-bottom:2.5mm}
.report-sheet .profile-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4mm}.report-sheet .profile{padding:3mm;border:1px solid var(--line)}.report-sheet .profile-head{gap:3mm;padding-bottom:2mm}.report-sheet .profile-head strong{font-size:12pt}.report-sheet .profile-head span{font-size:8pt}.report-sheet .profile-map{grid-template-columns:27mm 1fr;gap:3mm;margin:3mm 0}.report-sheet .profile-map .birkman-map{width:27mm;height:27mm!important}.report-sheet .profile-colors{font-size:9pt}.report-sheet .profile p{font-size:9pt;line-height:1.65;margin:1.5mm 0}.report-sheet .profile-status{font-size:9pt}.report-sheet .source-text{padding-top:2mm}.report-sheet .empty{font-size:9pt;padding:4mm}.report-sheet .source-pair{padding:3mm;background:#f5f7f9}.report-sheet .source-pair h3{margin-top:0}.report-sheet .source-pair ul{padding-left:4mm;font-size:9pt;margin-bottom:0}.report-sheet .source-pair li{margin-bottom:2mm}
@media print{@page{size:A4 portrait;margin:0}html,body{margin:0!important;padding:0!important;background:white!important;-webkit-print-color-adjust:exact;print-color-adjust:exact}.sidebar,.toolbar,.no-print,#person-control,#map-controls,#admin-panel,.report-tools{display:none!important}.shell,.main,#page{display:block!important;width:auto!important;max-width:none!important;padding:0!important;margin:0!important}.report{max-width:none!important;overflow:visible!important;margin:0!important;padding:0!important}.report-source{display:none!important}.report-preview{display:block!important;margin:0!important;padding:0!important}.report-page-wrap{display:block!important;width:210mm!important;height:297mm!important;margin:0!important;padding:0!important;break-after:page;page-break-after:always}.report-page-wrap:last-child{break-after:auto;page-break-after:auto}.report-preview .report-sheet{transform:none!important;box-shadow:none!important;break-before:auto!important;page-break-before:auto!important}.report-preview.measuring{display:none!important}}

"""

COLOR_TEXT = {"빨강":("실행", "일과 결과에 초점을 두고 행동하기"),
              "초록":("소통", "사람들과 이야기하고 설득하기"),
              "노랑":("체계", "기준을 세우고 세부사항 확인하기"),
              "파랑":("구상", "아이디어를 만들고 깊이 생각하기"),
              "중앙선":("중앙선", "색의 경계에 위치")}
MAP_TITLES = {"interest":"좋아하는 일", "usual":"남에게 보이는 모습", "needs":"바라는 대우", "stress":"지쳤을 때의 반응"}
SYMBOLS = {"interest":"✱", "usual":"◆", "needs":"○", "stress":"□"}
PALETTE.update({"빨강":"#971e2d", "초록":"#24754f", "노랑":"#d37820", "파랑":"#305faa"})


def empty(message):
    return html.Div(message, className="empty")


def card(title, children):
    return html.Section([html.H2(title)] + (children if isinstance(children,list) else [children]),className="card")


def table(columns, rows, cls=""):
    return html.Div(html.Table([html.Thead(html.Tr([html.Th(label) for _,label in columns])),html.Tbody([
        html.Tr([html.Td(txt(row.get(k)) or "—") for k,_ in columns]) for row in rows])]),className="table-wrap "+cls)


def page_header(title, subtitle, badge=""):
    return html.Div([html.Div([html.H1(title),html.P(subtitle,className="muted")]),html.Span(badge,className="tag")],className="page-head")


def map_points(rows, kind):
    return [r for r in rows if flag(r.get("analysis_eligible")) and flag(r.get(kind+"_valid")) and
            num(r.get(kind+"_x")) is not None and num(r.get(kind+"_y")) is not None and
            -1 <= num(r[kind+"_x"]) <= 1 and -1 <= num(r[kind+"_y"]) <= 1]


def color_of(row,kind):
    if not map_points([row],kind):
        return None
    x,y=num(row[kind+"_x"]),num(row[kind+"_y"])
    # 02 집계와 같은 중앙선 기준을 사용합니다.
    if abs(x)<=.002 or abs(y)<=.002:
        return "중앙선"
    return ("빨강" if x<0 else "초록") if y>0 else ("노랑" if x<0 else "파랑")


def map_stats(rows,kind):
    counts=collections.Counter(color_of(r,kind) for r in map_points(rows,kind))
    return {"n":sum(counts.values()),"quadrants":[{"color":c,"count":counts[c]} for c in PALETTE]}


def roster_number(rows):
    return {txt(r.get("employee_id")):i for i,r in enumerate(sorted(rows,key=lambda r:(txt(r.get("성명")),txt(r.get("employee_id")))),1)}


def svg_map(rows, kinds, labels=True, mini=False):
    """좌표를 그대로 둡니다. 겹치는 점은 묶고 라벨만 이동합니다. SVG 전체 비율=1:1."""
    import base64
    if isinstance(kinds,str): kinds=[kinds]
    size=480; start=36; span=408; mid=240
    parts=['<svg xmlns="http://www.w3.org/2000/svg" width="480" height="480" viewBox="0 0 480 480" preserveAspectRatio="xMidYMid meet">',
           '<style>text{font-family:Malgun Gothic,Apple SD Gothic Neo,sans-serif}</style>']
    for x,y,c in [(36,36,"빨강"),(240,36,"초록"),(36,240,"노랑"),(240,240,"파랑")]:
        parts.append('<rect x="%s" y="%s" width="204" height="204" fill="%s"/>'%(x,y,PALETTE[c]))
    for inset,opacity in [(60,.13),(126,.15)]:
        parts.append('<rect x="%s" y="%s" width="%s" height="%s" fill="white" opacity="%s"/>'%(36+inset,36+inset,408-2*inset,408-2*inset,opacity))
    if not mini:
        for x,y,text_,rotate in [(240,22,"외향",0),(240,466,"내향",0),(18,240,"과제 지향",-90),(464,240,"사람 지향",90)]:
            parts.append('<text x="%s" y="%s" text-anchor="middle" dominant-baseline="middle" transform="rotate(%s %s %s)" fill="#33434f" font-size="12" font-weight="600">%s</text>'%(x,y,rotate,x,y,text_))
    if not rows and not mini:
        for x,y,c in [(138,138,"빨강"),(342,138,"초록"),(138,342,"노랑"),(342,342,"파랑")]:
            parts.append('<text x="%s" y="%s" text-anchor="middle" fill="white" font-size="22" font-weight="700">%s</text>'%(x,y,c+" · "+COLOR_TEXT[c][0]))
    groups=collections.defaultdict(list);numbers=roster_number(rows)
    selected_ids=set()
    both = "needs" in kinds and "stress" in kinds
    display_rows, pair_info = paired_map_rows(rows) if both else (rows,{})
    for kind in kinds:
        for r in map_points(display_rows,kind):
            x=mid+num(r[kind+"_x"])*204; y=mid-num(r[kind+"_y"])*204
            groups[(x,y)].append((kind,r));selected_ids.add(txt(r.get("employee_id")))
    # 12명 이하는 이름, 그 이상은 모든 맵에서 같은 번호를 사용합니다.
    named=len(rows)<=12 and (len(kinds)==1 or set(kinds)=={"needs","stress"})
    occupied=[]
    for (x,y),items in sorted(groups.items()):
        for kind in sorted(set(k for k,_ in items),key=lambda k: {"stress":0,"needs":1,"usual":2,"interest":3}[k]):
            if kind=="stress": shape='<rect x="%.2f" y="%.2f" width="19" height="19" fill="white" stroke="#253d4c" stroke-width="1"/>'%(x-9.5,y-9.5)
            elif kind=="needs": shape='<circle cx="%.2f" cy="%.2f" r="6.5" fill="white" stroke="#263f4e" stroke-width="1.7"/>'%(x,y)
            elif kind=="usual": shape='<path d="M %.2f %.2f l 8 8 -8 8 -8 -8 Z" fill="white" stroke="#263f4e" stroke-width=".8"/>'%(x,y-8)
            else:
                shape=''.join('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="white" stroke-width="3"/>'%(x-math.cos(a)*8,y-math.sin(a)*8,x+math.cos(a)*8,y+math.sin(a)*8) for a in [0,math.pi/4,math.pi/2,3*math.pi/4])
            parts.append(('<g transform="translate(%.2f %.2f) scale(2.1) translate(%.2f %.2f)">'%(x,y,-x,-y)+shape+"</g>") if mini else shape)
        if labels and not mini:
            unique={txt(r.get("employee_id")):r for _,r in items}
            label=" · ".join(txt(r.get("성명")) if named else str(numbers[eid]) for eid,r in sorted(unique.items(),key=lambda pair:numbers[pair[0]]))
            # 같은 좌표에 많은 인원이 겹치면 라벨을 줄이고 대응표에 전원을 남깁니다.
            if len(label)>26:
                ids=sorted(numbers[e] for e in unique)
                first_id=min(unique,key=lambda eid:numbers[eid])
                label="%s 외 %s명"%(txt(unique[first_id].get("성명")) if named else ids[0],len(ids)-1)
            width=max(18,sum(11 if ord(c)>127 else 6.3 for c in label)+10);height=20
            candidates=[]
            for radius in [13,27,43,62,86,112,145]:
                for dx,dy in [(radius,-10),(-width-radius,-10),(-width/2,-radius-20),(-width/2,radius)]:
                    lx=min(438-width,max(42,x+dx));ly=min(420,max(42,y+dy))
                    overlaps=sum(max(0,min(lx+width,a+w)-max(lx,a))*max(0,min(ly+height,b+h)-max(ly,b)) for a,b,w,h in occupied)
                    covers=sum(1 for px,py in groups if lx-6<px<lx+width+6 and ly-6<py<ly+height+6)
                    candidates.append((overlaps*50+covers*300+abs(lx-x)+abs(ly-y),lx,ly))
            _,lx,ly=min(candidates);occupied.append((lx,ly,width,height))
            cx=min(lx+width,max(lx,x));cy=min(ly+height,max(ly,y))
            if abs(cx-x)+abs(cy-y)>18:
                parts.append('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="white" stroke-width="1" opacity=".8"/>'%(x,y,cx,cy))
            parts.append('<rect x="%.2f" y="%.2f" width="%.2f" height="20" rx="3" fill="#1f3341" fill-opacity=".9"/><text x="%.2f" y="%.2f" fill="white" font-size="11">%s</text>'%(lx,ly,width,lx+5,ly+14,escaping.escape(label)))
    parts.append('</svg>')
    src='data:image/svg+xml;base64,'+base64.b64encode(''.join(parts).encode()).decode()
    return html.Img(src=src,className="birkman-map",alt=" / ".join(MAP_TITLES[k] for k in kinds)+" 버크만 맵")


def swatch(c):
    return html.Span(className="swatch",style={"background":PALETTE.get(c,"#8a929a")})


def top_color(stats):
    qs=[q for q in stats.get("quadrants",[]) if q["count"]]
    if not qs:return "확인된 좌표 없음"
    mx=max(q["count"] for q in qs)
    return "·".join(q["color"] for q in qs if q["count"]==mx)+" %s명"%mx


def paired_map_rows(rows):
    """같은 맵에 그리기 위한 인원 집계만 수행합니다. 좌표·유효 플래그는 바꾸지 않습니다.
    욕구/스트레스가 다르면 원래 위치에 각각 그립니다. 같을 때만 두 기호가 겹칩니다.
    """
    display=[];info={"paired":0,"needs_only":0,"stress_only":0,"review":0}
    for r in rows:
        v=dict(r);n=bool(map_points([r],"needs"));t=bool(map_points([r],"stress"))
        if n and t:
            identical=(num(r["needs_x"])==num(r["stress_x"]) and
                       num(r["needs_y"])==num(r["stress_y"]))
            info["paired" if identical else "review"]+=1
        elif n:info["needs_only"]+=1
        elif t:info["stress_only"]+=1
        display.append(v)
    return display,info


MAP_COLOR_HINTS={
 "interest":{"빨강":"실제 대상을 다루고 결과를 만드는 활동","초록":"사람을 만나고 의견을 전달하는 활동","노랑":"자료와 절차를 정리하는 활동","파랑":"아이디어를 구상하고 표현하는 활동"},
 "usual":{"빨강":"실행과 결과를 중심으로 접근","초록":"대화와 설득을 중심으로 접근","노랑":"기준과 세부사항을 중심으로 접근","파랑":"생각과 구상을 중심으로 접근"},
 "needs":{"빨강":"직접적이고 분명한 소통을 바라는 쪽","초록":"교류하며 의견을 나누는 환경을 바라는 쪽","노랑":"기준과 예측 가능성을 바라는 쪽","파랑":"생각할 시간과 개별적 접근을 바라는 쪽"}}


def color_summary(rows,kind,show_names=True):
    stats=map_stats(rows,kind);nums=roster_number(rows)
    children=[html.H3(MAPS[kind]+" · 색별 구성")]
    for q in stats["quadrants"]:
        c=q["color"]
        if c=="중앙선" and not q["count"]:continue
        names=[("%s "%nums[txt(r.get("employee_id"))] if len(rows)>12 else "")+txt(r.get("성명")) for r in rows if color_of(r,kind)==c]
        children.append(html.Div([html.Div([swatch(c),html.B(c+" · "+COLOR_TEXT[c][0]),html.Strong("%s명"%q["count"])],className="color-heading"),
            html.Div(MAP_COLOR_HINTS.get(kind,{}).get(c,"색의 경계에 위치"),className="color-desc"),
            html.Div(", ".join(names),className="color-names") if show_names and len(rows)<=36 else None],className="color-row"))
    children.append(html.P("좌표 확인 %s명 / 구성원 %s명"%(stats["n"],len(rows)),className="small",style={"marginTop":"12px"}))
    return html.Div(children,className="color-summary")


def tip(text):
    return html.Div([html.Strong("이렇게 보세요"),text],className="read-tip")


def sheet(title,intro,children,anchor="",section=""):
    return html.Section([html.Div([
        html.Header([html.Span(section,className="section-no") if section else None,
                     html.H2(title) if title else None,
                     html.P(intro,className="sheet-intro") if intro else None],className="report-heading"),
        html.Div(children,className="report-body")],className="sheet-content")],
        className="report-sheet",**{"data-report-anchor":anchor})


def finish_report(sheets,title,snap):
    date=txt(snap["meta"].get("processed_at"))[:10]
    for item in sheets:
        item.children.append(html.Div([html.Span(title+" · "+date),html.Span(className="page-number")],className="sheet-footer"))
    # 펼치는 설명/근거는 고정 A4 밖에 둡니다. 펼쳐도 이미 나눈 페이지가 넘치지 않습니다.
    import copy
    notes=[]
    def gather(node,context=""):
        if isinstance(node,(list,tuple)):
            heading=next((txt(child.children) for child in node if type(child).__name__ in {"H1","H2","H3"} and isinstance(child.children,str)),context)
            for child in node:gather(child,heading)
        elif hasattr(node,"children"):
            if "evidence" in txt(getattr(node,"className","" )).split():
                notes.append(html.Div([html.Strong(context),copy.deepcopy(node)]))
            else:gather(node.children,context)
    for item in sheets:gather(item,title)
    # 원본 구성은 숨기고, 화면과 인쇄에서 동일한 페이지 DOM을 사용합니다.
    tools=[html.Button("원본 크기로 보기",className="report-zoom",type="button")]
    if notes:tools.insert(0,html.Details([html.Summary("설명 전문 · 작성 근거"),html.Div(notes,className="report-explanations")],className="report-notes no-print"))
    return html.Div([html.Div(tools,className="report-tools no-print"),
                     html.Div(sheets,className="report-source",**{"aria-hidden":"true"}),
                     html.Div(className="report-preview")],className="report")


def guide_block():
    colors=html.Div([html.Div([html.Strong(c+" · "+COLOR_TEXT[c][0]),html.P(COLOR_TEXT[c][1],className="small")],className="color-row") for c in ["빨강","초록","노랑","파랑"]])
    return [html.Div([svg_map([],list(MAPS),labels=False),html.Div([html.H3("네 가지 색과 두 방향"),colors,html.P("위·아래는 외향과 내향, 좌우는 과제와 사람을 향하는 방향입니다.",className="small")])],className="map-grid"),
        html.Div([html.Div([html.Span(SYMBOLS[k],className="symbol"),html.B(MAP_TITLES[k]+" · "+MAPS[k]),html.Span(t)]) for k,t in [
        ("interest","마음이 끌리는 활동"),("usual","일이 잘 풀릴 때 드러나는 방식"),("needs","편하게 일하려면 필요한 환경과 대우"),("stress","욕구가 충족되지 않을 때 나타날 수 있는 반응")]],className="symbols"),
        html.Div([html.Strong("원은 바라는 대우, 네모는 욕구가 충족되지 않을 때의 반응.")," 같은 위치라도 뜻은 다릅니다. 평소행동과 욕구를 함께 보며 대화 방식을 생각해 보세요."],className="read-tip")]


def score_table(rows,interest_stats,reference=None,names=True):
    head=html.Thead(html.Tr([html.Th("구성원" if names else "기준",className="name-cell")]+[html.Th(v) for v in INTERESTS.values()]))
    body=[]
    if names:
        for r in rows:
            vals={k:num(r.get("interest_"+k)) if flag(r.get("analysis_eligible")) else None for k in INTERESTS}
            available=sorted((v for v in vals.values() if v is not None),reverse=True)
            threshold=available[min(2,len(available)-1)] if available else None
            cells=[html.Td(txt(r.get("성명")),className="name-cell")]
            for k in INTERESTS:
                v=vals[k];alpha=.06+.58*(v or 0)/100
                cells.append(html.Td("—" if v is None else "%g"%v,style={"background":("rgba(46,76,97,%.3f)"%alpha) if v is not None else "#f5f7f8","color":"white" if v is not None and v>=70 else "#243440"},className="top-score" if v is not None and threshold is not None and v>=threshold else ""))
            body.append(html.Tr(cells))
    foot=[]
    for label,stats,cls in [("우리 조직 평균",interest_stats,""),("전체 진단자 평균",reference,"reference-row")]:
        if stats is None:continue
        foot.append(html.Tr([html.Td(label,className="name-cell")]+[html.Td("—" if num(stats.get(k,{}).get("mean")) is None else "%.0f"%stats[k]["mean"],title="유효 점수 %s명"%stats.get(k,{}).get("n",0)) for k in INTERESTS],className=cls))
    return html.Div(html.Table([head,html.Tbody(body),html.Tfoot(foot)],className="score-table"),className="table-wrap")


def interest_stats_for(rows):
    result={}
    for k in INTERESTS:
        vals=[num(r.get("interest_"+k)) for r in rows if flag(r.get("analysis_eligible")) and num(r.get("interest_"+k)) is not None]
        result[k]={"mean":sum(vals)/len(vals) if vals else None,"n":len(vals)}
    return result


def interest_rank(stats):
    return sorted([k for k in INTERESTS if num(stats.get(k,{}).get("mean")) is not None],key=lambda k:-stats[k]["mean"])


def interest_bars(stats):
    order=interest_rank(stats)
    if not order:return empty("확인된 흥미 점수가 없습니다.")
    return html.Div([html.Div([html.Span(INTERESTS[k]),html.Div(html.Div(className="orientation-fill",style={"width":"%s%%"%stats[k]["mean"],"background":"#527b83"}),className="orientation-track"),html.Span("%.0f"%stats[k]["mean"],className="orientation-number")],className="orientation-row") for k in order])


def stack_bar(stats,label,slim=False):
    n=stats["n"]
    return html.Div([html.Span(label,className="stack-label"),html.Div([
        html.Span(("%s %s"%(q["color"],q["count"])) if q["count"]/n>=.12 and not slim else "",style={"width":"%.5f%%"%(100*q["count"]/n),"background":PALETTE[q["color"]]},title="%s %s명"%(q["color"],q["count"])) for q in stats["quadrants"] if q["count"]] if n else [],className="stack slim" if slim else "stack"),html.Span("%s명"%n,className="stack-count")],className="stack-row")


def brief(text,limit=300):
    text=txt(text)
    if len(text)<=limit:return text
    sentences=re.split(r"(?<=[.!?])\s+|\n+",text)
    picked=[]
    for sentence in sentences:
        if picked and len(" ".join(picked+[sentence]))>limit:break
        if not picked and len(sentence)>limit:
            # 마침표가 없는 긴 원문도 요약·카드 영역을 무한히 늘리지 않습니다.
            cut=sentence.rfind(" ",0,limit)
            cut=cut if cut>=limit*.7 else limit-1
            return sentence[:cut].rstrip()+"…"
        picked.append(sentence)
    return " ".join(picked)


def readable_text(text,cls=""):
    short=brief(text)
    return html.Div([html.P(short,className=cls),html.Details([html.Summary("전체 설명 보기"),html.P(text)],className="evidence no-print") if short!=txt(text) else None])


def original_text(person,field,limit=None):
    vals=js(person.get(field),[])
    vals=[t for t in vals if isinstance(t,str) and txt(t)] if isinstance(vals,list) else []
    return vals[:limit] if limit is not None else vals


def evidence_view(ids,evidence):
    texts=[]
    for k in ids:
        v=evidence.get(k,{})
        texts.append(txt(v.get("text")) or txt(v.get("label")) or "집계 자료")
    return html.Details([html.Summary("작성 근거"),html.Ul([html.Li(t) for t in texts])],className="evidence no-print")


def orientation_content(stats,personal=False):
    if personal:
        vals=stats.get("relative_lengths",{})
        if set(vals)!=set(ORIENTATIONS) or any(num(v) is None or not 0<=num(v)<=1 for v in vals.values()):
            return [empty("이 항목의 결과는 아직 확인되지 않았습니다.")]
        entries=[(ORIENTATIONS[c],c,vals[c],"") for c in ORIENTATIONS]
        note="결과지처럼 네 막대를 가운데 정렬했습니다. 가장 긴 막대를 기준으로 개인 안의 길이 관계를 재현한 것으로, 공식 점수나 서로 반대되는 양방향 척도가 아닙니다. 다른 사람과 길이의 절대값을 비교하지 않습니다."
    else:
        cats=stats.get("categories",[]);n=stats.get("n",0)
        if not n:return [empty("이 항목의 결과는 아직 확인되지 않았습니다.")]
        entries=[(c["label"],c["color"],c["top_count"]/n,"%s명"%c["top_count"]) for c in cats]
        note="팀 막대는 ‘개인 결과에서 가장 긴 분야에 해당하는 인원 / 이 항목 반영 인원’입니다. 가운데 정렬은 표시 방식이며, 공식 팀 조직지향점 점수는 아닙니다. 반영 %s명 · 진단자 중 미확인 %s명 · 최상위 동률 %s명은 각 분야에 포함되어 합계가 반영 인원을 넘을 수 있습니다."%(n,stats.get("missing_count",0),stats.get("tie_count",0))
    descriptions={"파랑":"기획·혁신·아이디어를 중시하는 환경", "초록":"설득·교류·영향력 있는 소통을 중시하는 환경",
                  "빨강":"실행·현실적 접근·결과물을 중시하는 환경", "노랑":"기준·일관성·절차를 중시하는 환경"}
    return [html.P("네 가지 근무환경에서 일하는 사람들과의 유사성을 보여주는 항목입니다. 현재 소속 부서나 실제 담당 직무를 뜻하지 않습니다.",className="small"),
        html.Div([html.Div([html.Span(label),html.Div(html.Div(className="orientation-fill",style={"width":"%.3f%%"%(max(0,min(1,num(v)))*100),"background":PALETTE[c]}),className="orientation-track orientation-centered"),html.Span(count,className="orientation-number")],className="orientation-row") for label,c,v,count in entries]),
        html.P(note,className="small"),html.P("맵과 같은 네 색을 사용하지만 측정하는 항목은 다릅니다. 조직지향점의 색을 맵의 욕구나 평소행동 색으로 바꾸어 읽지 않습니다.",className="small"),
        html.Div([html.Div([html.B([swatch(c),ORIENTATIONS[c]]),html.P(descriptions[c],className="small")],className="orientation-description") for c in ORIENTATIONS],className="orientation-descriptions")]


def profile_card(r,snap):
    person=snap["people"].get(txt(r.get("employee_id")),r)
    eligible=flag(r.get("analysis_eligible"))
    head=html.Div([html.Strong(txt(r.get("성명"))),html.Span(" · ".join(filter(None,[txt(r.get("파트명")),txt(r.get("직책명"))])))],className="profile-head")
    if not eligible:return html.Div([head,html.P("표시할 진단 결과가 없습니다.",className="profile-status")],className="profile")
    values=interest_stats_for([person]);top=interest_rank(values)[:3]
    texts=[html.P([html.B("좋아하는 일")," · ".join(INTERESTS[k] for k in top) or "미확인"])]
    for field,label in [("needs_personal_json","바라는 대우"),("strengths_json","강점")]:
        lines=original_text(person,field,1)
        # 카드의 높이가 원문 길이에 좌우되지 않도록 발췌합니다. 전문은 구성원 상세에 있습니다.
        excerpt=txt(lines[0]) if lines else "이 항목 미확인"
        if len(excerpt)>160:excerpt=excerpt[:159].rstrip()+"…"
        texts.append(html.P([html.B(label),excerpt],className="source-text" if field=="strengths_json" else ""))
    return html.Div([head,html.Div([svg_map([r],list(MAPS),labels=False,mini=True),html.Div([html.Div([html.Span(SYMBOLS[k]+" "+("스트레스" if k=="stress" else MAPS[k])),html.Span([swatch(color_of(r,k)),color_of(r,k) or "미확인"])]) for k in MAPS],className="profile-colors")],className="profile-map")]+texts,className="profile")


def profile_sections(rows,org,snap):
    """팀에서는 팀장, 파트에서는 파트장을 먼저. 나머지는 직책과 관계없이 가나다순."""
    ordered=sorted(rows,key=lambda r:(txt(r.get("성명")),txt(r.get("employee_id"))))
    title={"team":"팀장","part":"파트장"}.get(org.get("org_level"),"")
    leaders=[r for r in ordered if title and txt(r.get("직책명"))==title]
    others=[r for r in ordered if not title or txt(r.get("직책명"))!=title]
    content=[]
    if leaders:
        content += [html.H3(title+" 요약"),html.Div([profile_card(r,snap) for r in leaders],className="profile-grid")]
    if others:
        content += [html.H3("구성원 요약 · 가나다순"),html.Div([profile_card(r,snap) for r in others],className="profile-grid")]
    return content


INTEREST_ACTIVITY_TEXT={
 "numerical":"숫자와 데이터를 계산하고 비교하는 활동",
 "administrative":"자료를 정리하고 절차와 기록을 관리하는 활동",
 "scientific":"원리를 탐구하고 새로운 지식을 알아가는 활동",
 "literary":"글을 읽고 쓰며 언어로 생각을 표현하는 활동",
 "technical":"도구와 장비의 작동 방식을 이해하고 다루는 활동",
 "musical":"음악을 듣고 만들거나 연주하는 활동",
 "artistic":"색과 형태를 활용해 시각적으로 표현하는 활동",
 "outdoor":"야외에서 움직이고 활동하는 일",
 "persuasive":"의견을 전달하고 다른 사람을 설득하는 활동",
 "social_service":"사람을 돕고 지원하는 활동"}


def team_interest_explanation(stats,rank):
    """흥미 점수만 사용합니다. AI의 work_patterns를 흥미/평소행동에 섞지 않습니다."""
    if not rank:return [empty("확인된 흥미 점수가 없습니다.")]
    top=rank[:3]
    return [html.H3("우리 팀이 관심을 보이는 활동"),
        html.P("10개 흥미 분야를 비교했을 때 평균 점수가 높은 항목은 "+" · ".join(INTERESTS[k] for k in top)+"입니다. 아래의 활동을 즐기거나 관심을 갖는 경향을 업무 대화에 참고해 보세요."),
        html.Div([html.P([html.B(INTERESTS[k]+" — "),INTEREST_ACTIVITY_TEXT[k],
                         html.Span(" (평균 %.0f / 100 · %s명 반영)"%(stats[k]["mean"],stats[k]["n"]),className="small")]) for k in top]),
        html.P("이 수치는 관심의 크기이며 능력·성과 점수가 아닙니다. 팀 평균이 높더라도 구성원 모두의 흥미가 같다는 뜻은 아니므로, 실제 업무를 맡길 때는 개인의 관심도 함께 확인하세요.",className="small")]


def team_usual_explanation(rows):
    """평소행동 좌표만 근거로 설명합니다. 흥미 점수·흥미 원문은 참조하지 않습니다."""
    stats=map_stats(rows,"usual")
    qs=[q for q in stats["quadrants"] if q["count"] and q["color"]!="중앙선"]
    if not qs:return [html.P("확인된 평소행동 좌표가 중앙선에 있어 한 색으로 요약하지 않습니다." if stats["n"] else "확인된 평소행동 좌표가 없습니다.",className="small")]
    highest=max(q["count"] for q in qs);tops=[q for q in qs if q["count"]==highest]
    labels=" · ".join("%s %s명"%(q["color"],q["count"]) for q in tops)
    return [html.H3("우리 팀의 평소 업무 방식"),
        html.P("평소행동 좌표가 확인된 %s명 중 가장 많이 나타나는 색은 %s입니다. 같은 자리의 기호는 함께 표시했으며, 해당 색의 접근 방식은 아래와 같습니다."%(stats["n"],labels)),
        html.Div([html.P([html.B(q["color"]+" — "),MAP_COLOR_HINTS["usual"][q["color"]]]) for q in tops]),
        html.P("평소행동은 주변에서 관찰할 수 있는 행동 방식이고, 흥미는 좋아하는 활동입니다. 같은 색이더라도 두 항목의 의미는 다릅니다. 사람별 기호 위치와 욕구를 함께 보며 소통 방식을 확인하세요.",className="small")]


def source_examples(snap,rows,kind,limit=2):
    counts=collections.Counter()
    for r in rows:
        if not flag(r.get("analysis_eligible")) or not flag(r.get(kind+"_valid")):continue
        person=snap["people"].get(txt(r.get("employee_id")),r)
        counts.update(set(original_text(person,kind+"_personal_json")))
    result=[];seen=set()
    for t,n in sorted(counts.items(),key=lambda v:(-v[1],len(v[0]),v[0])):
        excerpt=brief(t,170)
        if excerpt not in seen:
            result.append((excerpt,n));seen.add(excerpt)
        if len(result)>=limit:break
    return result


def source_panel(snap,rows,kind,title):
    examples=source_examples(snap,rows,kind)
    return html.Div([html.H3(title),html.Ul([html.Li([t,html.Span(" (%s명 원문)"%n,className="small")]) for t,n in examples]) if examples else html.P("확인된 원문 설명이 없습니다.",className="small")],className="source-pair")


def org_page(snap,p,key,kind=None,leader=False):
    if key not in visible_orgs(p,snap):return empty("조회할 조직을 선택하세요.")
    if leader and not can_detail(p,snap,key):return empty("이 조직의 리더 가이드 조회 권한이 없습니다.")
    org=snap["orgs"][key];rows=snap["members"][key];detail=can_detail(p,snap,key)
    title=txt(org["org_name"]);audience="leader" if detail else "shared"
    report,evidence,message=report_for(snap,key,audience)
    # 팀/파트 중복 없이 개인 테이블에서 전체 진단자 비교값을 만듭니다. 이름은 보내지 않습니다.
    population=list(snap["people"].values());ref_interest=interest_stats_for(population)
    interests=interest_stats_for(rows);rank=interest_rank(interests)
    usual=map_stats(rows,"usual");needs=map_stats(rows,"needs")
    paired=[r for r in rows if color_of(r,"usual") and color_of(r,"needs") and "중앙선" not in [color_of(r,"usual"),color_of(r,"needs")]]
    changed=[r for r in paired if color_of(r,"usual")!=color_of(r,"needs")]
    summary="평소행동은 %s, 바라는 대우는 %s이 가장 많습니다. 두 항목을 함께 보며 구성원에게 맞는 업무 방식과 소통 방법을 살펴보세요."%(top_color(usual),top_color(needs)) if usual["n"] and needs["n"] else "확인된 결과부터 정리했습니다. 항목마다 반영된 인원을 함께 확인해 주세요."
    anchors=[("read-guide","30초 읽는 법"),("interest","좋아하는 일"),("usual","남에게 보이는 모습"),("needs","바라는 대우와 스트레스"),("coaching","팀 운영에 적용하기"),("comparison","참고 · 구성 비교·조직지향점")]
    if detail:anchors += [("interest-table","구성원 흥미 점수표"),("profiles","구성원 한눈에 보기")]
    cover=sheet("",None,[html.Div([html.Span("BIRKMAN · TEAM REPORT"),html.Span("조직장 열람용" if detail else "팀 공유용")],className="sheet-kicker"),html.Div(txt(org.get("org_path")),className="cover-path"),html.H1(title),
        html.Div([html.B("진단 반영 %s명"%org["analysis_count"])," / 구성원 %s명 · 기준 %s"%(org["member_count"],txt(snap["meta"].get("processed_at"))[:10])],className="cover-meta"),
        html.P(brief(report["overview"],340) if report else summary,className="cover-lead"),
        html.Div([html.Div([html.Span(label),html.Strong(value),html.Small(note)],className="metric") for label,value,note in [
        ("구성원 / 진단 반영", "%s명 / %s명"%(org["member_count"],org["analysis_count"]),"진단 없는 구성원도 명단에 포함"),
        ("좋아하는 일 · 흥미 평균 상위", " · ".join(INTERESTS[k] for k in rank[:2]) or "미확인","항목별 유효 점수 평균"),
        ("남에게 보이는 모습 · 평소행동",top_color(usual),"좌표 확인 %s명"%usual["n"]),
        ("바라는 대우 · 욕구",top_color(needs),"좌표 확인 %s명"%needs["n"])]],className="metrics"),
        html.Div([html.Div([html.H3("먼저 볼 것"),html.P("평소 모습과 바라는 대우가 같은지 살펴보세요. 업무를 맡길 때는 흥미를, 대화할 때는 욕구를 함께 참고하면 좋습니다.",className="small"),html.P("버크만은 일하는 방식과 필요한 환경을 이해하는 도구입니다. 흥미 점수는 능력이나 성과 점수가 아닙니다.",className="small")]),html.Div([html.H3("보고서 차례")]+[html.A([html.Span(name),html.Span("—",className="toc-page")],href="#"+a,**{"data-report-link":a}) for i,(a,name) in enumerate(anchors)],className="toc")],className="cover-bottom")],anchor="overview")
    if report:
        actions_=[v["action"] for v in report["leader_actions"]] if detail else []
        actions_ += [v["action"] for v in report["work_patterns"]]
        actions_ += [v["suggestion"] for v in report["collaboration"]]
        chosen=list(dict.fromkeys(actions_))[:3]
        if chosen:
            first=cover.children[0].children[1].children[-1].children[0]
            first.children=[html.H3("팀장이 먼저 해볼 일" if detail else "함께 해볼 일"),html.Ol([html.Li(brief(t,140)) for t in chosen],className="cover-actions")]
    cover.className+=" cover";sheets=[cover];appendix=[]
    sheets.append(sheet("버크만, 이것만 알고 보세요","흥미·평소행동·욕구·스트레스행동을 기호로 구분합니다. 욕구와 스트레스행동은 한 맵에서 읽습니다.",guide_block(),"read-guide","01 · 읽는 법"))
    def map_section(k):
        if detail:
            visual=svg_map(rows,k) if map_points(rows,k) else empty("이 맵에서 확인된 좌표가 없습니다.")
            return html.Div([html.Div([visual,html.P("%s %s · 좌표를 옮기지 않고 같은 자리는 함께 표시"%(SYMBOLS[k],MAPS[k]),className="map-caption")],className="map-visual"),color_summary(rows,k,True)],className="map-grid")
        return html.Div([stack_bar(map_stats(rows,k),"우리 조직"),color_summary(rows,k,False)])
    sheets.append(sheet("좋아하는 일","흥미 ✱ · 어떤 활동에 마음이 끌리는지를 보여줍니다.",[tip("새 업무를 맡기기 전 관심 분야를 확인하세요. 점수가 높다는 것은 그 일을 좋아한다는 뜻입니다."),map_section("interest")]+team_interest_explanation(interests,rank),"interest","02 · 흥미"))
    if detail:
        appendix.append(sheet("구성원별 흥미 점수","0~100 점수 · 짙을수록 관심이 높습니다. 테두리는 각자의 상위 3개이며 동점은 함께 표시합니다.",[
            score_table(rows,interests,ref_interest),html.P("—는 확인된 점수 없음. 전체 진단자 평균은 현재 명부에 연결된 유효 점수 기준입니다.",className="small")],"interest-table","참고 · 흥미 점수표"))
    else:
        appendix.append(sheet("우리 조직의 흥미 점수","항목별 유효 점수를 평균한 값입니다.",[interest_bars(interests)]))
    usual_body=[tip("색별로 몇 명이 모였는지 보세요. 팀의 일하는 방식을 이해하는 출발점입니다."),map_section("usual")]+team_usual_explanation(rows)
    # AI 해설은 ‘팀 운영에 적용하기’에 모읍니다. 욕구 근거의 문장을 평소행동 설명으로 옮기지 않습니다.
    sheets.append(sheet("남에게 보이는 모습","평소행동 ◆ · 일이 잘 풀릴 때 드러나는 업무 방식입니다.",usual_body,"usual","03 · 평소행동"))
    needsbody=[tip("○는 바라는 대우, □는 그 욕구가 충족되지 않을 때 나타날 수 있는 반응입니다. 같은 자리일 때 겹쳐 보이며, 다른 자리라면 원래 위치를 각각 표시합니다.")]
    if detail:
        display,info=paired_map_rows(rows)
        valid=bool(map_points(display,"needs") or map_points(display,"stress"))
        needsbody.append(html.Div([html.Div([svg_map(rows,["needs","stress"]) if valid else empty("확인된 좌표 없음"),html.P("○ 욕구 · □ 스트레스행동",className="map-caption")]),color_summary(rows,"needs",True)],className="map-grid"))
        info_text="같은 위치 %s명"%info["paired"]
        if info["needs_only"]:info_text+=" · 욕구만 확인 %s명"%info["needs_only"]
        if info["stress_only"]:info_text+=" · 스트레스만 확인 %s명"%info["stress_only"]
        if info["review"]:info_text+=" · 서로 다른 위치 %s명(원·네모 원위치 표시)"%info["review"]
        needsbody.append(html.P(info_text,className="small"))
        if info["stress_only"]:
            nums=roster_number(rows)
            only=[r for r in rows if map_points([r],"stress") and not map_points([r],"needs")]
            needsbody.append(html.P("네모만 표시: "+", ".join((str(nums[txt(r.get("employee_id"))])+" " if len(rows)>12 else "")+txt(r.get("성명")) for r in only),className="small"))
        needsbody.append(html.Div([source_panel(snap,rows,"needs","바라는 대우"),source_panel(snap,rows,"stress","욕구가 채워지지 않을 때")],className="twocol"))
        if len(rows)>36:
            nums=roster_number(rows)
            appendix.append(sheet("맵 번호와 구성원","모든 맵에서 같은 번호를 사용합니다.",[html.Div([html.Span([html.B(str(nums[txt(r.get("employee_id"))])+" "),txt(r.get("성명"))]) for r in rows],className="number-key")]))
    else:
        needsbody += [stack_bar(map_stats(rows,"needs"),"욕구"),html.P("욕구는 바라는 환경, 스트레스행동은 그 환경이 충족되지 않을 때 나타날 수 있는 반응입니다.")]
    needsbody.append(html.P("평소행동과 욕구의 색이 다른 구성원은 %s명 / 비교 가능한 %s명입니다. 맵 중앙 부근은 작은 위치 차이로 색이 달라질 수 있으므로 위치와 원문을 함께 보세요."%(len(changed),len(paired)),className="small"))
    needsbody.append(html.P("네모는 현재 스트레스의 정도를 측정한 값이 아닙니다. 중앙에 가깝거나 색이 다른 경우에도 기호의 실제 위치와 원문 설명을 함께 확인하세요.",className="small"))
    sheets.append(sheet("바라는 대우와 스트레스","두 기호를 한 맵에 표시하고, 바라는 환경과 충족되지 않을 때의 반응을 나누어 읽습니다.",needsbody,"needs","04 · 욕구와 스트레스"))
    if detail and changed:
        groups=collections.defaultdict(list)
        nums=roster_number(rows)
        for r in changed:
            groups[(color_of(r,"usual"),color_of(r,"needs"))].append((str(nums[txt(r.get("employee_id"))])+" " if len(rows)>12 else "")+txt(r.get("성명")))
        entries=[{"from":a+" → "+b,"names":", ".join(names)} for (a,b),names in sorted(groups.items())]
        appendix.append(sheet("평소 모습과 바라는 대우가 다른 구성원","왼쪽은 평소행동, 오른쪽은 욕구입니다. 같은 변화끼리 모았습니다.",[table([("from","평소행동 → 욕구"),("names","구성원")],entries),tip("1:1 대화에서 ‘어떤 방식으로 이야기할 때 가장 편한가요?’라고 물어보세요.")]))
    comparison=[tip("우리 조직과 전체 진단자의 색 분포를 비교합니다. 색의 많고 적음 자체가 좋고 나쁨을 뜻하지는 않습니다.")]
    for k in ["interest","usual","needs"]:
        comparison.append(html.Div([html.H3(MAP_TITLES[k]+" · "+MAPS[k]),stack_bar(map_stats(rows,k),"우리 조직"),stack_bar(map_stats(population,k),"전체 진단자",True)],className="compare-row"))
    comparison += [html.Div([html.Span([swatch(c),c]) for c in PALETTE],className="stack-legend"),html.P("전체 진단자는 현재 명부와 연결된 유효 진단 결과 기준입니다. 팀·파트에 중복 소속된 사람은 한 번만 셉니다. 각 막대 오른쪽은 해당 항목의 반영 인원입니다.",className="small")]
    orientation=js(org.get("orientation_json"),{})
    comparison += [html.H3("조직지향점 · 네 가지 근무환경")]+orientation_content(orientation)
    sheets.append(sheet("우리 조직의 구성 비교","맵의 색 분포와 조직지향점을 함께 정리했습니다.",comparison,"comparison","05 · 구성 비교"))
    comp=js(org.get("leader_comparison_json"),{})
    if detail and comp.get("leaders",{}).get("coverage",{}).get("analysis_count",0):
        compbody=[tip("리더가 일하는 방식과 구성원들이 바라는 대우를 함께 봅니다. 서로 다른 색은 대화할 주제입니다.")]
        for k in ["usual","needs"]:
            compbody.append(html.Div([html.H3(MAP_TITLES[k]),stack_bar(comp["leaders"]["maps"].get(k,{"n":0,"quadrants":[]}),"리더"),stack_bar(comp["others"]["maps"].get(k,{"n":0,"quadrants":[]}),"구성원")],className="compare-row"))
        compbody.append(html.H3("흥미를 나란히 보기"))
        compbody.append(table([("field","흥미"),("leader","리더 평균"),("others","구성원 평균")],[{"field":INTERESTS[k],"leader":"—" if num(comp["leaders"]["interests"].get(k,{}).get("mean")) is None else "%.0f"%comp["leaders"]["interests"][k]["mean"],"others":"—" if num(comp["others"]["interests"].get(k,{}).get("mean")) is None else "%.0f"%comp["others"]["interests"][k]["mean"]} for k in INTERESTS]))
        appendix.append(sheet("리더와 구성원 함께 보기","팀에서는 팀장, 파트에서는 파트장의 유효 결과를 반영합니다.",compbody))
    coaching=[]
    if report:
        for v in report["work_patterns"]:
            coaching.append(html.Div([html.H3(v["title"]),readable_text(v["observation"]),readable_text(v["action"],"action"),evidence_view(v["basis"],evidence)],className="coach"))
        for v in report["collaboration"]:
            coaching.append(html.Div([html.H3(v["situation"]),readable_text(v["suggestion"]),evidence_view(v["basis"],evidence)],className="coach"))
        if detail:
            for v in report["leader_actions"]:
                coaching.append(html.Div([html.H3(v["situation"]),readable_text(v["action"]),evidence_view(v["basis"],evidence)],className="coach"))
    if not coaching:coaching=[empty("맞춤 해설을 아직 불러오지 못했습니다. 앞쪽의 맵·점수·명단은 확인할 수 있습니다."),tip("업무를 맡길 때 관심 분야를 확인하고, 1:1 대화에서는 바라는 지원을 직접 물어보세요.")]
    if report and report.get("coverage_note"):
        coaching.insert(0,html.P(report["coverage_note"],className="small"))
    if detail and not comp.get("leaders",{}).get("coverage",{}).get("analysis_count",0):
        coaching.insert(0,html.P("리더의 유효한 진단이 없어 리더 성향 비교는 생략했습니다.",className="small"))
    if report and report["discussion_questions"]:
        coaching += [html.H3("다음 팀 대화에서"),html.Ul([html.Li(t) for t in report["discussion_questions"]],className="tight-list")]
    # 행동 제안까지 먼저 읽고, 비교·점수표·개인 카드는 뒤에서 참고합니다.
    comparison_sheet=sheets.pop()
    sheets.append(sheet("팀 운영에 적용하기","보고서를 읽고 바로 시도할 대화와 업무 방식입니다.",coaching,"coaching","05 · 리더 가이드"))
    comparison_sheet.children[0].children[0].children[0].children="06 · 구성 비교"
    appendix.insert(0,comparison_sheet)
    if detail:
        appendix.append(sheet("구성원별 한눈에 보기","팀 보고서는 팀장, 파트 보고서는 파트장을 먼저 표시하고 나머지는 가나다순으로 정리했습니다. ✱ 흥미 · ◆ 평소행동 · ○ 욕구 · □ 스트레스행동. 전문은 구성원 상세에서 확인할 수 있습니다.",
            profile_sections(rows,org,snap),"profiles","참고 · 구성원"))
    if report:
        extra_texts=[]
        if brief(report["overview"],340)!=txt(report["overview"]):
            extra_texts += [html.H3("전체 개요"),html.P(report["overview"])]
        for category,fields in [("work_patterns",["observation","action"]),("collaboration",["suggestion"]),("leader_actions",["action"])]:
            for entry in report[category]:
                if any(brief(entry[f])!=txt(entry[f]) for f in fields):
                    extra_texts += [html.H3(entry.get("title") or entry.get("situation"))]+[html.P(entry[f]) for f in fields]
        if extra_texts:appendix.append(sheet("해설 전문","본문에서 짧게 제시한 해설의 전체 문장입니다.",extra_texts))
    sheets.extend(appendix)
    return finish_report(sheets,title+" 버크만 팀 리포트",snap)


def personal_page(snap,p,eid,kind=None):
    if not eid or not allowed_person(p,snap,eid):return empty("조회할 구성원을 선택하세요.")
    person=snap["people"].get(eid)
    if not person:return empty("구성원 정보가 없습니다.")
    name=txt(person.get("성명"));subtitle=" · ".join(filter(None,[txt(person.get("팀명")),txt(person.get("파트명")),txt(person.get("직책명"))]))
    if not flag(person.get("analysis_eligible")):
        return finish_report([sheet(name,subtitle,[empty("표시할 진단 결과가 없습니다. 명부에는 포함되어 있습니다.")])],name,snap)
    values=interest_stats_for([person])
    first=sheet(name,subtitle,[html.Div([html.Div([svg_map([person],list(MAPS),labels=False),html.P("✱ 흥미 · ◆ 평소행동 · ○ 욕구 · □ 스트레스행동",className="map-caption")]),html.Div([html.H3("좋아하는 일"),interest_bars(values)])],className="map-grid")])
    pages=[first]
    for k in ["interest","usual"]:
        lines=original_text(person,k+"_personal_json")
        pages.append(sheet(MAP_TITLES[k],SYMBOLS[k]+" "+MAPS[k],[html.Div([svg_map([person],k,labels=False) if map_points([person],k) else empty("좌표 미확인"),html.Div([html.H3("결과지 설명"),html.Ul([html.Li(t) for t in lines],className="tight-list") if lines else empty("설명 미확인")])],className="map-grid")]))
    pages.append(sheet("바라는 대우와 스트레스","○는 바라는 대우, □는 욕구가 충족되지 않을 때 나타날 수 있는 반응입니다.",[
        html.Div([svg_map([person],["needs","stress"],labels=False),html.Div([html.H3("두 기호, 다른 의미"),html.P("원과 네모는 추출된 원래 위치에 표시합니다. 아래에서 욕구와 스트레스행동의 설명을 구분해 읽어보세요.")])],className="map-grid"),
        html.H3("바라는 대우 · 욕구"),html.Ul([html.Li(t) for t in original_text(person,"needs_personal_json")],className="tight-list") if original_text(person,"needs_personal_json") else empty("욕구 설명 미확인"),
        html.H3("욕구가 채워지지 않을 때 · 스트레스행동"),html.Ul([html.Li(t) for t in original_text(person,"stress_personal_json")],className="tight-list") if original_text(person,"stress_personal_json") else empty("스트레스 설명 미확인")]))
    strengths=original_text(person,"strengths_json")
    if strengths:pages.append(sheet("강점","개인 결과지 원문",[html.Ul([html.Li(t) for t in strengths],className="tight-list")]))
    profile=next((js(r.get("orientation_profile_json"),{}) for members in snap["members"].values() for r in members if txt(r.get("employee_id"))==eid and flag(r.get("orientation_valid"))),{})
    pages.append(sheet("선호하는 업무 환경","조직지향점",orientation_content(profile,True)))
    return finish_report(pages,name+" 버크만 결과",snap)


def footer(snap,p):
    return html.Div("집계 기준: "+txt(snap["meta"].get("processed_at"))[:16],className="footer")


# =====================================================================
# 7. 메일 연결 / Dataiku가 생성한 Dash app 객체 설정
# =====================================================================
def send_otp(email, code):
    """기존 Dataiku 메일 채널을 이용합니다. 회사가 별도 발송 함수를 쓴다면
    이 함수의 본문만 회사에서 사용하는 방식으로 교체하면 됩니다.
    email, code를 print/log로 출력하지 마세요. 프런트엔드로 반환하지 않습니다.
    """
    client = dataiku.api_client()
    project_key = dataiku.default_project_key()
    channel = client.get_messaging_channel(MAIL_CHANNEL_ID)
    body = ("버크만 팀 리포트 로그인 인증번호: " + code + "\n\n"
            "5분 이내에 요청한 화면에서 입력해 주세요. 한 번만 사용할 수 있습니다.\n"
            "본인이 요청하지 않았다면 이 메일을 무시해 주세요.")
    channel.send(project_key, [email], "[버크만 팀 리포트] 로그인 인증번호", body, plain_text=True)


ACCESS = None
REPO = Repository()
SETUP_ERROR = ""
try:
    if AUTH_MODE not in {"preview", "email_otp", "password"}:
        raise ValueError("AUTH_MODE는 preview, email_otp, password 중 하나로 설정하세요.")
    if AUTH_MODE == "email_otp" and not MAIL_CHANNEL_ID.strip():
        raise ValueError("MAIL_CHANNEL_ID를 설정하세요.")
    state_folder = dataiku.Folder(AUTH_FOLDER_ID)
    # 로컬 폴더가 아니거나 컨테이너에서 접근 불가하면 여기서 안전하게 중단합니다.
    state_path = state_folder.get_path()
    ACCESS = AccessStore(state_path, FIRST_ADMIN_EMAIL)
except Exception as exc:
    SETUP_ERROR = "웹앱 초기 설정을 확인해 주세요. 관리자 이메일·상태 폴더 설정을 확인하세요. email_otp 모드에서는 메일 채널도 필요합니다."
    fail_log("setup", exc)
    # 로그에는 알려진 설정 안내만 표시. 외부 API 예외 전문은 출력하지 않음.
    if isinstance(exc, ValueError) and str(exc).startswith(("AUTH_MODE", "MAIL_CHANNEL_ID", "FIRST_ADMIN_EMAIL", "기존 DB")):
        LOG.error(str(exc))

if "app" not in globals():
    raise RuntimeError("이 코드는 Dataiku Dash 웹앱의 Python 편집기에서 실행하세요.")

server = app.server
server.secret_key = ACCESS.secret if ACCESS else secrets.token_hex(32)
cookie_suffix = hashlib.sha256((str(dataiku.default_project_key()) + ":" + AUTH_FOLDER_ID).encode()).hexdigest()[:16]
server.config.update(SESSION_COOKIE_NAME="birkman_"+cookie_suffix, SESSION_COOKIE_HTTPONLY=True,
                     SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Lax",
                     SESSION_COOKIE_PATH=app.config.requests_pathname_prefix or "/",
                     PERMANENT_SESSION_LIFETIME=dt.timedelta(seconds=SESSION_LIFETIME_SECONDS),
                     SESSION_REFRESH_EACH_REQUEST=False, MAX_CONTENT_LENGTH=256*1024)
app.title = APP_TITLE
app.config.suppress_callback_exceptions = False
app.css.config.serve_locally = True
app.scripts.config.serve_locally = True
# 가운데 정렬된 조직지향점만 별도로 지정합니다. 흥미 점수 막대는 0부터 시작합니다.
# 긴 이름/문장이 셀 밖으로 나가지 않게 줄바꿈하고, A4 크기는 모든 페이지에 동일합니다.
CSS += r"""
.orientation-centered{display:flex;justify-content:center;position:relative}
.orientation-centered:before{content:'';position:absolute;left:50%;top:0;bottom:0;width:1px;background:#9ba8b2;opacity:.35}
.orientation-centered .orientation-fill{flex:none;position:relative}
.orientation-descriptions{display:grid;grid-template-columns:1fr 1fr;gap:3mm 5mm;margin-top:4mm}
.orientation-description b{font-size:9pt}.orientation-description p{margin:1mm 0!important}
.report-sheet th,.report-sheet td,.report-sheet p,.report-sheet h1,.report-sheet h2,.report-sheet h3,.sheet-footer span{overflow-wrap:anywhere}
.report-sheet th{white-space:normal}.report-sheet .profile{min-width:0}.report-sheet .profile-head{flex-wrap:wrap}
.report-tools{gap:12px;align-items:start;justify-content:space-between}
.report-notes{flex:1;font-size:12px}.report-notes>summary{cursor:pointer;padding:7px 10px}
.report-explanations{background:white;border:1px solid var(--line);padding:14px;margin:5px 0 10px}
.report-explanations .evidence{display:block!important;margin:10px 0;font-size:12px}
.report-explanations .evidence summary{cursor:pointer}.report-explanations .evidence p,.report-explanations .evidence li{overflow-wrap:anywhere}
.test-mode-note{font-size:12px;background:#fff4dc;border:1px solid #e1cd97;padding:9px 12px;max-width:960px;margin:0 auto 15px}
.password-enrollment{border-top:1px solid var(--line);margin-top:20px;padding-top:18px}
.enrollment-token{font-family:monospace;overflow-wrap:anywhere;font-size:14px;background:#f2f5f7;padding:12px;user-select:all}
@media print{.report-notes,.test-mode-note,.report-explanations{display:none!important}}
"""
# 기본 Dataiku URL/자산 설정을 유지하면서 인쇄용 CSS만 삽입합니다.
if AUTH_MODE == "preview":
    # 콜백 ID는 유지하되 로그인 입력 UI는 표시하지 않습니다.
    CSS += "\n#login-panel label,#login-panel input,#login-panel button,#login-panel .actions,#login-panel p.small{display:none!important;}"
elif AUTH_MODE == "password":
    CSS += "\n#send-code{display:none!important;}"
app.index_string = app.index_string.replace("</head>", "<style>"+CSS+"</style></head>")
PAGINATION_JS = r"""
/* 고정 A4 화면과 인쇄에서 같은 DOM을 사용합니다. 측정은 글꼴·이미지 준비 후 합니다. */
(function(){
'use strict';
const roots=new WeakMap();
let observer, queued=false;
function clean(node){
 const clone=node.cloneNode(true);
 if(clone.nodeType===1){
  clone.removeAttribute('id');
  clone.querySelectorAll('[id]').forEach(n=>n.removeAttribute('id'));
 }
 return clone;
}
function box(root,source,continued){
 const wrap=document.createElement('div');wrap.className='report-page-wrap';
 const page=document.createElement('section');page.className=source.className.replace('report-source-sheet','')+' paged-sheet';
 const content=document.createElement('div');content.className='sheet-content';
 const heading=clean(source.querySelector('.report-heading'));
 if(continued){
  let h=heading.querySelector('h2');
  if(!h){h=document.createElement('h2');h.textContent=source.querySelector('h1,h2')?.textContent||'보고서';heading.appendChild(h);}
  h.append(' · 계속');
  heading.querySelectorAll('.sheet-intro').forEach(n=>n.remove());
 }
 const body=document.createElement('div');body.className='report-body';
 content.append(heading,body);page.append(content);
 const footer=clean(source.querySelector('.sheet-footer'));page.append(footer);
 wrap.append(page);root.append(wrap);
 return {wrap,page,body,heading};
}
function paginate(report){
 if(!report.isConnected)return;
 const source=report.querySelector(':scope > .report-source');
 const surface=report.querySelector(':scope > .report-preview');
 if(!source||!surface)return;
 const sources=[...source.querySelectorAll(':scope > .report-sheet')];
 if(!sources.length)return;
 surface.replaceChildren();surface.classList.add('measuring');
 const anchors={};let current,total=0;
 const pageState=[];
 function next(src,continued){
  current=box(surface,src,continued);total++;current.page.id='report-page-'+total;
  pageState.push(current);
  const anchor=src.dataset.reportAnchor;
  if(anchor&&!anchors[anchor])anchors[anchor]=total;
  return current;
 }
 function fits(){return current.body.scrollHeight<=current.body.clientHeight+0.8;}
 function hasBody(){return current.body.children.length>0;}
 function splitText(original,src,shellFactory){
  // 긴 문장은 내용을 빠뜨리지 않고 다음 페이지로 이어갑니다.
  let rest=original.textContent||'';
  while(rest){
   let lo=1,hi=rest.length,best=0,candidate;
   while(lo<=hi){
    const mid=Math.floor((lo+hi)/2);candidate=shellFactory();candidate.textContent=rest.slice(0,mid);
    current.body.append(candidate);const okay=fits();candidate.remove();
    if(okay){best=mid;lo=mid+1;}else hi=mid-1;
   }
   if(!best){if(hasBody()){next(src,true);continue;}throw Error('한 글자도 들어가지 않는 페이지 영역');}
   if(best<rest.length){
    const space=rest.lastIndexOf(' ',best);
    if(space>best*.7)best=space+1;
   }
   candidate=shellFactory();candidate.textContent=rest.slice(0,best);current.body.append(candidate);rest=rest.slice(best);
   if(rest)next(src,true);
  }
 }
 function tableBlock(block,src){
  const originalTable=block.querySelector('table');
  const rows=[...originalTable.querySelectorAll('tbody > tr')];
  let holder,tb;
  function start(){
   holder=clean(block);const t=holder.querySelector('table');tb=t.querySelector('tbody');tb.replaceChildren();
   current.body.append(holder);
  }
  start();
  for(const row of rows){
   let copy=clean(row);tb.append(copy);
   if(!fits()){
    copy.remove();
    if(!tb.children.length){holder.remove();}
    if(!hasBody()){
      start();splitRow(row);continue;
    }
    next(src,true);start();copy=clean(row);tb.append(copy);
    if(!fits()){
     copy.remove();splitRow(row);
    }
   }
  }
  if(holder&&!tb.children.length)holder.remove();
  function splitRow(row){
   // 한 행이 한 장보다 길어도 표를 문단으로 바꾸지 않고 같은 열·테두리로 이어갑니다.
   const rest=[...row.cells].map(c=>c.textContent||'');
   let continuation=false;
   while(rest.some(Boolean)){
    let lo=1,hi=Math.max(...rest.map(t=>t.length)),best=0;
    function fragment(n){const r=clean(row);[...r.cells].forEach((cell,i)=>{cell.textContent=rest[i].slice(0,n);});return r;}
    while(lo<=hi){const mid=Math.floor((lo+hi)/2),probe=fragment(mid);tb.append(probe);const okay=fits();probe.remove();if(okay){best=mid;lo=mid+1;}else hi=mid-1;}
    if(!best)throw Error('표 머리글과 한 글자를 함께 담을 수 없습니다');
    const r=fragment(best);if(continuation)r.classList.add('continued-row');tb.append(r);
    rest.forEach((t,i)=>rest[i]=t.slice(best));
    if(rest.some(Boolean)){next(src,true);start();continuation=true;}
   }
  }
 }
 function gridBlock(block,src){
  const children=[...block.children];let holder;
  function start(){holder=block.cloneNode(false);holder.removeAttribute('id');current.body.append(holder);}
  start();
  for(const child of children){
   let c=clean(child);holder.append(c);
   if(fits())continue;
   c.remove();
   if(!holder.children.length)holder.remove();
   if(hasBody()){next(src,true);start();c=clean(child);holder.append(c);if(fits())continue;c.remove();holder.remove();}
   // 한 카드가 한 장보다 길면 내부 내용 단위로 나눕니다.
   append(child,src,true);start();
  }
  if(holder&&!holder.children.length)holder.remove();
 }
 function append(block,src,forceSplit=false){
  if(block.nodeType!==1)return;
  let node=clean(block);current.body.append(node);
  if(fits()&&!forceSplit)return;
  node.remove();
  if(block.classList.contains('table-wrap')&&block.querySelector('tbody')){tableBlock(block,src);return;}
  if(block.matches('.profile-grid,.number-key')){gridBlock(block,src);return;}
  // 소제목만 페이지 끝에 남지 않도록 다음 내용과 같이 넘깁니다.
  const prev=current.body.lastElementChild;
  if(prev&&/^H[23]$/.test(prev.tagName)){prev.remove();next(src,true);current.body.append(prev);}
  else if(hasBody()&&!forceSplit){next(src,true);node=clean(block);current.body.append(node);if(fits())return;node.remove();}
  if(block.matches('img,svg')){
   // 맵 비율과 폭을 보존합니다. 잘라내거나 문자로 변환하지 않습니다.
   node=clean(block);current.body.append(node);if(!fits())throw Error('맵을 A4 영역에 담을 수 없습니다');return;
  }
  if(block.matches('p,li')||!block.children.length){splitText(block,src,()=>{const n=block.cloneNode(false);n.removeAttribute('id');return n;});return;}
  if(block.matches('ul,ol')){listBlock(block,src);return;}
  // 큰 구성은 자식 단위로 넘기되 맵 위치·크기는 유지합니다.
  for(const child of [...block.children])append(child,src);
 }
 function listBlock(block,src){
  let holder;const base=parseInt(block.getAttribute('start'),10)||1;
  function start(){holder=block.cloneNode(false);current.body.append(holder);}
  start();
  [...block.children].forEach((child,index)=>{
   let rest=child.textContent||'',first=true;
   do{
    const item=clean(child);if(block.tagName==='OL')item.value=base+index;
    if(!first)item.textContent=rest;
    holder.append(item);if(fits())break;item.remove();
    if(holder.children.length||[...current.body.children].some(n=>n!==holder)){
     if(!holder.children.length)holder.remove();next(src,true);start();continue;
    }
    let lo=1,hi=rest.length,best=0;
    while(lo<=hi){const mid=Math.floor((lo+hi)/2),probe=child.cloneNode(false);probe.textContent=rest.slice(0,mid);holder.append(probe);const okay=fits();probe.remove();if(okay){best=mid;lo=mid+1;}else hi=mid-1;}
    if(!best)throw Error('목록 한 글자를 담을 수 없습니다');
    const part=child.cloneNode(false);if(block.tagName==='OL')part.value=base+index;part.textContent=rest.slice(0,best);holder.append(part);
    rest=rest.slice(best);first=false;if(rest){next(src,true);start();}
   }while(rest);
  });
  if(!holder.children.length)holder.remove();
 }
 for(const src of sources){
  next(src,false);
  const body=src.querySelector('.report-body');
  const blocks=[...body.children];
  for(let i=0;i<blocks.length;i++){
   const block=blocks[i];
   if(/^H[23]$/.test(block.tagName)&&blocks[i+1]){
    const probe=clean(block),nextProbe=clean(blocks[i+1]);current.body.append(probe,nextProbe);
    const can=fits();probe.remove();nextProbe.remove();if(!can&&hasBody())next(src,true);
   }
   append(block,src);
  }
 }
 // 실제 만들어진 페이지 번호를 목차와 바닥글에 함께 반영합니다.
 pageState.forEach((p,i)=>{
  const n=p.page.querySelector('.page-number');if(n)n.textContent=(i+1)+' / '+total;
  p.page.querySelectorAll('.toc a').forEach(a=>{
   const target=a.dataset.reportLink||a.getAttribute('href')?.replace(/^#/,'');
   const pageNo=anchors[target];if(!pageNo)return;
   a.href='#report-page-'+pageNo;
   const label=a.querySelector('.toc-page');if(label)label.textContent=pageNo;
  });
 });
 surface.classList.remove('measuring');
 report.dataset.pages=String(total);report.dataset.paged='ready';
 const btn=document.querySelector('#print');if(btn)btn.disabled=false;
 resize(report);
}
function resize(report){
 const surface=report.querySelector('.report-preview');if(!surface)return;
 const fit=report.dataset.zoom!=='actual';
 const available=report.clientWidth;
 surface.querySelectorAll(':scope > .report-page-wrap').forEach(w=>{
  const page=w.firstElementChild;
  const width=page.offsetWidth,height=page.offsetHeight;
  const scale=fit?Math.min(1,available/width):1;
  w.style.width=(width*scale)+'px';w.style.height=(height*scale)+'px';
  page.style.transform='scale('+scale+')';
 });
}
async function prepare(report){
 const source=report.querySelector('.report-source');if(!source)return;
 const signature=source.innerHTML;
 const previous=roots.get(report);if(previous&&previous.signature===signature)return;
 const state={signature};roots.set(report,state);report.dataset.paged='loading';
 const btn=document.querySelector('#print');if(btn)btn.disabled=true;
 await document.fonts.ready;
 const images=[...report.querySelectorAll('.report-source img')];
 await Promise.all(images.map(img=>img.decode?img.decode().catch(()=>{}):Promise.resolve()));
 if(!report.isConnected||roots.get(report)!==state)return;
 try{paginate(report);}catch(error){
  report.dataset.paged='error';
  const surface=report.querySelector('.report-preview');surface.replaceChildren();
  const msg=document.createElement('p');msg.className='empty';msg.textContent='페이지 배치를 완료하지 못했습니다. 화면을 새로 열어 주세요.';surface.append(msg);
  console.error('Birkman pagination failed',error);
 }
}
function scan(){
 if(queued)return;queued=true;
 requestAnimationFrame(()=>{queued=false;document.querySelectorAll('#page .report').forEach(prepare);});
}
window.birkmanPrint=async function(){
 const r=document.querySelector('#page .report');
 if(!r||r.dataset.paged!=='ready')return;
 await document.fonts.ready;window.print();
};
window.addEventListener('resize',()=>document.querySelectorAll('#page .report').forEach(resize));
window.addEventListener('beforeprint',()=>document.querySelectorAll('#page .report').forEach(r=>{
 if(r.dataset.paged==='ready')r.querySelectorAll('.report-page-wrap').forEach(w=>w.firstElementChild.style.transform='none');
}));
window.addEventListener('afterprint',()=>document.querySelectorAll('#page .report').forEach(resize));
document.addEventListener('click',event=>{
 const b=event.target.closest('.report-zoom');if(!b)return;
 const r=b.closest('.report');r.dataset.zoom=r.dataset.zoom==='actual'?'fit':'actual';
 b.textContent=r.dataset.zoom==='actual'?'폭에 맞추기':'원본 크기로 보기';resize(r);
});
function start(){observer=new MutationObserver(scan);observer.observe(document.body,{childList:true,subtree:true});scan();}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();

"""
app.index_string = app.index_string.replace("</body>","<script>"+PAGINATION_JS+"</script></body>")


@server.after_request
def no_private_cache(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self'; object-src 'none'; base-uri 'self'"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


def csrf_ok(token):
    real = session.get("birkman_csrf", "")
    return isinstance(token, str) and bool(real) and hmac.compare_digest(real, token)


def layout():
    csrf = ""
    if has_request_context():
        if "birkman_csrf" not in session:
            session["birkman_csrf"] = secrets.token_urlsafe(32)
            session["birkman_browser"] = secrets.token_urlsafe(32)
        csrf = session["birkman_csrf"]
    return html.Div([
        dcc.Store(id="csrf", data=csrf, storage_type="memory"),
        dcc.Store(id="event", data="", storage_type="memory"),
        dcc.Store(id="admin-revision", data=0),
        # 미리보기의 60초마다 화면 재구성을 중단. 인증 모드에서는 세션 만료 확인을 유지합니다.
        dcc.Interval(id="heartbeat", interval=60000, n_intervals=0,disabled=AUTH_MODE=="preview"),
        html.Div([
            html.Div("동우화인켐", className="wordmark"), html.H1(APP_TITLE),
            html.P("보고서를 불러오는 중입니다. 계속 이 화면이면 FIRST_ADMIN_EMAIL이 명부에 유일하게 등록되어 있는지와 집계 완료 여부를 확인하세요." if AUTH_MODE == "preview" else "사내 이메일로 로그인해 우리 팀의 결과를 확인하세요.", className="muted"),
            html.Label("사내 이메일",htmlFor="login-email",className="label",style={"marginTop":"26px"}),
            dcc.Input(id="login-email",type="email",placeholder="이메일주소@dwchem.co.kr",maxLength=254,autoComplete="email"),
            html.Div(html.Button("인증번호 받기",id="send-code",n_clicks=0,className="primary"),className="actions"),
            html.Label("비밀번호" if AUTH_MODE=="password" else "인증번호",htmlFor="login-code",className="label"),
            dcc.Input(id="login-code",type="password" if AUTH_MODE=="password" else "text",placeholder="본인이 설정한 비밀번호" if AUTH_MODE=="password" else "6자리 인증번호",maxLength=128 if AUTH_MODE=="password" else 6,autoComplete="current-password" if AUTH_MODE=="password" else "one-time-code"),
            html.Button("로그인",id="verify-code",n_clicks=0,className="primary"),
            html.Div(SETUP_ERROR,id="login-feedback",className="notice",role="status"),
            html.P("비밀번호를 잊었다면 담당자에게 본인 확인 후 등록코드 재발급을 요청하세요." if AUTH_MODE=="password" else "인증번호는 5분 동안 유효합니다.",className="muted small",style={"marginTop":"16px"}),
            html.Div([
                html.H3("처음 이용하거나 비밀번호를 다시 설정할 때"),
                html.P("담당자가 본인 확인 후 전달한 등록코드를 입력하세요. 회사 계정의 비밀번호와 다르게 정하세요.",className="small"),
                dcc.Input(id="activation-code",type="text",placeholder="담당자에게 받은 일회용 등록코드",maxLength=64,autoComplete="off"),
                dcc.Input(id="new-password",type="password",placeholder="새 비밀번호 (15~128자)",maxLength=128,autoComplete="new-password"),
                dcc.Input(id="confirm-password",type="password",placeholder="새 비밀번호 한 번 더",maxLength=128,autoComplete="new-password"),
                html.Button("비밀번호 설정",id="register-password",n_clicks=0),
                html.Div(id="register-feedback",role="status",className="notice"),
            ],className="password-enrollment",style={} if AUTH_MODE=="password" else {"display":"none"}),
        ],id="login-panel",className="login"),
        html.Div([
            html.Aside([
                html.Div("동우화인켐",className="wordmark"),html.H2(APP_TITLE),
                dcc.RadioItems(id="nav", options=[{"label":"팀·파트 보고서","value":"team"}], value="team",className="nav"),
                html.Div([html.Div(id="user-label"),html.Button("로그아웃",id="logout",n_clicks=0,style={"display":"none"} if AUTH_MODE == "preview" else {})],className="user"),
            ],className="sidebar no-print"),
            html.Main([
                html.Div("로그인 없는 테스트 모드 · 접속자는 모두 같은 인사 화면을 봅니다.",className="test-mode-note no-print",style={} if AUTH_MODE=="preview" else {"display":"none"}),
                html.Div([
                    html.Div([html.Label("조직",className="label"),dcc.Dropdown(id="org",options=[],value=None,clearable=False,placeholder="조직 선택")],className="org"),
                    html.Div([html.Button("새로 읽기",id="reload",n_clicks=0),html.Button("인쇄 · PDF 저장",id="print",n_clicks=0)],className="actions"),
                ],className="toolbar"),
                html.Div([
                    dcc.Input(id="map-kind",value="usual",type="hidden"),
                ],id="map-controls",className="no-print",style={"marginBottom":"18px"}),
                html.Div([html.Label("구성원",className="label"),dcc.Dropdown(id="person",options=[],value=None,clearable=False)],
                         id="person-control",className="no-print",style={"display":"none"}),
                dcc.Loading(html.Div(id="page"),type="circle",color="#527a60"),
                html.Div([
                    page_header("접근 권한 관리","이메일별로 역할과 조회 조직을 지정합니다."),
                    html.Div([
                        html.Div([html.Label("대상 직원",className="label"),dcc.Dropdown(id="admin-email",options=[],value=None,placeholder="성명 또는 이메일로 검색")],className="wide"),
                        html.Div([html.Label("조회 역할",className="label"),dcc.Dropdown(id="admin-role",options=[{"label":v,"value":k} for k,v in ROLE_LABELS.items()],value="member",clearable=False)]),
                        html.Div([html.Label("사용·관리 권한",className="label"),dcc.Checklist(id="admin-flags",options=[{"label":"로그인 허용","value":"active"},{"label":"권한 관리자","value":"admin"}],value=["active"])]),
                        html.Div([html.Label("조회 조직 — 팀장은 팀, 파트장은 파트 선택",className="label"),dcc.Dropdown(id="admin-scopes",options=[],value=[],multi=True,placeholder="조회할 팀 또는 파트를 선택")],className="wide"),
                        html.Div(html.Button("권한 저장",id="save-access",n_clicks=0,className="primary"),className="wide"),
                    ],className="card admin-form"),
                    html.P("팀원은 본인 소속 팀·파트의 공유 보고서를 볼 수 있습니다. 팀장은 지정 팀과 하위 파트, 파트장은 지정 파트의 개인 결과와 리더 가이드를 볼 수 있습니다. 인사팀은 전체 조직을 조회합니다. 권한 관리자는 조회 역할과 별도로 지정합니다.",className="admin-note"),
                    html.Div(id="admin-feedback",className="notice",role="status"),
                    html.Div(id="access-table"),
                    html.Div([
                        html.H3("메일 없이 계정 등록 / 비밀번호 재설정"),
                        html.P("위에서 대상 직원과 로그인 허용을 확인하세요. 대면 또는 기존 사내 연락수단으로 본인 확인한 경우에만 체크하고 발급합니다. 등록코드는 30분·1회 유효하며 발급 화면에서만 표시됩니다.",className="small"),
                        dcc.Checklist(id="identity-confirmed",options=[{"label":"대상자 본인을 확인했고 본인에게 직접 전달합니다","value":"confirmed"}],value=[]),
                        html.Button("일회용 등록코드 발급",id="issue-activation",n_clicks=0),
                        html.Div(id="activation-feedback",role="status",className="notice"),
                    ],className="card",style={} if AUTH_MODE in {"preview","password"} else {"display":"none"}),
                ],id="admin-panel",style={"display":"none"}),
                html.Div(id="print-signal",style={"display":"none"}),
            ],className="main"),
        ],id="app-panel",className="shell",style={"display":"none"}),
    ])


app.layout=layout


# =====================================================================
# 8. 콜백: 모든 개인/조직 조회, 권한 변경 시 서버 세션을 다시 확인합니다.
# =====================================================================
@app.callback([Output("event","data"),Output("login-feedback","children"),Output("admin-feedback","children")],
              [Input("send-code","n_clicks"),Input("verify-code","n_clicks"),Input("logout","n_clicks"),Input("save-access","n_clicks"),Input("reload","n_clicks")],
              [State("login-email","value"),State("login-code","value"),State("csrf","data"),
               State("admin-email","value"),State("admin-role","value"),State("admin-scopes","value"),State("admin-flags","value"),State("admin-revision","data")],
              prevent_initial_call=True)
def actions(_send,_verify,_logout,_save,_reload,email,code,csrf,target,role,scopes,flags,revision):
    event=callback_context.triggered[0]["prop_id"].split(".")[0]
    nonce=secrets.token_hex(8)
    if not ACCESS:
        return nonce,SETUP_ERROR,""
    if not csrf_ok(csrf):
        return nonce,"페이지를 새로 열고 다시 시도해 주세요.",""
    try:
        if AUTH_MODE == "preview" and event in {"send-code", "verify-code", "logout"}:
            return nonce,"현재는 로그인 없이 확인하는 모드입니다.",""
        if event=="logout":
            ACCESS.logout(session.pop("birkman_sid",None))
            return nonce,"로그아웃했습니다.",""
        snap=REPO.get()
        if event in {"send-code","verify-code"}:
            email=email_address(email)
            if not email:
                return nonce,"회사 이메일 주소를 입력해 주세요.",""
            person=snap["login"].get(email)
            permitted=bool(person and ACCESS.rule(email)["active"])
            browser=session.get("birkman_browser","")
            if not browser:
                return nonce,"페이지를 새로 열어 주세요. HTTPS 주소인지 확인해 주세요.",""
            if AUTH_MODE=="password":
                if event=="send-code":return nonce,"비밀번호 로그인 모드입니다.",""
                token=ACCESS.verify_password(email,txt(person.get("employee_id")) if permitted else "",code or "",browser)
                if not token:return nonce,"이메일 또는 비밀번호를 확인하세요. 여러 번 실패했다면 15분 후 다시 시도하세요.",""
                ACCESS.logout(session.get("birkman_sid"))
                session["birkman_sid"]=token;session.permanent=True
                return nonce,"",""
            if event=="send-code":
                challenge=ACCESS.challenge(email,browser,permitted)
                if challenge:
                    cid,otp=challenge
                    try:
                        send_otp(email,otp)
                    except Exception as exc:
                        ACCESS.cancel_challenge(email,cid)
                        fail_log("mail",exc)
                        return nonce,"메일 발송을 완료하지 못했습니다. 담당자에게 문의해 주세요.",""
                return nonce,"등록된 주소라면 인증번호가 발송됩니다. 재요청은 1분 뒤 가능하며, 받지 못했다면 명부 등록 여부를 담당자에게 확인해 주세요.",""
            token=ACCESS.verify(email,browser,txt(code),txt(person.get("employee_id")) if permitted else "")
            if not token:
                return nonce,"인증번호가 일치하지 않거나 만료되었습니다. 요청한 화면에서 다시 확인해 주세요.",""
            ACCESS.logout(session.get("birkman_sid"))
            session["birkman_sid"]=token
            session.permanent=True
            return nonce,"",""
        p=principal(snap)
        if not p:
            return nonce,"로그인이 필요합니다.",""
        if event=="reload":
            # 일반 사용자도 완료된 자료를 다시 요청할 수 있으나 읽기 캐시의 주기를 유지.
            if p["rule"]["role"]=="hr" or p["rule"]["admin"]:
                REPO.get(force=True)
            return nonce,"","자료를 다시 읽었습니다."
        if event=="save-access":
            if not p["rule"]["admin"]:
                raise PermissionError()
            target=email_address(target)
            if target not in snap["login"]:
                return nonce,"","명부에 유일하게 등록된 회사 이메일을 선택하세요."
            scopes=scopes or []
            if not isinstance(scopes,list) or len(scopes)>500:
                return nonce,"","조회 조직을 확인하세요."
            if role in {"member","hr"}:
                scopes=[]
            elif role in {"team_leader","part_leader"}:
                level="team" if role=="team_leader" else "part"
                if not scopes or any(not isinstance(k,str) or snap["orgs"].get(k,{}).get("org_level")!=level for k in scopes):
                    return nonce,"","팀장은 팀, 파트장은 파트를 최소 한 개 지정하세요."
            else:
                return nonce,"","조회 역할을 선택하세요."
            flags=flags or []
            ACCESS.save_rule(p["email"],target,role,scopes,"active" in flags,"admin" in flags,int(revision or 0))
            return nonce,"권한을 변경한 계정은 다시 로그인해 주세요.","저장했습니다. 대상 계정의 기존 로그인은 종료했습니다."
    except PermissionError:
        return nonce,"","이 작업의 권한이 없습니다."
    except ValueError as exc:
        # 내부 API의 예외는 그대로 화면에 보내지 않습니다.
        if event=="save-access":
            safe=("최초 관리자는", "다른 관리자가", "관리자는", "권한과 이메일")
            msg=str(exc) if str(exc).startswith(safe) else "설정값을 확인해 주세요."
            return nonce,"",msg
        fail_log("action",exc)
        return nonce,"자료를 준비 중입니다. 담당자에게 집계 완료 여부를 확인해 주세요.",""
    except Exception as exc:
        fail_log("action",exc)
        return nonce,"요청을 처리하지 못했습니다. 담당자에게 문의해 주세요.",""
    return nonce,no_update,no_update


@app.callback(Output("register-feedback","children"),Input("register-password","n_clicks"),
              [State("login-email","value"),State("activation-code","value"),State("new-password","value"),
               State("confirm-password","value"),State("csrf","data")],prevent_initial_call=True)
def register_password(_click,email,code,password,confirmation,csrf):
    if AUTH_MODE!="password" or not ACCESS:return "비밀번호 운영 모드에서 사용합니다."
    if not csrf_ok(csrf):return "페이지를 새로 열고 다시 시도하세요."
    if not isinstance(password,str) or password!=confirmation:return "새 비밀번호 두 항목이 일치하는지 확인하세요."
    try:
        snap=REPO.get();email=email_address(email);person=snap["login"].get(email)
        if not email or not person or not ACCESS.rule(email)["active"]:return "이메일과 등록코드를 확인하세요."
        browser=session.get("birkman_browser","")
        if not browser:return "페이지를 새로 열어 주세요. HTTPS 주소인지 확인하세요."
        okay=ACCESS.enroll_password(email,txt(person["employee_id"]),code,password,browser)
        return "비밀번호를 설정했습니다. 위쪽 로그인 칸에 새 비밀번호를 입력하세요." if okay else "등록코드가 잘못되었거나 만료되었습니다. 담당자에게 확인하세요."
    except ValueError as exc:
        return "비밀번호는 15~128자로 입력하세요. 회사에서 쓰는 비밀번호와 다르게 정하세요."
    except Exception as exc:
        fail_log("enrollment",exc);return "등록을 완료하지 못했습니다. 담당자에게 문의하세요."


@app.callback([Output("activation-feedback","children"),Output("identity-confirmed","value")],
              [Input("issue-activation","n_clicks"),Input("admin-email","value"),Input("nav","value")],
              [State("identity-confirmed","value"),State("csrf","data")],prevent_initial_call=True)
def issue_activation(_click,email,nav,confirmed,csrf):
    # 선택 직원/메뉴가 바뀌면 이전 코드와 확인 체크를 즉시 지웁니다.
    event=callback_context.triggered[0]["prop_id"].split(".")[0]
    if event!="issue-activation":return "",[]
    if AUTH_MODE not in {"preview","password"} or not ACCESS or not csrf_ok(csrf):return "이 모드에서는 발급할 수 없습니다.",[]
    try:
        snap=REPO.get();p=principal(snap);email=email_address(email)
        if not p or not p["rule"]["admin"] or nav!="admin":return "관리자 권한이 필요합니다.",[]
        person=snap["login"].get(email)
        if not person or "confirmed" not in (confirmed or []):return "대상자를 선택하고 본인 확인 항목을 체크하세요.",[]
        token=ACCESS.issue_enrollment(p["email"],email,txt(person["employee_id"]))
        return html.Div([html.P(txt(person["성명"])+" · "+email),html.Div(token,className="enrollment-token"),
            html.P("등록코드만 대상자에게 직접 전달하세요. 발급 화면을 벗어나면 다시 표시하지 않습니다. 재발급하면 이전 코드는 폐기됩니다.",className="small")]),[]
    except (ValueError,PermissionError):return "대상자의 로그인 허용 여부와 관리자 권한을 확인하세요.",[]
    except Exception as exc:
        fail_log("enrollment_issue",exc);return "등록코드를 발급하지 못했습니다.",[]


@app.callback([Output("org","options"),Output("org","value"),Output("nav","options"),
               Output("admin-email","options"),Output("admin-scopes","options"),Output("user-label","children")],
              [Input("event","data"),Input("heartbeat","n_intervals")], [State("org","value")])
def choices(_event,_tick,selected):
    blank=([],None,[{"label":"팀·파트 보고서","value":"team"}],[],[],"")
    if not ACCESS:
        return blank
    try:
        snap=REPO.get()
        p=principal(snap)
        if not p:
            return blank
        keys=selectable_report_orgs(p,snap)
        options=[{"label":snap["orgs"][k]["org_path_full"]+" · "+("파트" if snap["orgs"][k]["org_level"]=="part" else "팀" if snap["orgs"][k]["org_level"]=="team" else "직속"),"value":k} for k in sorted(keys,key=lambda k:snap["orgs"][k]["org_path_full"])]
        if selected not in keys:
            preferred=txt(p["person"].get("team_key")) or txt(p["person"].get("part_key"))
            selected=preferred if preferred in keys else (options[0]["value"] if options else None)
        nav=[{"label":"팀·파트 보고서","value":"team"},{"label":"내 결과","value":"self"}]
        if leader_scopes(p,snap):
            nav.insert(1,{"label":"구성원 상세","value":"members"})
        admin_people,org_options=[],[]
        if p["rule"]["admin"]:
            nav.append({"label":"관리자","value":"admin"})
            admin_people=[{"label":"%s · %s · %s"%(r.get("성명",""),r.get("조직명",""),email),"value":email} for email,r in sorted(snap["login"].items(),key=lambda kv:txt(kv[1].get("성명")))]
            org_options=[{"label":o["org_path_full"]+" · "+("팀" if o["org_level"]=="team" else "파트"),"value":k} for k,o in sorted(snap["orgs"].items(),key=lambda kv:kv[1]["org_path_full"]) if o["org_level"] in {"team","part"}]
        label=html.Div([html.Strong(txt(p["person"].get("성명"))),html.Div("로그인 없이 확인 중 · 인사 권한" if AUTH_MODE == "preview" else ROLE_LABELS[p["rule"]["role"]],className="muted small")])
        return options,selected,nav,admin_people,org_options,label
    except Exception as exc:
        fail_log("choices",exc)
        return blank


@app.callback([Output("person","options"),Output("person","value")],
              [Input("org","value"),Input("event","data"),Input("heartbeat","n_intervals")], [State("person","value")])
def people_choices(key,_event,_tick,selected):
    if not ACCESS:
        return [],None
    try:
        snap=REPO.get()
        p=principal(snap)
        if not p or not can_detail(p,snap,key):
            return [],None
        rows=snap["members"].get(key,[])
        opts=[{"label":"%s · %s"%(txt(r.get("성명")),txt(r.get("직책명"))),"value":txt(r["employee_id"])} for r in rows]
        ids={o["value"] for o in opts}
        return opts,selected if selected in ids else (opts[0]["value"] if opts else None)
    except Exception as exc:
        fail_log("people_choices",exc)
        return [],None


@app.callback([Output("admin-role","value"),Output("admin-scopes","value"),Output("admin-flags","value"),Output("admin-revision","data")],
              [Input("admin-email","value"),Input("event","data")])
def load_rule(email,_event):
    blank=("member",[],["active"],0)
    if not ACCESS or not email:
        return blank
    try:
        snap=REPO.get()
        p=principal(snap)
        if not p or not p["rule"]["admin"] or email not in snap["login"]:
            return blank
        r=ACCESS.rule(email)
        flags_=(["active"] if r["active"] else [])+(["admin"] if r["admin"] else [])
        return r["role"],js(r["scopes"],[]),flags_,r["revision"]
    except Exception as exc:
        fail_log("load_rule",exc)
        return blank


@app.callback([Output("login-panel","style"),Output("app-panel","style"),Output("page","children"),
               Output("admin-panel","style"),Output("person-control","style"),Output("map-controls","style"),Output("access-table","children")],
              [Input("event","data"),Input("heartbeat","n_intervals"),Input("nav","value"),Input("org","value"),Input("person","value"),Input("map-kind","value")])
def render(_event,_tick,nav,key,eid,kind):
    hidden={"display":"none"}
    logged_out=({},hidden,[],hidden,hidden,hidden,[])
    if not ACCESS:
        return logged_out
    try:
        snap=REPO.get()
        p=principal(snap)
        if not p:
            return logged_out
        kind=kind if kind in MAPS else "usual"
        admin_table=[]
        if nav=="admin":
            if not p["rule"]["admin"]:
                page=empty("관리자 권한이 없습니다.")
                admin_style=hidden
            else:
                page=[]
                admin_style={}
                rows=[]
                for r in ACCESS.list_rules():
                    scope_names=[snap["orgs"][k]["org_path_full"] if k in snap["orgs"] else "현재 명부에 없는 조직 (재지정 필요)" for k in js(r["scopes"],[])]
                    rows.append(dict(email=r["email"],role=ROLE_LABELS.get(r["role"],r["role"]),scopes=" / ".join(scope_names) or ("전체" if r["role"]=="hr" else "본인 소속"),
                                     active="허용" if r["active"] else "중지",admin="관리자" if r["admin"] else "",modified=r["modified"]))
                admin_table=[card("등록한 권한",table([("email","이메일"),("role","역할"),("scopes","조회 조직"),("active","로그인"),("admin","관리"),("modified","변경")],rows)),
                             card("최근 로그인·권한 변경",table([("ts","시간"),("actor","실행자"),("action","작업"),("target","대상")],[dict(r,action={"login":"로그인","access_update":"권한 변경"}.get(r["action"],r["action"])) for r in ACCESS.recent_audit()]))]
            return hidden,{},page,admin_style,hidden,hidden,admin_table
        if nav=="self":
            page=personal_page(snap,p,p["eid"],kind)
        elif nav=="members":
            if not can_detail(p,snap,key) or not any(txt(m["employee_id"])==txt(eid) for m in snap["members"].get(key,[])):
                page=empty("구성원을 조회할 수 있는 조직을 선택하세요.")
            else:
                page=personal_page(snap,p,txt(eid),kind)
        else:
            page=org_page(snap,p,key,kind,leader=(nav=="leader"))
        return hidden,{},page,hidden,({"marginBottom":"20px"} if nav=="members" else hidden),hidden,[]
    except Exception as exc:
        fail_log("render",exc)
        # 자료 확인에 실패하면 이전 개인 보고서를 응답에 재사용하지 않습니다.
        if AUTH_MODE == "preview" or ACCESS.lookup_session(session.get("birkman_sid")):
            return hidden,{},empty("집계 자료를 준비 중입니다. 잠시 후 새로 읽기를 누르거나 담당자에게 문의해 주세요."),hidden,hidden,hidden,[]
        return logged_out


app.clientside_callback("""function(n){if(n && window.birkmanPrint){window.birkmanPrint();}return ''; }""",
                        Output("print-signal","children"),Input("print","n_clicks"),prevent_initial_call=True)
