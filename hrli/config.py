"""환경 설정. 모든 값은 환경변수로 덮어쓸 수 있다."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(os.environ.get("HRLI_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = Path(os.environ.get("HRLI_DB_PATH", BASE_DIR / "hrli.sqlite3"))
RAW_DIR = Path(os.environ.get("HRLI_RAW_DIR", BASE_DIR / "raw"))

# 조직별 소수 인원 데이터 표시 기준(재식별 위험 방지). 이 인원 미만 집단의 비율·평균은 가린다.
MIN_GROUP_SIZE = int(os.environ.get("HRLI_MIN_GROUP_SIZE", "5"))

# 교육 일자 집계 기준 컬럼(기획안 14장: 시작일·종료일 중 확정 필요). 기본은 교육 시작일.
DATE_BASIS = os.environ.get("HRLI_DATE_BASIS", "start_date")

# 비정상 교육 시간 경고 기준(1건당 시간)
MAX_REASONABLE_HOURS = float(os.environ.get("HRLI_MAX_HOURS", "400"))

# AI 요약은 선택 기능이다. 켜지 않으면 Python 규칙 기반 요약만 제공한다.
AI_ENABLED = os.environ.get("HRLI_AI_ENABLED", "0") == "1"
AI_MODEL = os.environ.get("HRLI_AI_MODEL", "claude-opus-5-5")

FIELDS = ["직무", "어학", "소통", "문제해결", "리더십", "업무능력", "법정·공통", "미분류"]
OPERATIONS = ["사내", "사외", "미분류"]
METHODS = ["온라인", "오프라인", "혼합", "미분류"]
ROLES = {
    "admin": "시스템 관리자",
    "trainer": "교육 담당자",
    "analyst": "HR 분석 사용자",
    "org_manager": "조직 관리자",
}
