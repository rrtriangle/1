"""이메일 인증 코드(OTP) 로그인과 역할별 접근 권한(기획안 10장).

- 관리자가 등록한 이메일만 로그인할 수 있다. 비밀번호는 없다.
- 인증 코드는 6자리, 일정 시간 뒤 만료되고, 정해진 횟수 이상 틀리면 폐기된다. DB에는 해시만 저장한다.
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
Mailer = Callable[[str, str, str], None]  # (받는 사람, 제목, 본문)


def norm(email: str | None) -> str:
    return (email or "").strip().lower()


def can(user: dict | None, perm: str) -> bool:
    return bool(user) and perm in PERMS.get(user["role"], set())


# ---------------------------------------------------------------- 사용자

def save_user(email: str, role: str, org_scope: str | None = None, name: str | None = None, active: bool = True,
              path=None) -> None:
    email = norm(email)
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
        con.execute("INSERT OR REPLACE INTO users (username, name, role, org_scope, active) VALUES (?,?,?,?,?)",
                    (email, name, role, org_scope if role == "org_manager" else None, int(active)))
        if not active:
            con.execute("DELETE FROM otp WHERE username=?", (email,))


def get_user(email: str, path=None) -> dict | None:
    rows = db.read("SELECT username, name, role, org_scope FROM users WHERE username=? AND active=1", (norm(email),), path)
    if rows.empty:
        return None
    r = rows.iloc[0]
    return {"username": r["username"], "name": r["name"], "role": r["role"], "org_scope": r["org_scope"]}


# ---------------------------------------------------------------- 인증 코드

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


def _hash(key: str, email: str, code: str) -> str:
    return hmac.new(key.encode(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def request_code(email: str, mailer: Mailer | None, path=None) -> str:
    """인증 코드를 만들어 메일로 보낸다. 반환값은 화면에 보여 줄 안내 문구다."""
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
