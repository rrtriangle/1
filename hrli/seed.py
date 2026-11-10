"""DB 초기화와 계정 생성, 데모 데이터 적재.

  python -m hrli.seed --init            # 빈 DB + 관리자 계정
  python -m hrli.seed --demo            # 가상 데이터까지 적재(검증·시연용)
"""
from __future__ import annotations

import argparse
import os
import secrets

from . import auth, config, db, ingest, sample_data


def _password(env: str) -> tuple[str, bool]:
    pw = os.environ.get(env)
    return (pw, False) if pw else (secrets.token_urlsafe(9), True)


def init(demo: bool = False, path=None, quiet: bool = False) -> dict:
    db.init_db(path)
    accounts = [("admin", "admin", None)]
    if demo:
        accounts += [("trainer", "trainer", None), ("analyst", "analyst", None), ("manager1", "org_manager", "1공장")]
    created = {}
    for name, role, scope in accounts:
        if db.read("SELECT 1 FROM users WHERE username=?", (name,), path).empty:
            pw, generated = _password(f"HRLI_{name.upper()}_PASSWORD")
            auth.create_user(name, pw, role, scope, path)
            created[name] = pw if generated else "(환경변수 값)"
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
        for name, pw in created.items():
            print(f"계정 생성: {name} / {pw}")
        if created:
            print("위 임시 비밀번호는 다시 표시되지 않습니다.")
    return created


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    init(demo=a.demo)
