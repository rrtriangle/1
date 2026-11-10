"""사용자 인증과 역할별 접근 권한(기획안 10장)."""
from __future__ import annotations

from werkzeug.security import check_password_hash, generate_password_hash

from . import config, db

# 역할별 허용 기능
PERMS = {
    "admin": {"analysis", "person", "download", "manage", "users"},
    "trainer": {"analysis", "person", "download", "manage"},
    "analyst": {"analysis", "person", "download"},
    "org_manager": {"analysis"},  # 권한이 부여된 소속 조직의 집계 현황만
}


def create_user(username: str, password: str, role: str, org_scope: str | None = None, path=None) -> None:
    if not username or not username.strip():
        raise ValueError("아이디를 입력하세요.")
    if role not in config.ROLES:
        raise ValueError(f"알 수 없는 역할: {role}")
    if len(password) < 8:
        raise ValueError("비밀번호는 8자 이상이어야 합니다.")
    if role == "org_manager" and not org_scope:
        raise ValueError("조직 관리자는 조회 범위(그룹)를 지정해야 합니다.")
    with db.tx(path) as con:
        con.execute("INSERT OR REPLACE INTO users (username, pw_hash, role, org_scope, active) VALUES (?,?,?,?,1)",
                    (username, generate_password_hash(password), role, org_scope))


def verify(username: str, password: str, path=None) -> dict | None:
    rows = db.read("SELECT * FROM users WHERE username=? AND active=1", (username or "",), path)
    if rows.empty or not check_password_hash(rows.at[0, "pw_hash"], password or ""):
        return None
    r = rows.iloc[0]
    return {"username": r["username"], "role": r["role"], "org_scope": r["org_scope"]}


def can(user: dict | None, perm: str) -> bool:
    return bool(user) and perm in PERMS.get(user["role"], set())
