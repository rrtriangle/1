"""DB 초기화와 계정 생성, 데모 데이터 적재.

  python -m hrli.seed --init --admin-email me@company.com   # 빈 DB + 첫 관리자
  python -m hrli.seed --demo                               # 가상 데이터와 시험용 사용자까지 적재
"""
from __future__ import annotations

import argparse

from . import auth, config, db, ingest, sample_data


DEMO_USERS = [("trainer@example.com", "trainer", None), ("analyst@example.com", "analyst", None),
              ("manager1@example.com", "org_manager", "1공장")]


def init(demo: bool = False, admin_email: str | None = None, admin_dss_login: str | None = None, path=None,
         quiet: bool = False) -> list[str]:
    """DB를 만들고 첫 시스템 관리자를 등록한다. demo=True면 가상 데이터와 시험용 사용자도 넣는다.

    admin_dss_login: 관리자의 Dataiku 로그인 ID. 넣어 두면 Dataiku 계정으로 웹앱에 자동 로그인된다.
    """
    db.init_db(path)
    accounts = [(admin_email or ("admin@example.com" if demo else None), "admin", None, admin_dss_login)]
    if demo:
        accounts += [(e, r, sc, None) for e, r, sc in DEMO_USERS]
    created = []
    for email, role, scope, login in accounts:
        if email and auth.get_user(email, path) is None:
            auth.save_user(email, role, scope, dss_login=login, path=path)
            created.append(f"{auth.norm(email)} ({config.ROLES[role]}"
                           + (f", Dataiku 로그인 ID {auth.norm(login)})" if login else ")"))
    if demo and db.read("SELECT 1 FROM upload_batch LIMIT 1", path=path).empty:
        files = sample_data.demo_files()
        for month, content in files["rosters"].items():
            b = ingest.stage(content, f"직원명부_{month}.xlsx", "roster", "full", "seed", month, path)
            ingest.approve(b, "seed", path)
        b = ingest.stage(files["training_full"], "교육이력_전체.xlsx", "training", "full", "seed", path=path)
        ingest.approve(b, "seed", path)
        # 마지막 달 증분 파일은 승인하지 않고 검토 대기 상태로 둔다(관리자 화면 시연용)
        ingest.stage(files["training_incremental"], f"교육이력_{files['last_month']}_증분.xlsx", "training",
                     "incremental", "seed", path=path)
        # 기획안 5.2 예시: 어학 교육 시간은 신뢰도가 낮아 시간 통계에서 제외(건수·인원에는 포함)
        with db.tx(path) as con:
            con.execute("INSERT INTO time_rule (scope, value, reason, created_by, created_at) VALUES"
                        " ('field','어학','어학 교육 시간은 임의 입력 가능성이 있어 시간 통계 제외','seed',?)", (db.now(),))
    if not quiet:
        print(f"DB: {path or config.DB_PATH}")
        for c in created:
            print(f"사용자 등록: {c}")
        if db.read("SELECT 1 FROM users WHERE role='admin' AND active=1", path=path).empty:
            print("주의: 시스템 관리자가 없습니다. --admin-email 로 관리자 이메일을 등록하세요.")
    return created


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--admin-email", help="첫 시스템 관리자의 이메일")
    ap.add_argument("--admin-dss-login", help="첫 시스템 관리자의 Dataiku 로그인 ID")
    a = ap.parse_args()
    init(demo=a.demo, admin_email=a.admin_email, admin_dss_login=a.admin_dss_login)
