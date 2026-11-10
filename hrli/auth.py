"""로그인과 역할별 접근 권한(기획안 10장).

로그인 방법은 두 가지다.
1) Dataiku 계정(기본): Dataiku에 로그인한 상태로 웹앱을 열면, 그 Dataiku 로그인 ID가 등록된 사용자일 때
   자동으로 들어간다. 접속자 확인은 Dataiku가 하므로 이 앱이 따로 인증 정보를 다루지 않는다.
2) 이메일 인증 코드(선택, 기본 꺼짐): Dataiku 계정이 없는 사람을 위한 방법이다. 시스템 관리자가 화면에서
   켜야 동작하고, 실제로 외부 사람이 접속하려면 DSS 관리자가 웹앱을 공개로 등록해야 한다.
   - 관리자가 등록한 이메일만 가능하고 비밀번호는 없다.
   - 6자리 코드는 1회용이며 일정 시간 뒤 만료되고, 정해진 횟수 이상 틀리면 폐기된다. DB에는 해시만 저장한다.
   - 등록되지 않은 이메일에도 같은 안내 문구를 돌려줘 등록 여부가 드러나지 않게 한다.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Callable

from . import config, db

# 역할별 허용 기능
PERMS = {
    "admin": {"analysis", "person", "download", "manage", "users"},
    "trainer": {"analysis", "person", "download", "manage"},
    "analyst": {"analysis", "person", "download"},
    "org_manager": {"analysis"},  # 권한이 부여된 소속 조직의 집계 현황만
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SENT_MSG = "등록된 이메일이면 인증 코드를 보냈습니다. 메일함을 확인하세요."
OTP_OFF_MSG = "이메일 인증 로그인이 꺼져 있습니다. 관리자에게 문의하세요."
Mailer = Callable[[str, str, str], None]          # (받는 사람, 제목, 본문)
Identify = Callable[[dict], "str | None"]         # 요청 헤더 → Dataiku 로그인 ID

MAILER: Mailer | None = None                      # 웹앱이 시작할 때 정해진다(attach)


def norm(email: str | None) -> str:
    return (email or "").strip().lower()


def can(user: dict | None, perm: str) -> bool:
    return bool(user) and perm in PERMS.get(user["role"], set())


# ---------------------------------------------------------------- 사용자

def save_user(email: str, role: str, org_scope: str | None = None, name: str | None = None, active: bool = True,
              dss_login: str | None = None, path=None) -> None:
    email, dss_login = norm(email), (norm(dss_login) or None)
    if not EMAIL_RE.match(email):
        raise ValueError("이메일 주소 형식이 올바르지 않습니다.")
    if role not in config.ROLES:
        raise ValueError(f"알 수 없는 역할: {role}")
    if role == "org_manager" and not org_scope:
        raise ValueError("조직 관리자는 조회 범위(그룹)를 지정해야 합니다.")
    with db.tx(path) as con:
        others = con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND username!=?",
                             (email,)).fetchone()[0]
        was_admin = con.execute("SELECT 1 FROM users WHERE username=? AND role='admin' AND active=1", (email,)).fetchone()
        if was_admin and others == 0 and (role != "admin" or not active):
            raise ValueError("마지막 시스템 관리자는 해제하거나 중지할 수 없습니다.")
        if dss_login and con.execute("SELECT 1 FROM users WHERE dss_login=? AND username!=?",
                                     (dss_login, email)).fetchone():
            raise ValueError(f"Dataiku 로그인 ID '{dss_login}'는 이미 다른 사용자에게 연결되어 있습니다.")
        con.execute("INSERT OR REPLACE INTO users (username, name, dss_login, role, org_scope, active)"
                    " VALUES (?,?,?,?,?,?)",
                    (email, name, dss_login, role, org_scope if role == "org_manager" else None, int(active)))
        if not active:
            con.execute("DELETE FROM otp WHERE username=?", (email,))


def _user(rows) -> dict | None:
    if rows.empty:
        return None
    r = rows.iloc[0]
    return {"username": r["username"], "name": r["name"], "dss_login": r["dss_login"], "role": r["role"],
            "org_scope": r["org_scope"]}


def get_user(email: str, path=None) -> dict | None:
    return _user(db.read("SELECT * FROM users WHERE username=? AND active=1", (norm(email),), path))


def get_user_by_login(dss_login: str | None, path=None) -> dict | None:
    if not dss_login:
        return None
    return _user(db.read("SELECT * FROM users WHERE dss_login=? AND active=1", (norm(dss_login),), path))


# ---------------------------------------------------------------- 설정

def get_setting(key: str, default: str | None = None, path=None) -> str | None:
    rows = db.read("SELECT value FROM settings WHERE key=?", (key,), path)
    return default if rows.empty else rows.iat[0, 0]


def set_setting(key: str, value: str, path=None) -> None:
    with db.tx(path) as con:
        con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))


def otp_enabled(path=None) -> bool:
    return get_setting("otp_enabled", "0", path) == "1"


def mail_ready() -> bool:
    return MAILER is not None or config.OTP_DEBUG


def set_otp_enabled(flag: bool, by: str, path=None) -> None:
    """이메일 인증 로그인 켜기/끄기. 메일 발송 방법이 없으면 켤 수 없다."""
    if flag and not mail_ready():
        raise ValueError("인증 메일을 보낼 방법이 설정되어 있지 않습니다(웹앱 코드의 mailer 설정).")
    set_setting("otp_enabled", "1" if flag else "0", path)
    if not flag:
        with db.tx(path) as con:
            con.execute("DELETE FROM otp")
    db.log_access(by, "이메일 인증 로그인 " + ("켜기" if flag else "끄기"), path=path)


def secret(path=None) -> str:
    """세션 서명과 코드 해시에 쓰는 서버 비밀값. 환경변수가 없으면 한 번 만들어 DB에 보관한다."""
    env = os.environ.get("HRLI_SECRET_KEY")
    if env:
        return env
    with db.tx(path) as con:
        row = con.execute("SELECT value FROM settings WHERE key='secret'").fetchone()
        if row:
            return row[0]
        value = secrets.token_hex(32)
        con.execute("INSERT INTO settings (key, value) VALUES ('secret', ?)", (value,))
        return value


# ---------------------------------------------------------------- Dataiku 계정 로그인

def dataiku_identify(headers: dict) -> str | None:
    """브라우저 요청 헤더(쿠키 포함)로 Dataiku에 로그인한 사용자를 확인한다.

    Dataiku가 서버 쪽에서 세션을 검증하므로 헤더를 꾸며서 다른 사람으로 들어올 수 없다.
    Dataiku 밖(로컬)이거나 Dataiku에 로그인하지 않은 상태면 None.
    """
    try:
        import dataiku
        info = dataiku.api_client().get_auth_info_from_browser_headers(headers)
    except Exception:  # noqa: BLE001 - 로그인하지 않았거나 Dataiku 밖이면 확인 불가로 본다
        return None
    return norm(info.get("authIdentifier")) or None


def default_identify() -> Identify | None:
    try:
        import dataiku  # noqa: F401
    except ImportError:
        return None
    return dataiku_identify


# ---------------------------------------------------------------- 이메일 인증 코드

def _hash(key: str, email: str, code: str) -> str:
    return hmac.new(key.encode(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def request_code(email: str, mailer: Mailer | None, path=None) -> str:
    """인증 코드를 만들어 메일로 보낸다. 반환값은 화면에 보여 줄 안내 문구다."""
    if not otp_enabled(path):
        return OTP_OFF_MSG
    email = norm(email)
    if not EMAIL_RE.match(email):
        return "이메일 주소를 정확히 입력하세요."
    if get_user(email, path) is None:
        db.log_access(email, "인증 코드 요청(미등록 이메일)", path=path)
        return SENT_MSG  # 등록 여부를 알려 주지 않는다
    now = datetime.now()
    code = f"{secrets.randbelow(10 ** 6):06d}"
    code_hash = _hash(secret(path), email, code)
    with db.tx(path) as con:
        old = con.execute("SELECT created_at FROM otp WHERE username=?", (email,)).fetchone()
        if old and (now - datetime.fromisoformat(old[0])).total_seconds() < config.OTP_RESEND_SECONDS:
            return f"방금 보낸 코드가 있습니다. {config.OTP_RESEND_SECONDS}초 뒤에 다시 요청하세요."
        con.execute("INSERT OR REPLACE INTO otp (username, code_hash, expires_at, created_at, attempts) VALUES (?,?,?,?,0)",
                    (email, code_hash, (now + timedelta(minutes=config.OTP_TTL_MINUTES)).isoformat(), now.isoformat()))
    body = (f"HR Learning Intelligence 로그인 인증 코드: {code}\n\n"
            f"{config.OTP_TTL_MINUTES}분 안에 입력하세요. 본인이 요청하지 않았다면 이 메일을 무시하세요.")
    try:
        if mailer is not None:
            mailer(email, "[HR Learning Intelligence] 로그인 인증 코드", body)
        elif config.OTP_DEBUG:
            print(f"[HRLI OTP DEBUG] {email} 인증 코드: {code}", flush=True)
        else:
            raise RuntimeError("메일 발송 설정이 없습니다")
    except Exception as e:  # noqa: BLE001 - 발송 실패 사유는 로그에만 남긴다
        with db.tx(path) as con:
            con.execute("DELETE FROM otp WHERE username=?", (email,))
        db.log_access(email, "인증 코드 발송 실패", str(e)[:200], path=path)
        return "인증 코드를 보내지 못했습니다. 관리자에게 문의하세요."
    db.log_access(email, "인증 코드 발송", path=path)
    return SENT_MSG


def verify_code(email: str, code: str, path=None) -> dict | None:
    """코드가 맞으면 사용자 정보를 돌려주고 코드를 폐기한다(1회용)."""
    if not otp_enabled(path):
        return None
    email, code = norm(email), re.sub(r"\s", "", code or "")
    user = get_user(email, path)
    if user is None or not code:
        return None
    expected = _hash(secret(path), email, code)
    with db.tx(path) as con:
        row = con.execute("SELECT code_hash, expires_at, attempts FROM otp WHERE username=?", (email,)).fetchone()
        if row is None:
            return None
        if datetime.now() > datetime.fromisoformat(row["expires_at"]) or row["attempts"] >= config.OTP_MAX_ATTEMPTS:
            con.execute("DELETE FROM otp WHERE username=?", (email,))
            return None
        if not hmac.compare_digest(row["code_hash"], expected):
            con.execute("UPDATE otp SET attempts=attempts+1 WHERE username=?", (email,))
            return None
        con.execute("DELETE FROM otp WHERE username=?", (email,))
    return user


# ---------------------------------------------------------------- 메일 발송 방법

def smtp_mailer(host: str, sender: str, port: int = 25, username: str | None = None, password: str | None = None,
                starttls: bool = False) -> Mailer:
    """사내 SMTP 서버로 보내는 발송 함수를 만든다."""
    def send(to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = sender, to, subject
        msg.set_content(body)
        with smtplib.SMTP(host, port, timeout=15) as s:
            if starttls:
                s.starttls()
            if username:
                s.login(username, password or "")
            s.send_message(msg)
    return send


def mailer_from_env() -> Mailer | None:
    host = os.environ.get("HRLI_SMTP_HOST")
    if not host:
        return None
    return smtp_mailer(host, os.environ.get("HRLI_SMTP_FROM", "no-reply@localhost"),
                       int(os.environ.get("HRLI_SMTP_PORT", "25")), os.environ.get("HRLI_SMTP_USER"),
                       os.environ.get("HRLI_SMTP_PASSWORD"), os.environ.get("HRLI_SMTP_TLS", "0") == "1")


def dataiku_mailer(channel_id: str, project_key: str) -> Mailer:
    """Dataiku에 관리자가 설정해 둔 메일 채널로 보내는 발송 함수(DSS 12.6 이상)."""
    def send(to: str, subject: str, body: str) -> None:
        import dataiku
        channel = dataiku.api_client().get_messaging_channel(channel_id)
        channel.send(project_key, [to], subject, body.replace("\n", "<br>"))
    return send
