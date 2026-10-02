# -*- coding: utf-8 -*-
"""
==============================================================================
[Dataiku Dash 웹앱] 버크만 팀 리포트 (사내 메일 인증번호 로그인)
==============================================================================

이 웹앱이 하는 일
  1) 사내 메일을 입력하면 6자리 인증번호를 메일로 보내고, 맞게 입력하면 로그인시킵니다.
  2) 로그인한 사람의 직책(조직 엑셀 기준)에 따라 볼 수 있는 범위를 정합니다.
  3) 그 범위의 팀원들을 버크만 맵 / 조직지향점 중심으로 분석해 보여줍니다.

입력 데이터셋
  - birkman_scores : 01_recipe_parse_birkman.py 의 결과 (1인 1행)
  - org_members    : 조직 정보 엑셀을 업로드한 데이터셋 (사번, 이름, 이메일, 팀, 파트, 직책)

볼 수 있는 범위 (권한)
  - 팀장   : 자기 팀 전체 + 팀 안의 파트별 보기
  - 파트장 : 자기 파트만
  - 팀원   : 본인 결과만 (ALLOW_MEMBER_SELF_VIEW = True 일 때)
  - ADMIN_EMAILS 에 적은 사람 : 전체 팀

화면(탭) 구성
  팀 개요 / 버크만 맵 / 조직지향점 / 구성요소(시그니처) / 개인별 / 직업 흥미(참고)

사용 방법
  * Dataiku: 웹앱 > Dash > Python 탭에 이 파일 전체를 붙여넣고 백엔드를 시작합니다.
  * PC 테스트:  BIRKMAN_LOCAL_DIR=<csv 폴더> python 02_webapp_dash.py  (인증번호가 화면에 표시됨)

파일 구성 (번호 = 아래 섹션 번호)
  1. 설정            : 도메인, 직책명, 메일 발송 방식 등 (보통 여기만 수정)
  2. 해석 사전        : 색/구성요소/조직지향점별 설명 문구 (문구 수정은 여기서)
  3. 데이터 읽기      : 데이터셋 로딩, 조직 정보와 버크만 결과 연결
  4. 로그인(인증)     : 인증번호 발급/확인, 로그인 토큰
  5. 권한            : 누가 어떤 팀/파트를 볼 수 있는지
  6. 분석            : 팀 해석 문장 자동 생성
  7. 차트            : 버크만 맵, 막대/히트맵
  8. 화면 구성        : 로그인 화면, 메인 화면 배치
  9. 동작(콜백)       : 버튼/선택에 반응해 화면을 그리는 함수들
"""
from __future__ import print_function

import base64
import hashlib
import hmac
import math
import os
import re
import secrets
import smtplib
import threading
import time
from email.header import Header
from email.mime.text import MIMEText

import pandas as pd
import plotly.graph_objects as go

import dash

# Dash 버전에 따라 import 위치가 다르므로 둘 다 지원 (Dash 2.x 이상 / 1.x)
try:
    from dash import dcc, html, Input, Output, State, no_update, callback_context
except ImportError:  # Dash 1.x
    import dash_core_components as dcc
    import dash_html_components as html
    from dash.dependencies import Input, Output, State
    from dash import no_update, callback_context

# Dataiku 안에서 실행 중인지 확인 (PC 테스트일 때는 CSV 파일을 읽고, 메일 대신 화면에 인증번호 표시)
try:
    import dataiku
    IN_DSS = True
except ImportError:
    IN_DSS = False

# =============================================================================
# 1. 설정 (보통은 여기만 수정하면 됩니다)
# =============================================================================
# --- 데이터셋 이름 (Dataiku 프로젝트 안의 이름과 같아야 함) ---
SCORES_DATASET = "birkman_scores"   # 레시피 결과
ORG_DATASET = "org_members"         # 조직 정보 엑셀 업로드

# --- 로그인 허용 메일 도메인 ---
# 이 도메인의 메일만 인증번호를 요청할 수 있습니다. 여러 개면 ["dwchem.co.kr", "other.co.kr"] 처럼 추가.
# (비워 두면 [] 조직 엑셀에 있는 메일이면 도메인과 관계없이 허용)
ALLOWED_EMAIL_DOMAINS = ["dwchem.co.kr"]
if not IN_DSS:
    # PC 테스트용 가상 데이터(tools/make_sample_data.py)의 메일 도메인. Dataiku 에서는 적용되지 않음
    ALLOWED_EMAIL_DOMAINS = ALLOWED_EMAIL_DOMAINS + ["dongwoo.example.com", "company.com"]

# --- 전체 팀을 볼 수 있는 관리자 메일 (HR 담당자 등) ---
# 예: ADMIN_EMAILS = ["hong.gildong@dwchem.co.kr", "kim.chulsoo@dwchem.co.kr"]
ADMIN_EMAILS = []

# --- 직책명 ---
# 조직 엑셀의 '직책' 칸에 적힌 값과 정확히 같아야 합니다. (앞뒤 공백은 자동 제거)
# 직책명을 추가/수정하려면 아래 목록에 넣으세요.
#   예) 그룹장·실장도 팀장처럼 '자기 팀 전체'를 보게 하려면:  TEAM_LEADER_ROLES = ["팀장", "그룹장", "실장"]
#   예) 셀장도 파트장처럼 '자기 파트'를 보게 하려면:          PART_LEADER_ROLES = ["파트장", "셀장"]
# 두 목록에 없는 직책(팀원, 사원, 책임 등)은 모두 '팀원'으로 취급합니다.
TEAM_LEADER_ROLES = ["팀장"]        # 자기 팀 전체를 볼 수 있는 직책
PART_LEADER_ROLES = ["파트장"]      # 자기 파트만 볼 수 있는 직책
ALLOW_MEMBER_SELF_VIEW = True       # True: 팀원은 로그인하면 본인 결과만 봄 / False: 팀원은 로그인 불가

# --- 인증번호 메일 발송 방식 ---
#   "auto"        : Dataiku 메일 채널을 먼저 시도하고, 안 되면 아래 SMTP 설정으로 시도 (권장)
#   "dss_channel" : Dataiku 관리자 화면에 등록된 메일 채널만 사용
#   "smtp"        : 아래 SMTP 서버로 직접 발송
#   "console"     : 메일을 보내지 않고 로그에만 출력 (테스트용, 운영 금지)
MAIL_MODE = "auto"
DSS_MAIL_CHANNEL_ID = ""            # Dataiku 메일 채널 ID. 비우면 등록된 첫 번째 메일 채널 사용
SMTP_HOST = ""                      # 사내 메일 서버 주소. 예: "smtp.dwchem.co.kr" (IT 부서 확인)
SMTP_PORT = 25                      # 보통 25 (사내망), 587 (TLS)
SMTP_USE_TLS = False                # 587 포트면 보통 True
SMTP_USER = ""                      # 로그인이 필요한 서버면 계정
SMTP_PASSWORD = ""                  # 코드에 적지 말고 프로젝트 변수 smtp_password 사용 권장
MAIL_FROM = ""                      # 보내는 사람 주소. 예: "noreply@dwchem.co.kr"
MAIL_SUBJECT = "[버크만 팀 리포트] 로그인 인증번호"

# --- 보안 관련 ---
OTP_TTL_SEC = 300                   # 인증번호 유효시간 (초). 300 = 5분
OTP_MAX_TRY = 5                     # 인증번호 입력 허용 횟수 (넘으면 무효)
OTP_RESEND_SEC = 60                 # 인증번호 재요청 대기시간 (초)
OTP_MAX_PER_HOUR = 5                # 메일 주소 1개당 1시간에 보낼 수 있는 최대 횟수
SESSION_HOURS = 8                   # 로그인 유지 시간. 브라우저 탭을 닫으면 바로 로그아웃
DATA_CACHE_SEC = 300                # 데이터셋을 다시 읽는 주기 (초). 레시피를 다시 돌리면 최대 5분 뒤 반영

# --- 점수 해석 기준 (구성요소 0~100 점수) ---
HIGH, LOW = 60, 40                  # 60 이상 = 높음, 40 이하 = 낮음, 그 사이 = 중간
GAP_ALERT = 35                      # 평소와 욕구 점수 차이가 이 이상이면 "보이는 모습과 필요가 다름" 표시

# =============================================================================
# 2. 버크만 해석 사전 (사내 교육용 요약. 공식 해석은 원 레포트를 참고)
#    화면에 나오는 설명 문구는 모두 여기서 고칠 수 있습니다.
# =============================================================================
# 사분면 색 순서 / 화면 색상 / 맵 배경 색
COLOR_ORDER = ["red", "green", "yellow", "blue"]
COLOR_HEX = {"red": "#D64545", "green": "#2E9E5B", "yellow": "#E0A800", "blue": "#2F6FD0"}
COLOR_BG = {"red": "rgba(214,69,69,0.10)", "green": "rgba(46,158,91,0.10)",
            "yellow": "rgba(224,168,0,0.12)", "blue": "rgba(47,111,208,0.10)"}
# 색(사분면)별 설명
#   ko=이름, nick=별칭, axis=맵 위치, interest/usual/needs/stress=기호별 설명,
#   absent=팀에 그 색이 없을 때의 주의점, tip=그 욕구를 가진 사람과 일하는 팁
COLOR_INFO = {
    "red": {
        "ko": "빨강", "nick": "실행가", "axis": "외향 · 과제지향",
        "interest": "직접 만들고 고치며 눈에 보이는 결과를 내는 실무·현장형 활동에 끌립니다.",
        "usual": "결과와 실행을 중시합니다. 빠르게 결정하고 직접 움직이며, 실용적이고 구체적인 해법을 선호합니다.",
        "needs": "명확한 목표와 권한, 눈에 보이는 성과, 군더더기 없는 지시가 있을 때 힘이 납니다.",
        "stress": "욕구가 채워지지 않으면 성급해지고, 고압적이거나 독단적으로 보일 수 있습니다.",
        "absent": "실행력·추진력이 약할 수 있습니다. 논의가 길어지고 결정이 늦어지지 않도록 마감과 책임자를 분명히 하세요.",
        "tip": "목표·기한·권한을 짧고 명확하게 주고, 결과로 인정해 주세요.",
    },
    "green": {
        "ko": "초록", "nick": "소통가", "axis": "외향 · 사람지향",
        "interest": "사람을 만나고 설득·협상·교육하는 활동에 끌립니다.",
        "usual": "사람들과 적극적으로 소통하고 설득합니다. 열정적이고 경쟁적이며 분위기를 이끕니다.",
        "needs": "사람들과 교류할 기회, 인정과 피드백, 자신의 영향력을 발휘할 무대가 필요합니다.",
        "stress": "욕구가 채워지지 않으면 말이 많아지거나 산만해지고, 지나치게 밀어붙이거나 감정적으로 반응할 수 있습니다.",
        "absent": "대외 소통·설득·동기부여가 약할 수 있습니다. 성과를 알리고 이해관계자를 설득하는 역할을 의도적으로 맡기세요.",
        "tip": "아이디어를 말할 기회를 주고, 공개적으로 인정해 주세요. 협업·대외 업무에 강점이 있습니다.",
    },
    "yellow": {
        "ko": "노랑", "nick": "관리자·분석가", "axis": "내향 · 과제지향",
        "interest": "데이터·숫자·문서를 다루고 체계를 세우는 활동에 끌립니다.",
        "usual": "체계적이고 꼼꼼합니다. 절차와 데이터를 중시하고, 일정과 품질을 안정적으로 관리합니다.",
        "needs": "명확한 규칙과 절차, 예측 가능한 일정, 정확한 정보가 있을 때 안정감을 느낍니다.",
        "stress": "욕구가 채워지지 않으면 경직되고 비판적이 되며, 세부사항에 과도하게 집착할 수 있습니다.",
        "absent": "절차·디테일·리스크 관리가 소홀해질 수 있습니다. 체크리스트, 리뷰 단계, 문서화를 제도로 보완하세요.",
        "tip": "변경 사항은 미리 알리고, 기준과 절차를 문서로 주세요. 품질·관리 업무에 강점이 있습니다.",
    },
    "blue": {
        "ko": "파랑", "nick": "기획가·사색가", "axis": "내향 · 사람지향",
        "interest": "아이디어를 내고 기획·연구·창작하는 활동에 끌립니다.",
        "usual": "창의적이고 깊이 생각합니다. 장기적인 관점에서 아이디어를 내고, 의미와 가능성을 탐색합니다.",
        "needs": "충분히 생각할 시간, 지지적인 분위기, 일의 의미와 큰 그림에 대한 공유가 필요합니다.",
        "stress": "욕구가 채워지지 않으면 결정을 미루거나 위축되고, 비관적으로 생각하거나 혼자 고민할 수 있습니다.",
        "absent": "장기 전략·새로운 아이디어가 부족할 수 있습니다. 정기적으로 '왜/앞으로'를 논의하는 시간을 확보하세요.",
        "tip": "결정 전에 생각할 시간을 주고, 일의 배경과 의미를 설명해 주세요. 기획·개선 아이디어에 강점이 있습니다.",
    },
}
# 버크만 맵의 4가지 기호: 이름, 맵에서의 모양, 설명
LAYERS = ["interest", "usual", "needs", "stress"]
LAYER_INFO = {
    "interest": {"ko": "흥미", "symbol": "star", "desc": "하고 싶어하는 일·활동"},
    "usual": {"ko": "평소 행동", "symbol": "diamond", "desc": "다른 사람에게 보이는 평상시 모습"},
    "needs": {"ko": "욕구", "symbol": "circle", "desc": "최고의 모습을 내기 위해 필요한 환경·대우"},
    "stress": {"ko": "스트레스", "symbol": "square", "desc": "욕구가 채워지지 않을 때 나타나는 행동"},
}

# 구성요소 11개 (시그니처). 각 줄의 의미:
# (내부키, 한글명, 평소 점수 높을 때 설명, 평소 낮을 때, 욕구 높을 때 필요한 것, 욕구 낮을 때 필요한 것)
COMPONENTS = [
    ("social_energy", "사회적 에너지",
     "사교적이고 여럿이 함께하는 활동을 즐김", "독립적이고 소수와의 깊은 관계를 선호",
     "사람들과 어울리고 소속감을 느낄 시간", "혼자 집중할 수 있는 시간과 공간"),
    ("physical_energy", "신체적 에너지",
     "빠른 템포로 바쁘게 움직임", "신중하게 자기 페이스로 일함",
     "바쁜 일정과 몸을 움직일 기회", "스스로 속도를 조절할 여유"),
    ("emotional_energy", "감정적 에너지",
     "감정 표현이 풍부하고 공감적", "객관적이고 감정을 절제",
     "감정적 지지와 공감", "사실 중심의 담백한 소통"),
    ("self_consciousness", "자의식",
     "상대의 감정을 배려하며 조심스럽게 말함", "직설적이고 솔직하게 말함",
     "개별적인 인정, 비공개 피드백", "직설적이고 명확한 피드백"),
    ("assertiveness", "주장성",
     "의견을 강하게 주장하고 주도함", "부드럽게 제안하고 경청함",
     "확고하고 분명한 리더십", "부드럽고 민주적인 방향 제시"),
    ("insistence", "고집성(구조)",
     "체계·절차·일관성을 중시", "유연하고 즉흥적으로 대응",
     "명확한 규칙, 절차, 계획", "유연성과 재량"),
    ("incentives", "인센티브",
     "경쟁적이고 개인 성과를 추구", "팀·공익을 중시하고 협력적",
     "개인 성과에 대한 분명한 보상", "공정함과 팀 차원의 보상"),
    ("restlessness", "변화",
     "여러 일을 병행하며 변화를 즐김", "한 가지에 꾸준히 집중",
     "다양한 업무와 변화", "방해받지 않는 집중 환경과 루틴"),
    ("thought", "사고",
     "결정 전 여러 각도로 숙고", "빠르고 단호하게 결정",
     "결정 전 충분한 검토 시간", "빠른 결론과 실행"),
    ("autonomy", "자율성",
     "독자적이고 개성 있게 행동", "관습과 기대를 따르며 예측 가능",
     "재량과 자율", "명확한 기대와 일관된 환경"),
    ("challenge", "도전",
     "높은 목표에 도전하고 자기 이미지를 중시", "현실적 목표를 세우고 과정 자체를 중시",
     "도전적 목표와 성취 기회", "현실적이고 달성 가능한 목표"),
]
COMP_LABEL = {c[0]: c[1] for c in COMPONENTS}

# 버크만 조직지향점 4개 영역 (레포트 표기에 맞게 수정 가능)
ORGFOCUS = [("red", "운영/기술"), ("green", "영업/마케팅"),
            ("yellow", "관리/회계"), ("blue", "디자인/전략")]
# 직업 흥미 10개 영역 (버크만 코리아 레포트 표기)
JOBINT = [("numerical", "숫자"), ("clerical", "관리"), ("scientific", "과학"), ("literary", "문학"),
          ("mechanical", "기술"), ("musical", "음악"), ("artistic", "예술"), ("outdoor", "야외"),
          ("persuasive", "설득"), ("social_service", "사회복지")]

# =============================================================================
# 3. 데이터 로딩
# =============================================================================
# 조직 엑셀의 컬럼 이름으로 인정하는 표기 (한글/영문). 엑셀 머리글이 다르면 여기에 추가
ORG_COL_ALIASES = {
    "emp_id": ["emp_id", "사번", "사원번호", "직원번호", "employee_id", "empno"],
    "name": ["name", "이름", "성명"],
    "email": ["email", "이메일", "메일", "e-mail", "mail"],
    "team": ["team", "팀", "팀명", "부서", "부서명"],
    "part": ["part", "파트", "파트명", "셀"],
    "role": ["role", "직책", "보직"],
}
# 읽어 둔 데이터 보관소 (t = 마지막으로 읽은 시각)
_cache = {"t": 0, "org": None, "scores": None}
_cache_lock = threading.Lock()


def _read(name):
    """데이터셋을 pandas 표로 읽음. Dataiku 에서는 데이터셋, PC 테스트에서는 BIRKMAN_LOCAL_DIR 의 CSV"""
    if IN_DSS:
        return dataiku.Dataset(name).get_dataframe(infer_with_pandas=False)
    d = os.environ.get("BIRKMAN_LOCAL_DIR", ".")
    return pd.read_csv(os.path.join(d, name + ".csv"), dtype=str, encoding="utf-8-sig")


def _norm_org(df):
    """조직 엑셀의 컬럼 이름(한글/영문)을 표준 이름(emp_id, name, email, team, part, role)으로 바꾸고 값의 공백을 정리"""
    rename = {}
    lower = {str(c).strip().lower(): c for c in df.columns}
    for std, aliases in ORG_COL_ALIASES.items():
        for a in aliases:
            if a.lower() in lower:
                rename[lower[a.lower()]] = std
                break
    df = df.rename(columns=rename)
    for c in ORG_COL_ALIASES:
        if c not in df.columns:
            df[c] = ""
        df[c] = df[c].fillna("").astype(str).str.strip()
    df["email"] = df["email"].str.lower()
    df["emp_id"] = df["emp_id"].str.replace(r"\.0$", "", regex=True)
    return df[df["email"] != ""].drop_duplicates("email")


def load_data(force=False):
    """조직 정보와 버크만 결과를 읽어 메모리에 보관. DATA_CACHE_SEC 마다 새로 읽음 (레시피 재실행 반영)"""
    with _cache_lock:
        if force or _cache["org"] is None or time.time() - _cache["t"] > DATA_CACHE_SEC:
            org = _norm_org(_read(ORG_DATASET))
            sc = _read(SCORES_DATASET)
            for c in sc.columns:
                if c.startswith(("map_", "orgfocus_", "comp_", "jobint_")) and not c.endswith(("_color", "_bullets")):
                    sc[c] = pd.to_numeric(sc[c], errors="coerce")
            for c in ("emp_id", "email", "name"):
                if c in sc.columns:
                    sc[c] = sc[c].fillna("").astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
            sc["email"] = sc["email"].str.lower()
            _cache.update(t=time.time(), org=org, scores=sc)
        return _cache["org"], _cache["scores"]


def members_with_scores(org_rows):
    """조직 행에 버크만 점수 붙이기: 사번 -> 이메일 -> (동명이인 없을 때) 이름 순으로 매칭"""
    _, sc = load_data()
    by_id = {r["emp_id"]: r for _, r in sc.iterrows() if r.get("emp_id")}
    by_mail = {r["email"]: r for _, r in sc.iterrows() if r.get("email")}
    name_counts = sc["name"].value_counts() if "name" in sc.columns else {}
    by_name = {r["name"]: r for _, r in sc.iterrows() if r.get("name") and name_counts.get(r["name"], 0) == 1}
    rows = []
    for _, m in org_rows.iterrows():
        s = by_id.get(m["emp_id"]) if m["emp_id"] else None
        if s is None:
            s = by_mail.get(m["email"])
        if s is None:
            s = by_name.get(m["name"])
        row = m.to_dict()
        row["has_birkman"] = s is not None
        if s is not None:
            for c, v in s.items():
                if c not in ("emp_id", "name", "email"):
                    row[c] = v
        rows.append(row)
    return pd.DataFrame(rows)


# =============================================================================
# 4. 인증 (OTP + 서명 토큰)
# =============================================================================
def _secret():
    """로그인 토큰 서명용 비밀키. 프로젝트 변수 birkman_app_secret 이 있으면 사용(재시작해도 로그인 유지),
    없으면 실행할 때마다 새로 만듦(웹앱 재시작 시 전원 로그아웃)
    """
    if IN_DSS:
        try:
            v = dataiku.get_custom_variables().get("birkman_app_secret")
            if v:
                return v.encode("utf-8")
        except Exception:  # noqa
            pass
    return secrets.token_bytes(32)  # 웹앱 재시작 시 전원 로그아웃


APP_SECRET = _secret()
_otp = {}                       # 메일별 인증번호 상태 {메일: {hash, exp(만료시각), tries(입력횟수), sent(발송시각 목록)}}
_otp_lock = threading.Lock()    # 여러 사람이 동시에 요청해도 꼬이지 않게 잠금
_revoked = set()                # 로그아웃한 토큰 목록 (다시 쓸 수 없게)


def audit(event, email, detail=""):
    """열람/로그인 기록을 웹앱 로그에 남김. Dataiku 웹앱 화면의 Log 탭에서 [AUDIT] 로 검색"""
    print("[AUDIT] %s | %s | %s | %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), event, email, detail))


def _hash_code(email, code):
    """인증번호를 그대로 저장하지 않고 해시(복원 불가한 값)로 바꿔 보관"""
    return hmac.new(APP_SECRET, ("%s|%s" % (email, code)).encode("utf-8"), hashlib.sha256).hexdigest()


def find_user(email):
    """메일 주소로 조직 엑셀에서 사람을 찾음. 없으면 None"""
    org, _ = load_data()
    r = org[org["email"] == email]
    return None if r.empty else r.iloc[0].to_dict()


def email_allowed(email):
    """메일 형식과 허용 도메인(ALLOWED_EMAIL_DOMAINS) 확인"""
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""):
        return False
    if ALLOWED_EMAIL_DOMAINS and email.split("@")[1] not in [d.lower() for d in ALLOWED_EMAIL_DOMAINS]:
        return False
    return True


def _smtp_password():
    """SMTP 비밀번호: 코드의 SMTP_PASSWORD -> 프로젝트 변수 smtp_password 순서로 사용"""
    if SMTP_PASSWORD:
        return SMTP_PASSWORD
    if IN_DSS:
        try:
            return dataiku.get_custom_variables().get("smtp_password", "")
        except Exception:  # noqa
            return ""
    return ""


def send_mail(to, subject, body):
    """인증번호 메일 발송. MAIL_MODE 에 따라 Dataiku 메일 채널 또는 SMTP 로 보냄.
    반환: (성공여부, 사용한 방식 또는 실패 이유)
    """
    modes = ["dss_channel", "smtp"] if MAIL_MODE == "auto" else [MAIL_MODE]
    if not IN_DSS and MAIL_MODE == "auto":
        modes = ["console"]
    errors = []
    for mode in modes:
        try:
            if mode == "dss_channel":
                client = dataiku.api_client()
                ch_id = DSS_MAIL_CHANNEL_ID
                if not ch_id:
                    chans = client.list_messaging_channels(as_type="objects", channel_family="mail")
                    if not chans:
                        raise RuntimeError("DSS 에 메일 채널이 없습니다")
                    ch = chans[0]
                else:
                    ch = client.get_messaging_channel(ch_id)
                ch.send(dataiku.default_project_key(), [to], subject, body, plain_text=True)
            elif mode == "smtp":
                if not SMTP_HOST:
                    raise RuntimeError("SMTP_HOST 미설정")
                msg = MIMEText(body, "plain", "utf-8")
                msg["Subject"] = Header(subject, "utf-8")
                msg["From"] = MAIL_FROM or SMTP_USER
                msg["To"] = to
                s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
                try:
                    if SMTP_USE_TLS:
                        s.starttls()
                    if SMTP_USER:
                        s.login(SMTP_USER, _smtp_password())
                    s.sendmail(msg["From"], [to], msg.as_string())
                finally:
                    s.quit()
            elif mode == "console":
                print("[MAIL-CONSOLE] to=%s\n%s" % (to, body))
            return True, mode
        except Exception as e:  # noqa
            errors.append("%s: %s" % (mode, e))
    print("[MAIL-ERROR] " + " | ".join(errors))
    return False, " | ".join(errors)


def request_otp(email):
    """반환: (사용자에게 보여줄 메시지, 개발용 코드 or None)"""
    generic = "등록된 사내 메일이면 인증번호를 보냈습니다. 메일함(스팸함 포함)을 확인해 주세요."
    if not email_allowed(email):
        return "사내 메일 주소 형식을 확인해 주세요.", None
    user = find_user(email)
    now = time.time()
    with _otp_lock:
        st = _otp.get(email, {"sent": []})
        st["sent"] = [t for t in st.get("sent", []) if now - t < 3600]
        if st["sent"] and now - st["sent"][-1] < OTP_RESEND_SEC:
            return "잠시 후 다시 요청해 주세요. (%d초)" % int(OTP_RESEND_SEC - (now - st["sent"][-1])), None
        if len(st["sent"]) >= OTP_MAX_PER_HOUR:
            return "요청이 너무 많습니다. 1시간 후 다시 시도해 주세요.", None
        st["sent"].append(now)
        _otp[email] = st
        if user is None:
            audit("OTP_UNKNOWN_EMAIL", email)
            return generic, None
        if not is_authorized_user(user):
            audit("OTP_NO_PERMISSION", email)
            return "열람 권한이 없는 계정입니다. (팀장/파트장 대상)", None
        code = "%06d" % secrets.randbelow(1000000)
        st.update(hash=_hash_code(email, code), exp=now + OTP_TTL_SEC, tries=0)
    body = ("버크만 팀 리포트 로그인 인증번호입니다.\n\n    %s\n\n%d분 안에 입력해 주세요. "
            "본인이 요청하지 않았다면 이 메일을 무시하세요." % (code, OTP_TTL_SEC // 60))
    ok, info = send_mail(email, MAIL_SUBJECT, body)
    audit("OTP_SENT" if ok else "OTP_SEND_FAIL", email, info)
    if not ok:
        return "메일 발송에 실패했습니다. 관리자에게 문의해 주세요.", None
    return generic, (code if info == "console" else None)


def verify_otp(email, code):
    """'로그인' 버튼 처리: 유효시간, 입력 횟수, 번호 일치 확인. 한 번 맞히면 그 번호는 폐기"""
    now = time.time()
    with _otp_lock:
        st = _otp.get(email)
        if not st or "hash" not in st:
            return False, "먼저 인증번호를 요청해 주세요."
        if now > st["exp"]:
            st.pop("hash", None)
            return False, "인증번호가 만료되었습니다. 다시 요청해 주세요."
        st["tries"] += 1
        if st["tries"] > OTP_MAX_TRY:
            st.pop("hash", None)
            audit("OTP_LOCKED", email)
            return False, "입력 횟수를 초과했습니다. 다시 요청해 주세요."
        if not hmac.compare_digest(st["hash"], _hash_code(email, (code or "").strip())):
            audit("OTP_WRONG", email, "try=%d" % st["tries"])
            return False, "인증번호가 올바르지 않습니다. (%d/%d)" % (st["tries"], OTP_MAX_TRY)
        st.pop("hash", None)
    audit("LOGIN", email)
    return True, ""


def _b64(b):
    """bytes -> URL 에 안전한 base64 글자"""
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s):
    """URL 안전 base64 글자 -> bytes"""
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(email):
    """로그인 성공 시 브라우저에 보관할 토큰 생성: '메일|만료시각|임의값' + 서명(위조 방지)"""
    payload = "%s|%d|%s" % (email, int(time.time() + SESSION_HOURS * 3600), secrets.token_hex(8))
    sig = hmac.new(APP_SECRET, payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64(payload.encode("utf-8")) + "." + _b64(sig)


def check_token(token):
    """유효하면 사용자(dict) 반환. 모든 콜백에서 서버 측 재검증"""
    try:
        p, s = token.split(".")
        payload = _unb64(p)
        exp_sig = hmac.new(APP_SECRET, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(exp_sig, _unb64(s)):
            return None
        email, exp, _ = payload.decode("utf-8").split("|")
        if time.time() > int(exp) or token in _revoked:
            return None
        user = find_user(email)
        if user is None or not is_authorized_user(user):
            return None
        return user
    except Exception:  # noqa
        return None


# =============================================================================
# 5. 권한
# =============================================================================
def is_admin(user):
    """ADMIN_EMAILS 에 있는 관리자인지"""
    return user["email"] in [e.lower() for e in ADMIN_EMAILS]


def is_authorized_user(user):
    """로그인 자체를 허용할지: 관리자/팀장/파트장은 항상, 팀원은 ALLOW_MEMBER_SELF_VIEW 일 때만"""
    return bool(is_admin(user) or user["role"] in TEAM_LEADER_ROLES or user["role"] in PART_LEADER_ROLES
                or ALLOW_MEMBER_SELF_VIEW)


def allowed_scopes(user):
    """[(value, label)] : value 는 서버에서 다시 해석하므로 조작해도 권한 밖 데이터는 보이지 않음"""
    org, _ = load_data()
    out = []
    if is_admin(user):
        for t in sorted(org["team"].unique()):
            if t:
                out.append(("team::" + t, "%s 전체" % t))
                for p in sorted(org.loc[org["team"] == t, "part"].unique()):
                    if p:
                        out.append(("part::%s::%s" % (t, p), "%s / %s" % (t, p)))
        return out
    if user["role"] in TEAM_LEADER_ROLES and user["team"]:
        out.append(("team::" + user["team"], "%s 전체" % user["team"]))
        for p in sorted(org.loc[org["team"] == user["team"], "part"].unique()):
            if p:
                out.append(("part::%s::%s" % (user["team"], p), "%s / %s" % (user["team"], p)))
    elif user["role"] in PART_LEADER_ROLES and user["team"] and user["part"]:
        out.append(("part::%s::%s" % (user["team"], user["part"]), "%s / %s" % (user["team"], user["part"])))
    if not out and ALLOW_MEMBER_SELF_VIEW:
        out.append(("self::" + user["email"], "내 결과 (%s)" % user["name"]))
    return out


def scope_members(user, scope):
    """선택한 범위의 구성원 + 버크만 결과 표. 권한 밖 범위면 None.
    정렬: 팀장 -> 파트장 -> 파트 -> 이름 순
    """
    if not user or scope not in [v for v, _ in allowed_scopes(user)]:
        return None
    org, _ = load_data()
    kind, _, rest = scope.partition("::")
    if kind == "team":
        rows = org[org["team"] == rest]
    elif kind == "part":
        t, _, p = rest.partition("::")
        rows = org[(org["team"] == t) & (org["part"] == p)]
    elif kind == "self":
        rows = org[org["email"] == rest]
    else:
        return None
    df = members_with_scores(rows)
    if df.empty:
        return df
    rank = {r: 0 for r in TEAM_LEADER_ROLES}
    rank.update({r: 1 for r in PART_LEADER_ROLES})
    df["_rank"] = df["role"].map(lambda r: rank.get(r, 2))
    return df.sort_values(["_rank", "part", "name"]).drop(columns="_rank").reset_index(drop=True)


def scope_leader(df, scope):
    """범위의 리더(팀 화면이면 팀장, 파트 화면이면 파트장). '리더 vs 팀원' 비교에 사용"""
    if df is None or df.empty:
        return None
    roles = TEAM_LEADER_ROLES if scope.startswith("team::") else PART_LEADER_ROLES
    r = df[df["role"].isin(roles) & df["has_birkman"]]
    return None if r.empty else r.iloc[0]


# =============================================================================
# 6. 분석 로직
# =============================================================================
def _num(v):
    """숫자로 변환 (빈 값/글자는 None)"""
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def color_of(row, layer):
    """한 사람의 기호(흥미/평소/욕구/스트레스) 색. 색 값이 있으면 그것을, 없으면 좌표로 사분면 계산"""
    c = row.get("map_%s_color" % layer)
    if isinstance(c, str) and c in COLOR_HEX:
        return c
    x, y = _num(row.get("map_%s_x" % layer)), _num(row.get("map_%s_y" % layer))
    if x is None or y is None:
        return None
    if y >= 50:
        return "red" if x < 50 else "green"
    return "yellow" if x < 50 else "blue"


def xy_of(row, layer):
    """맵에 찍을 위치(0~100). 좌표가 없고 색만 있으면(예: PDF 에서 읽은 경우) 그 사분면 가운데 근처에 찍습니다.
    여러 명이 한 점에 겹치지 않도록 이름으로 정해지는 작은 간격을 둡니다(매번 같은 위치).
    """
    x, y = _num(row.get("map_%s_x" % layer)), _num(row.get("map_%s_y" % layer))
    if x is not None and y is not None:
        return x, y
    c = color_of(row, layer)
    centers = {"red": (25, 75), "green": (75, 75), "yellow": (25, 25), "blue": (75, 25)}
    if c not in centers:
        return None, None
    h = int(hashlib.md5(("%s|%s" % (row.get("email") or row.get("name"), layer)).encode("utf-8")).hexdigest(), 16)
    dx, dy = (h % 21) - 10, ((h // 21) % 21) - 10       # -10 ~ +10 범위
    return centers[c][0] + dx, centers[c][1] + dy


def color_counts(df, layer):
    """범위 안에서 기호별로 색마다 몇 명인지 셈 (예: 평소행동 파랑 5명)"""
    cnt = {c: 0 for c in COLOR_ORDER}
    for _, r in df.iterrows():
        c = color_of(r, layer)
        if c:
            cnt[c] += 1
    return cnt


def level(v):
    """구성요소 점수를 높음/중간/낮음으로 구분 (HIGH, LOW 기준)"""
    if v is None:
        return None
    return "high" if v >= HIGH else ("low" if v <= LOW else "mid")


def comp_text(key, kind, v):
    """구성요소 점수에 맞는 설명 문구 (kind: 'usual' 평소 / 'needs' 욕구)"""
    c = [x for x in COMPONENTS if x[0] == key][0]
    lv = level(v)
    if lv is None:
        return ""
    if lv == "mid":
        return "상황에 따라 균형 있게" if kind == "usual" else "어느 쪽이든 크게 불편하지 않음"
    if kind == "usual":
        return c[2] if lv == "high" else c[3]
    return c[4] if lv == "high" else c[5]


def cname(c):
    """색 이름 표기. 예) 'blue' -> '파랑(기획가·사색가)'"""
    return "%s(%s)" % (COLOR_INFO[c]["ko"], COLOR_INFO[c]["nick"])


def team_insights(df, leader=None):
    """'팀 해석 요약' 문장을 규칙에 따라 자동으로 만듭니다.
    - 팀 분위기(평소행동 최다 색), 빈 영역(아무도 없는 색), 팀 욕구, 평소와 욕구가 다른 팀원,
      흥미, 성향 다양성(맵에서 퍼진 정도), 조직지향점, 구성요소 팁, 리더 vs 팀원 비교
    반환: [(제목, 내용 문장 또는 문장 목록), ...]
    """
    d = df[df["has_birkman"]]
    n = len(d)
    out = []
    if n == 0:
        return [("진단 결과 없음", "이 범위에 버크만 결과가 있는 인원이 없습니다.")]

    usual, needs, inter = color_counts(d, "usual"), color_counts(d, "needs"), color_counts(d, "interest")
    tot_u = sum(usual.values()) or 1
    dom_u = sorted(COLOR_ORDER, key=lambda c: -usual[c])
    top = dom_u[0]
    out.append(("팀의 기본 분위기 (평소 행동)",
                "%s 성향이 %d명(%d%%)으로 가장 많습니다. %s" % (
                    cname(top), usual[top], round(100.0 * usual[top] / tot_u), COLOR_INFO[top]["usual"])))
    missing = [c for c in COLOR_ORDER if usual[c] == 0]
    if missing and n >= 3:
        out.append(("빈 영역 (사각지대)", ["%s 없음: %s" % (cname(c), COLOR_INFO[c]["absent"]) for c in missing]))

    tot_n = sum(needs.values())
    if tot_n:
        dom_n = max(COLOR_ORDER, key=lambda c: needs[c])
        txt = "팀원 %d명(%d%%)의 욕구가 %s 영역입니다. 리더가 만들어 줄 환경: %s" % (
            needs[dom_n], round(100.0 * needs[dom_n] / tot_n), cname(dom_n), COLOR_INFO[dom_n]["needs"])
        if dom_n != top:
            txt += (" ※ 겉으로 보이는 모습(%s)과 실제로 필요한 것(%s)이 다릅니다. "
                    "평소 모습만 보고 대하면 팀원들이 스트레스를 받을 수 있습니다." % (
                        COLOR_INFO[top]["ko"], COLOR_INFO[dom_n]["ko"]))
        out.append(("팀이 필요로 하는 것 (욕구)", txt))

    diff = [r["name"] for _, r in d.iterrows()
            if color_of(r, "usual") and color_of(r, "needs") and color_of(r, "usual") != color_of(r, "needs")]
    if diff:
        out.append(("평소 모습과 욕구가 다른 팀원",
                    "%s (%d명) — 겉으로 드러나는 행동만 보고 판단하지 말고, 개인별 탭의 '욕구'를 확인해 주세요."
                    % (", ".join(diff[:10]) + (" 외" if len(diff) > 10 else ""), len(diff))))

    mism = sum(1 for _, r in d.iterrows()
               if color_of(r, "interest") and color_of(r, "usual") and color_of(r, "interest") != color_of(r, "usual"))
    tot_i = sum(inter.values())
    if tot_i:
        dom_i = max(COLOR_ORDER, key=lambda c: inter[c])
        txt = "팀의 흥미는 %s 쪽이 가장 많습니다(%d명)." % (cname(dom_i), inter[dom_i])
        if mism >= max(2, tot_i / 2.0):
            txt += " 절반 이상(%d명)이 하고 싶은 일(흥미)과 일하는 방식(평소)이 달라, 업무 배분 시 흥미를 고려하면 몰입도를 높일 수 있습니다." % mism
        out.append(("흥미", txt))

    pts = [xy_of(r, "usual") for _, r in d.iterrows()]
    pts = [p for p in pts if p[0] is not None]
    if len(pts) >= 3:
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        spread = sum(math.hypot(p[0] - cx, p[1] - cy) for p in pts) / len(pts)
        if spread < 15:
            out.append(("성향 다양성: 비슷한 편", "팀원들의 평소 행동이 맵의 한 곳에 모여 있습니다. 합의가 빠르고 손발이 잘 맞지만, "
                        "다른 관점을 놓치기 쉽습니다. 의사결정 때 반대 관점을 일부러 검토해 보세요."))
        elif spread > 25:
            out.append(("성향 다양성: 다양한 편", "팀원들의 평소 행동이 맵 전체에 퍼져 있습니다. 관점이 다양해 문제를 넓게 보지만, "
                        "소통 방식 차이로 오해가 생기기 쉽습니다. 회의 규칙과 역할을 명확히 해 주세요."))

    of = {c: d["orgfocus_%s" % c].astype(float).mean() for c, _ in ORGFOCUS if "orgfocus_%s" % c in d.columns}
    of = {k: v for k, v in of.items() if v == v}
    if of:
        lbl = dict(ORGFOCUS)
        hi = max(of, key=of.get)
        lo = min(of, key=of.get)
        out.append(("조직지향점", "팀의 에너지는 '%s'(평균 %.0f)에 가장 많이 모이고, '%s'(평균 %.0f)가 가장 약합니다. "
                    "약한 영역의 업무는 담당자를 지정하거나 프로세스로 보완하는 것이 좋습니다." % (
                        lbl[hi], of[hi], lbl[lo], of[lo])))

    sig = d[d[[c for c in d.columns if c.startswith("comp_") and c.endswith("_needs")]].notna().sum(axis=1) >= 5] \
        if any(c.startswith("comp_") for c in d.columns) else d.iloc[0:0]
    if len(sig) >= 2:
        tips = []
        for key, label, _, _, nh, nl in COMPONENTS:
            col = "comp_%s_needs" % key
            if col not in sig.columns:
                continue
            m = sig[col].astype(float).mean()
            if m != m:
                continue
            if m >= HIGH + 5:
                tips.append("%s 욕구 높음(평균 %.0f): %s" % (label, m, nh))
            elif m <= LOW - 5:
                tips.append("%s 욕구 낮음(평균 %.0f): %s" % (label, m, nl))
        if tips:
            out.append(("구성요소로 본 팀 운영 팁 (시그니처 %d명 기준)" % len(sig), tips[:5]))

        if leader is not None:
            gaps = []
            for key, label, uh, ul, _, _ in COMPONENTS:
                lu = _num(leader.get("comp_%s_usual" % key))
                col = "comp_%s_needs" % key
                others = sig[sig["email"] != leader["email"]]
                if lu is None or col not in others.columns or others[col].notna().sum() < 2:
                    continue
                m = others[col].astype(float).mean()
                if abs(lu - m) >= 30:
                    gaps.append("%s: 리더 평소 %.0f(%s) vs 팀원 욕구 평균 %.0f" % (
                        label, lu, uh if lu >= m else ul, m))
            if gaps:
                out.append(("리더 vs 팀원 (리더 평소 행동 ↔ 팀원 욕구)",
                            ["리더의 평소 방식과 팀원들이 원하는 방식의 차이가 큰 항목입니다."] + gaps[:4]))
    elif leader is not None:
        lc, nc = color_of(leader, "usual"), needs
        if lc and sum(nc.values()):
            dom_n = max(COLOR_ORDER, key=lambda c: nc[c])
            if lc != dom_n:
                out.append(("리더 vs 팀원", "리더의 평소 행동은 %s, 팀원 욕구는 %s가 가장 많습니다. %s" % (
                    cname(lc), cname(dom_n), COLOR_INFO[dom_n]["tip"])))
    return out


# =============================================================================
# 7. 차트
# =============================================================================
# 팀 맵에서 사람마다 구분하는 색 (20명 넘으면 반복)
MEMBER_PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
                  "#bcbd22", "#17becf", "#393b79", "#637939", "#8c6d31", "#843c39", "#7b4173", "#3182bd",
                  "#e6550d", "#31a354", "#756bb1", "#636363"]
# 화면 글꼴 (윈도우: 맑은 고딕)
FONT = "Malgun Gothic, Apple SD Gothic Neo, Noto Sans KR, sans-serif"


def _map_base(height=560):
    """빈 버크만 맵(4색 사분면, 축 이름)을 그림. 좌표 숫자는 표시하지 않음"""
    fig = go.Figure()
    quads = [("red", 0, 50, 50, 100), ("green", 50, 50, 100, 100), ("yellow", 0, 0, 50, 50), ("blue", 50, 0, 100, 50)]
    for c, x0, y0, x1, y1 in quads:
        fig.add_shape(type="rect", x0=x0, y0=y0, x1=x1, y1=y1, line=dict(width=0), fillcolor=COLOR_BG[c], layer="below")
        fig.add_annotation(x=(x0 + x1) / 2.0, y=y1 - 4 if y0 >= 50 else y0 + 4, showarrow=False,
                           text="<b>%s</b> %s" % (COLOR_INFO[c]["ko"].upper(), COLOR_INFO[c]["nick"]),
                           font=dict(color=COLOR_HEX[c], size=13))
    for v in (25, 75):
        fig.add_shape(type="line", x0=v, x1=v, y0=0, y1=100, line=dict(color="rgba(0,0,0,0.06)", width=1))
        fig.add_shape(type="line", y0=v, y1=v, x0=0, x1=100, line=dict(color="rgba(0,0,0,0.06)", width=1))
    fig.add_shape(type="line", x0=50, x1=50, y0=0, y1=100, line=dict(color="rgba(0,0,0,0.35)", width=1.5))
    fig.add_shape(type="line", y0=50, y1=50, x0=0, x1=100, line=dict(color="rgba(0,0,0,0.35)", width=1.5))
    fig.update_xaxes(range=[0, 100], showticklabels=False, showgrid=False, zeroline=False,
                     title="← 과제지향                                     사람지향 →")
    fig.update_yaxes(range=[0, 100], showticklabels=False, showgrid=False, zeroline=False,
                     scaleanchor="x", scaleratio=1, title="← 내향                    외향 →")
    fig.update_layout(height=height, margin=dict(l=40, r=10, t=30, b=40), plot_bgcolor="white",
                      paper_bgcolor="white", font=dict(family=FONT), legend=dict(font=dict(size=11)))
    return fig


def team_map_figure(df, layers, show_names=True, connect=False):
    """팀 버크만 맵: 사람마다 다른 색, 기호 모양으로 흥미(별)/평소(다이아몬드)/욕구(원)/스트레스(사각형) 구분.
    connect=True 면 평소 -> 욕구 화살표를 그림
    """
    fig = _map_base()
    d = df[df["has_birkman"]].reset_index(drop=True)
    for i, r in d.iterrows():
        col = MEMBER_PALETTE[i % len(MEMBER_PALETTE)]
        label = "%s%s" % (r["name"], " (%s)" % r["role"] if r["role"] in TEAM_LEADER_ROLES + PART_LEADER_ROLES else "")
        first = True
        pts = {}
        for layer in layers:
            x, y = xy_of(r, layer)
            if x is None:
                continue
            pts[layer] = (x, y)
            filled = layer in ("usual", "interest")
            fig.add_trace(go.Scatter(
                x=[x], y=[y], mode="markers+text" if (show_names and layer == layers[0]) else "markers",
                text=[r["name"]], textposition="top center", textfont=dict(size=11, color=col),
                marker=dict(symbol=LAYER_INFO[layer]["symbol"] + ("" if filled else "-open"), size=15 if layer == "interest" else 13,
                            color=col, line=dict(width=2, color=col if not filled else "white")),
                name=label, legendgroup=label, showlegend=first,
                hovertemplate="<b>%s</b><br>%s: %s<extra></extra>" % (
                    label, LAYER_INFO[layer]["ko"], cname(color_of(r, layer)) if color_of(r, layer) else "-")))
            first = False
        if connect and "usual" in pts and "needs" in pts:
            (x0, y0), (x1, y1) = pts["usual"], pts["needs"]
            fig.add_annotation(x=x1, y=y1, ax=x0, ay=y0, xref="x", yref="y", axref="x", ayref="y", showarrow=True,
                               arrowhead=2, arrowsize=1, arrowwidth=1.2, arrowcolor=col, opacity=0.6, text="")
    return fig


def person_map_figure(r):
    """개인 버크만 맵: 4개 기호를 각 사분면 색으로 표시. 같은 위치의 기호는 이름을 합쳐 표시"""
    fig = _map_base(height=380)
    labels = {}
    for layer in LAYERS:
        x, y = xy_of(r, layer)
        if x is not None:
            labels.setdefault((round(x), round(y)), []).append(LAYER_INFO[layer]["ko"])
    done = set()
    for layer in LAYERS:
        x, y = xy_of(r, layer)
        if x is None:
            continue
        c = color_of(r, layer) or "red"
        key = (round(x), round(y))
        text = "" if key in done else "·".join(labels[key])
        done.add(key)
        fig.add_trace(go.Scatter(
            x=[x], y=[y], mode="markers+text", text=[text], textposition="bottom center",
            marker=dict(symbol=LAYER_INFO[layer]["symbol"], size=18, color=COLOR_HEX[c], line=dict(width=1.5, color="white")),
            name=LAYER_INFO[layer]["ko"], hovertemplate="%s: %s<extra></extra>" % (LAYER_INFO[layer]["ko"], cname(c))))
    fig.update_layout(showlegend=False, margin=dict(l=30, r=10, t=10, b=30))
    fig.update_xaxes(title="")
    fig.update_yaxes(title="")
    return fig


def color_dist_figure(df):
    """기호별 색 분포 가로 막대 (몇 명이 어느 색인지)"""
    d = df[df["has_birkman"]]
    fig = go.Figure()
    for c in COLOR_ORDER:
        fig.add_trace(go.Bar(
            y=[LAYER_INFO[l]["ko"] for l in LAYERS], x=[color_counts(d, l)[c] for l in LAYERS], orientation="h",
            name=cname(c), marker_color=COLOR_HEX[c], text=[color_counts(d, l)[c] or "" for l in LAYERS],
            textposition="inside", insidetextanchor="middle"))
    fig.update_layout(barmode="stack", height=280, margin=dict(l=10, r=10, t=40, b=10), font=dict(family=FONT),
                      plot_bgcolor="white", legend=dict(orientation="h", y=1.18, traceorder="normal"), xaxis=dict(dtick=1))
    fig.update_yaxes(autorange="reversed")
    return fig


def orgfocus_figures(df):
    """조직지향점: 팀 평균 막대 + 구성원별 히트맵. 데이터가 없으면 (None, None)"""
    d = df[df["has_birkman"]]
    cols = ["orgfocus_%s" % c for c, _ in ORGFOCUS]
    if not all(c in d.columns for c in cols) or d[cols].notna().sum().sum() == 0:
        return None, None
    avg = [d[c].astype(float).mean() for c in cols]
    bar = go.Figure(go.Bar(x=[l for _, l in ORGFOCUS], y=avg, marker_color=[COLOR_HEX[c] for c, _ in ORGFOCUS],
                           text=["%.0f" % v if v == v else "" for v in avg], textposition="outside"))
    bar.update_layout(height=300, margin=dict(l=10, r=10, t=20, b=10), yaxis=dict(range=[0, 105]),
                      plot_bgcolor="white", font=dict(family=FONT))
    dd = d[d[cols].notna().any(axis=1)]
    z = dd[cols].astype(float).values.tolist()
    heat = go.Figure(go.Heatmap(z=z, x=[l for _, l in ORGFOCUS], y=dd["name"].tolist(), colorscale="Blues", zmin=0,
                                zmax=100, text=[["%.0f" % v if v == v else "" for v in row] for row in z],
                                texttemplate="%{text}", hovertemplate="%{y} · %{x}: %{z:.0f}<extra></extra>"))
    heat.update_layout(height=max(260, 34 * len(dd) + 80), margin=dict(l=10, r=10, t=10, b=10), font=dict(family=FONT))
    heat.update_yaxes(autorange="reversed")
    return bar, heat


def components_figures(df, kind):
    """구성요소(시그니처): 팀 평균 평소 vs 욕구 막대 + 구성원별 히트맵. 시그니처 점수가 있는 사람만 사용"""
    d = df[df["has_birkman"]]
    ucols = ["comp_%s_usual" % k for k, *_ in COMPONENTS]
    ncols = ["comp_%s_needs" % k for k, *_ in COMPONENTS]
    have = [c for c in ucols + ncols if c in d.columns]
    if not have:
        return None, None, d.iloc[0:0]
    sig = d[d[have].notna().sum(axis=1) >= 5]
    if sig.empty:
        return None, None, sig
    labels = [c[1] for c in COMPONENTS]
    au = [sig[c].astype(float).mean() if c in sig.columns else None for c in ucols]
    an = [sig[c].astype(float).mean() if c in sig.columns else None for c in ncols]
    bar = go.Figure()
    bar.add_trace(go.Bar(x=labels, y=au, name="평소 행동 평균", marker_color="#5B6B7F"))
    bar.add_trace(go.Bar(x=labels, y=an, name="욕구 평균", marker_color="#E58E26"))
    bar.add_hrect(y0=LOW, y1=HIGH, fillcolor="rgba(0,0,0,0.04)", line_width=0)
    bar.update_layout(barmode="group", height=330, margin=dict(l=10, r=10, t=20, b=10), yaxis=dict(range=[0, 100]),
                      plot_bgcolor="white", legend=dict(orientation="h", y=1.12), font=dict(family=FONT))
    cols = ucols if kind == "usual" else ncols
    z = [[_num(r.get(c)) for c in cols] for _, r in sig.iterrows()]
    heat = go.Figure(go.Heatmap(
        z=z, x=labels, y=sig["name"].tolist(), zmin=0, zmax=100,
        colorscale=[[0, "#3B6FB6"], [0.4, "#DCE6F2"], [0.5, "#F7F7F7"], [0.6, "#FBE3CF"], [1, "#D9631E"]],
        text=[["" if v is None else "%.0f" % v for v in row] for row in z], texttemplate="%{text}",
        hovertemplate="%{y} · %{x}: %{z:.0f}<extra></extra>"))
    heat.update_layout(height=max(260, 34 * len(sig) + 90), margin=dict(l=10, r=10, t=10, b=10), font=dict(family=FONT))
    heat.update_yaxes(autorange="reversed")
    return bar, heat, sig


def jobint_figure(df):
    """직업 흥미 구성원별 히트맵 (참고용)"""
    d = df[df["has_birkman"]]
    cols = ["jobint_%s" % k for k, _ in JOBINT]
    if not all(c in d.columns for c in cols):
        return None
    dd = d[d[cols].notna().sum(axis=1) >= 3]
    if dd.empty:
        return None
    z = dd[cols].astype(float).values.tolist()
    fig = go.Figure(go.Heatmap(z=z, x=[l for _, l in JOBINT], y=dd["name"].tolist(), colorscale="Greens", zmin=0,
                               zmax=100, text=[["%.0f" % v if v == v else "" for v in row] for row in z],
                               texttemplate="%{text}", hovertemplate="%{y} · %{x}: %{z:.0f}<extra></extra>"))
    fig.update_layout(height=max(240, 32 * len(dd) + 80), margin=dict(l=10, r=10, t=10, b=10), font=dict(family=FONT))
    fig.update_yaxes(autorange="reversed")
    return fig


# =============================================================================
# 8. 화면 구성
# =============================================================================
# 화면 요소 공통 스타일 (카드, 버튼, 입력칸, 흐린 글씨)
S_CARD = {"background": "white", "border": "1px solid #E3E7ED", "borderRadius": "10px", "padding": "16px 18px",
          "marginBottom": "14px"}
S_BTN = {"background": "#1F3A5F", "color": "white", "border": "none", "borderRadius": "6px", "padding": "9px 16px",
         "cursor": "pointer", "fontSize": "14px"}
S_BTN2 = dict(S_BTN, background="white", color="#1F3A5F", border="1px solid #1F3A5F")
S_INPUT = {"padding": "9px 10px", "border": "1px solid #C9D1DB", "borderRadius": "6px", "fontSize": "14px",
           "width": "100%", "boxSizing": "border-box"}
S_MUTED = {"color": "#6B7685", "fontSize": "13px"}


def card(children, title=None, sub=None):
    """흰 카드 상자 (제목, 설명, 내용)"""
    head = []
    if title:
        head.append(html.Div(title, style={"fontWeight": "700", "fontSize": "16px", "marginBottom": "4px"}))
    if sub:
        head.append(html.Div(sub, style=dict(S_MUTED, marginBottom="10px")))
    return html.Div(head + (children if isinstance(children, list) else [children]), style=S_CARD)


def chip(c, text=None):
    """색 이름표 (예: 파란 바탕의 '파랑(기획가·사색가)')"""
    if not c:
        return html.Span("-", style=S_MUTED)
    return html.Span(text or cname(c), style={"background": COLOR_HEX[c], "color": "white", "borderRadius": "12px",
                                              "padding": "2px 10px", "fontSize": "12px", "marginRight": "6px",
                                              "whiteSpace": "nowrap"})


def graph(fig):
    """plotly 차트를 화면 요소로 감쌈 (불필요한 도구 버튼 제거)"""
    return dcc.Graph(figure=fig, config={"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d"]})


def legend_symbols():
    """맵 기호 설명 줄 (✱ 흥미, ◆ 평소 행동, ○ 욕구, □ 스트레스)"""
    items = []
    sym = {"interest": "✱", "usual": "◆", "needs": "○", "stress": "□"}
    for l in LAYERS:
        items.append(html.Span([html.B(sym[l] + " " + LAYER_INFO[l]["ko"]), " : " + LAYER_INFO[l]["desc"]],
                               style={"marginRight": "16px", "fontSize": "13px"}))
    return html.Div(items, style={"marginBottom": "6px"})


# --- 로그인 화면: 메일 입력 -> 인증번호 받기 -> 인증번호 입력 -> 로그인 ---
login_view = html.Div(id="login-view", style={"maxWidth": "380px", "margin": "80px auto"}, children=[
    html.Div("버크만 팀 리포트", style={"fontSize": "24px", "fontWeight": "800", "color": "#1F3A5F"}),
    html.Div("팀장·파트장이 팀의 성향과 팀원 개개인을 이해하도록 돕는 도구입니다.", style=dict(S_MUTED, margin="6px 0 24px")),
    html.Label("사내 메일", style={"fontSize": "13px", "fontWeight": "600"}),
    dcc.Input(id="in-email", type="email", placeholder="name@company.com", style=S_INPUT, debounce=False),
    html.Button("인증번호 받기", id="btn-send", n_clicks=0, style=dict(S_BTN, width="100%", marginTop="10px")),
    html.Div(style={"height": "18px"}),
    html.Label("인증번호 6자리", style={"fontSize": "13px", "fontWeight": "600"}),
    dcc.Input(id="in-code", type="text", maxLength=6, placeholder="000000", style=S_INPUT),
    html.Button("로그인", id="btn-verify", n_clicks=0, style=dict(S_BTN, width="100%", marginTop="10px")),
    html.Div(id="login-msg", style={"marginTop": "14px", "fontSize": "13px", "color": "#B03A2E", "whiteSpace": "pre-line"}),
    html.Div("※ 결과는 사람을 평가·선발하기 위한 것이 아니라, 서로를 이해하고 함께 일하는 방식을 찾기 위한 참고자료입니다. "
             "열람 기록이 남습니다.", style=dict(S_MUTED, marginTop="28px", fontSize="12px")),
])

# --- 메인 화면: 상단(범위 선택, 사용자, 로그아웃) + 6개 탭 ---
# 각 탭의 내용(pane-*)은 아래 9번 콜백 함수들이 채웁니다.
TAB_STYLE = {"padding": "10px 14px", "fontWeight": "600"}
main_view = html.Div(id="main-view", style={"display": "none"}, children=[
    html.Div(style={"display": "flex", "alignItems": "center", "gap": "12px", "flexWrap": "wrap",
                    "padding": "14px 0", "borderBottom": "1px solid #E3E7ED", "marginBottom": "14px"}, children=[
        html.Div("버크만 팀 리포트", style={"fontSize": "20px", "fontWeight": "800", "color": "#1F3A5F"}),
        html.Div(dcc.Dropdown(id="dd-scope", clearable=False, placeholder="범위 선택"), style={"minWidth": "260px"}),
        html.Div(id="who", style=dict(S_MUTED, marginLeft="auto")),
        html.Button("로그아웃", id="btn-logout", n_clicks=0, style=S_BTN2),
    ]),
    dcc.Tabs(id="tabs", value="overview", children=[
        dcc.Tab(label="팀 개요", value="overview", style=TAB_STYLE, selected_style=TAB_STYLE, children=[
            html.Div(id="pane-overview", style={"paddingTop": "14px"})]),
        dcc.Tab(label="버크만 맵", value="map", style=TAB_STYLE, selected_style=TAB_STYLE, children=[
            html.Div(style={"paddingTop": "14px"}, children=[card([
                legend_symbols(),
                html.Div(style={"display": "flex", "gap": "20px", "flexWrap": "wrap"}, children=[
                    dcc.Checklist(id="cl-layers", value=["usual", "needs"], inline=True,
                                  options=[{"label": " " + LAYER_INFO[l]["ko"], "value": l} for l in LAYERS],
                                  inputStyle={"marginLeft": "10px"}),
                    dcc.Checklist(id="cl-opts", value=["names"], inline=True, inputStyle={"marginLeft": "10px"},
                                  options=[{"label": " 이름 표시", "value": "names"},
                                           {"label": " 평소→욕구 화살표", "value": "arrow"}]),
                ]),
                dcc.Graph(id="g-map", config={"displaylogo": False}),
                html.Div("범례에서 이름을 클릭하면 해당 팀원을 숨기거나 표시할 수 있습니다. 좌표 없이 색만 있는 사람(PDF로 읽은 경우)은 해당 사분면 가운데 근처에 대략적으로 표시됩니다.",
                         style=S_MUTED)],
                title="팀 버크만 맵", sub="같은 색 = 같은 사람, 모양 = 흥미/평소/욕구/스트레스")])]),
        dcc.Tab(label="조직지향점", value="orgfocus", style=TAB_STYLE, selected_style=TAB_STYLE, children=[
            html.Div(id="pane-orgfocus", style={"paddingTop": "14px"})]),
        dcc.Tab(label="구성요소 (시그니처)", value="comp", style=TAB_STYLE, selected_style=TAB_STYLE, children=[
            html.Div(style={"paddingTop": "14px"}, children=[
                dcc.RadioItems(id="rb-comp", value="needs", inline=True, inputStyle={"marginLeft": "10px"},
                               options=[{"label": " 욕구 보기", "value": "needs"}, {"label": " 평소 행동 보기", "value": "usual"}]),
                html.Div(id="pane-comp")])]),
        dcc.Tab(label="개인별", value="person", style=TAB_STYLE, selected_style=TAB_STYLE, children=[
            html.Div(style={"paddingTop": "14px"}, children=[
                html.Div(dcc.Dropdown(id="dd-person", placeholder="팀원 선택", clearable=False),
                         style={"maxWidth": "360px", "marginBottom": "12px"}),
                html.Div(id="pane-person")])]),
        dcc.Tab(label="직업 흥미 (참고)", value="jobint", style=TAB_STYLE, selected_style=TAB_STYLE, children=[
            html.Div(id="pane-jobint", style={"paddingTop": "14px"})]),
    ]),
])

if "app" not in globals():  # 로컬 실행 시. Dataiku 에서는 app 이 미리 만들어져 있음
    app = dash.Dash(__name__)
app.config.suppress_callback_exceptions = True
try:
    app.title = "버크만 팀 리포트"
except Exception:  # noqa
    pass

# 전체 화면 = 로그인 화면 + 메인 화면 (로그인 여부에 따라 하나만 보임)
# auth: 로그인 토큰을 브라우저 탭에 보관 (session = 탭을 닫으면 삭제)
app.layout = html.Div(style={"fontFamily": FONT, "background": "#F5F7FA", "minHeight": "100vh", "padding": "0 20px"},
                      children=[
                          dcc.Store(id="auth", storage_type="session"),
                          html.Div(style={"maxWidth": "1200px", "margin": "0 auto"}, children=[login_view, main_view]),
                      ])


# =============================================================================
# 9. 동작(콜백)
#    Dash 에서는 "어떤 입력이 바뀌면 어떤 화면 요소를 다시 그린다"를 함수로 정의합니다.
#    @app.callback(Output=다시 그릴 곳, Input=반응할 입력, State=참고만 하는 값)
#    모든 화면 함수는 _ctx() 로 로그인/권한을 서버에서 다시 확인한 뒤에만 데이터를 보여줍니다.
# =============================================================================
@app.callback([Output("auth", "data"), Output("login-msg", "children")],
              [Input("btn-send", "n_clicks"), Input("btn-verify", "n_clicks"), Input("btn-logout", "n_clicks")],
              [State("in-email", "value"), State("in-code", "value"), State("auth", "data")],
              prevent_initial_call=True)
def on_auth(_s, _v, _l, email, code, token):
    """[로그인 화면] '인증번호 받기' / '로그인' / '로그아웃' 버튼 처리.
    로그인에 성공하면 토큰을 브라우저 탭 저장소(auth)에 넣고, 로그아웃하면 지우고 토큰을 무효화
    """
    trig = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    email = (email or "").strip().lower()
    if trig == "btn-logout":
        if token:
            _revoked.add(token)
        return None, ""
    if trig == "btn-send":
        msg, dev_code = request_otp(email)
        if dev_code:
            msg += "\n[로컬 테스트 모드] 인증번호: %s" % dev_code
        return no_update, msg
    if trig == "btn-verify":
        if not email_allowed(email):
            return no_update, "메일 주소를 확인해 주세요."
        ok, msg = verify_otp(email, code)
        if not ok:
            return no_update, msg
        return make_token(email), ""
    return no_update, no_update


@app.callback([Output("login-view", "style"), Output("main-view", "style"), Output("who", "children"), Output("dd-scope", "options"), Output("dd-scope", "value")],
              [Input("auth", "data")])
def on_session(token):
    """[화면 전환] 토큰이 유효하면 메인 화면 + 볼 수 있는 범위 목록, 아니면 로그인 화면"""
    user = check_token(token) if token else None
    hide, show = {"display": "none"}, {"display": "block"}
    if not user:
        return dict(login_view.style, display="block"), hide, "", [], None
    scopes = allowed_scopes(user)
    who = "%s · %s%s%s" % (user["name"], user["team"], " / " + user["part"] if user["part"] else "",
                          " · " + user["role"] if user["role"] else "")
    return hide, show, who, [{"label": l, "value": v} for v, l in scopes], (scopes[0][0] if scopes else None)


def _ctx(token, scope):
    """모든 화면 공통: 토큰 확인 -> 권한 확인 -> 범위 구성원 표. 하나라도 실패하면 (None, None)"""
    user = check_token(token) if token else None
    if not user or not scope:
        return None, None
    return user, scope_members(user, scope)


@app.callback(Output("pane-overview", "children"),
              [Input("dd-scope", "value")],
              [State("auth", "data")])
def on_overview(scope, token):
    """[팀 개요 탭] 인원 현황, 팀 해석 요약, 색 분포, 구성원 표"""
    user, df = _ctx(token, scope)
    if df is None:
        return ""
    audit("VIEW_TEAM", user["email"], scope)
    n_all, d = len(df), df[df["has_birkman"]]
    n_sig = int((d.get("report_type", pd.Series(dtype=str)) == "signature").sum())
    kpi = lambda v, l: html.Div([html.Div(str(v), style={"fontSize": "26px", "fontWeight": "800", "color": "#1F3A5F"}),
                                 html.Div(l, style=S_MUTED)], style=dict(S_CARD, flex="1", minWidth="140px", marginBottom="0"))
    kpis = html.Div([kpi(n_all, "구성원"), kpi(len(d), "진단 완료"), kpi(n_sig, "시그니처"),
                     kpi(len(d) - n_sig, "베이직"), kpi(n_all - len(d), "결과 없음")],
                    style={"display": "flex", "gap": "12px", "flexWrap": "wrap", "marginBottom": "14px"})
    leader = scope_leader(df, scope)
    ins = team_insights(df, leader)
    ins_cards = [html.Div([html.Div(t, style={"fontWeight": "700", "marginBottom": "4px"}),
                           html.Div(b, style={"lineHeight": "1.6", "fontSize": "14px"}) if isinstance(b, str) else
                           html.Ul([html.Li(x) for x in b], style={"lineHeight": "1.6", "fontSize": "14px",
                                                                   "margin": "0", "paddingLeft": "18px"})],
                          style={"borderLeft": "4px solid #1F3A5F", "padding": "6px 12px", "marginBottom": "12px"})
                 for t, b in ins]
    rows = []
    for _, r in df.iterrows():
        rows.append(html.Tr([
            html.Td(r["name"]), html.Td(r["part"] or "-"), html.Td(r["role"] or "-"),
            html.Td({"signature": "시그니처", "basic": "베이직"}.get(r.get("report_type"), "-") if r["has_birkman"] else "결과 없음"),
        ] + [html.Td(chip(color_of(r, l), COLOR_INFO[color_of(r, l)]["ko"] if color_of(r, l) else None)
                     if r["has_birkman"] else "") for l in LAYERS]))
    th = lambda t: html.Th(t, style={"textAlign": "left", "padding": "6px", "borderBottom": "2px solid #E3E7ED", "fontSize": "13px"})
    table = html.Table([html.Thead(html.Tr([th(t) for t in ["이름", "파트", "직책", "진단"] +
                                            [LAYER_INFO[l]["ko"] for l in LAYERS]])),
                        html.Tbody(rows)], style={"width": "100%", "borderCollapse": "collapse", "fontSize": "14px"})
    return [kpis,
            card(ins_cards, "팀 해석 요약", "버크만 맵과 조직지향점을 중심으로 자동 생성된 요약입니다."),
            card(graph(color_dist_figure(df)), "색상 분포", "평소 행동·욕구·흥미·스트레스가 어느 색에 몇 명씩 있는지"),
            card(table, "구성원 한눈에 보기")]


@app.callback(Output("g-map", "figure"),
              [Input("dd-scope", "value"), Input("cl-layers", "value"), Input("cl-opts", "value")],
              [State("auth", "data")])
def on_map(scope, layers, opts, token):
    """[버크만 맵 탭] 선택한 기호(흥미/평소/욕구/스트레스)와 옵션(이름, 화살표)으로 팀 맵 그리기"""
    user, df = _ctx(token, scope)
    if df is None:
        return _map_base()
    layers = [l for l in LAYERS if l in (layers or [])] or ["usual"]
    return team_map_figure(df, layers, show_names="names" in (opts or []), connect="arrow" in (opts or []))


@app.callback(Output("pane-orgfocus", "children"),
              [Input("dd-scope", "value")],
              [State("auth", "data")])
def on_orgfocus(scope, token):
    """[조직지향점 탭] 팀 평균, 구성원별 점수, 영역별 강점 인원, 영역 설명"""
    user, df = _ctx(token, scope)
    if df is None:
        return ""
    bar, heat = orgfocus_figures(df)
    if bar is None:
        return card(html.Div("조직지향점 데이터가 없습니다.", style=S_MUTED))
    desc = html.Ul([html.Li([chip(c, l), " " + COLOR_INFO[c]["usual"]], style={"marginBottom": "6px", "fontSize": "14px"})
                    for c, l in ORGFOCUS], style={"listStyle": "none", "paddingLeft": "0"})
    d = df[df["has_birkman"]]
    best = []
    for c, l in ORGFOCUS:
        col = "orgfocus_%s" % c
        if col in d.columns and d[col].notna().any():
            top = d.sort_values(col, ascending=False).head(3)
            best.append(html.Div([chip(c, l), "  ", ", ".join("%s(%.0f)" % (r["name"], r[col]) for _, r in top.iterrows()
                                                         if r[col] == r[col])], style={"marginBottom": "6px", "fontSize": "14px"}))
    return [card(graph(bar), "팀 조직지향점 평균", "팀이 일에서 자연스럽게 에너지를 쏟는 영역"),
            card(graph(heat), "구성원별 조직지향점"),
            card(best, "영역별 강점 인원 (상위 3명)", "업무 배분·역할 분담 시 참고"),
            card(desc, "영역 설명")]


@app.callback(Output("pane-comp", "children"),
              [Input("dd-scope", "value"), Input("rb-comp", "value")],
              [State("auth", "data")])
def on_comp(scope, kind, token):
    """[구성요소 탭] 시그니처 진단자의 11개 구성요소. 평소와 욕구 차이가 큰 항목을 따로 표시"""
    user, df = _ctx(token, scope)
    if df is None:
        return ""
    bar, heat, sig = components_figures(df, kind)
    if bar is None:
        return card(html.Div("구성요소 점수는 시그니처 진단자만 있습니다. 이 범위에는 시그니처 결과가 없습니다.", style=S_MUTED))
    gaps = []
    for _, r in sig.iterrows():
        for key, label, *_ in COMPONENTS:
            u, n = _num(r.get("comp_%s_usual" % key)), _num(r.get("comp_%s_needs" % key))
            if u is not None and n is not None and abs(u - n) >= GAP_ALERT:
                gaps.append(html.Li("%s · %s: 평소 %.0f (%s) ↔ 욕구 %.0f (%s)" % (
                    r["name"], label, u, comp_text(key, "usual", u), n, comp_text(key, "needs", n)),
                    style={"marginBottom": "4px", "fontSize": "14px"}))
    return [card(graph(bar), "팀 평균: 평소 행동 vs 욕구 (시그니처 %d명)" % len(sig),
                 "회색 띠(%d~%d)는 중간 범위. 막대 차이가 크면 '보이는 모습'과 '원하는 대우'가 다른 항목입니다." % (LOW, HIGH)),
            card(graph(heat), "구성원별 %s 점수" % ("욕구" if kind == "needs" else "평소 행동"),
                 "주황 = 높음, 파랑 = 낮음. 높고 낮음에 좋고 나쁨은 없습니다."),
            card(html.Ul(gaps) if gaps else html.Div("큰 차이 없음", style=S_MUTED),
                 "주의 깊게 볼 항목 (평소-욕구 차이 %d 이상)" % GAP_ALERT,
                 "겉으로는 괜찮아 보여도 실제로는 다른 대우를 원하는 부분입니다.")]


@app.callback([Output("dd-person", "options"), Output("dd-person", "value")],
              [Input("dd-scope", "value")],
              [State("auth", "data")])
def on_person_list(scope, token):
    """[개인별 탭] 범위 안 구성원 목록 (결과 없는 사람은 선택 불가)"""
    user, df = _ctx(token, scope)
    if df is None:
        return [], None
    opts = [{"label": "%s%s%s" % (r["name"], " · " + r["role"] if r["role"] else "", "" if r["has_birkman"] else " (결과 없음)"),
             "value": r["email"], "disabled": not r["has_birkman"]} for _, r in df.iterrows()]
    first = next((o["value"] for o in opts if not o["disabled"]), None)
    return opts, first


@app.callback(Output("pane-person", "children"),
              [Input("dd-person", "value"), Input("dd-scope", "value")],
              [State("auth", "data")])
def on_person(email, scope, token):
    """[개인별 탭] 한 사람의 맵, 성향 요약(레포트 문장 포함), '이 팀원과 일할 때' 팁, 구성요소/조직지향점/직업흥미.
    선택한 사람이 현재 범위에 없으면 아무것도 보여주지 않음 (권한 확인)
    """
    user, df = _ctx(token, scope)
    if df is None or not email:
        return ""
    sel = df[(df["email"] == email) & df["has_birkman"]]
    if sel.empty:  # 범위 밖 사람은 조회 불가
        return ""
    r = sel.iloc[0]
    audit("VIEW_PERSON", user["email"], email)
    rtype = {"signature": "시그니처", "basic": "베이직"}.get(r.get("report_type"), "-")
    layer_rows = []
    for l in LAYERS:
        c = color_of(r, l)
        txt = COLOR_INFO[c][l] if c else ""
        bullets = r.get("map_%s_bullets" % l)
        items = [b for b in str(bullets).split("\n") if b.strip()] if isinstance(bullets, str) else []
        row = [html.B(LAYER_INFO[l]["ko"] + "  "), chip(c), html.Span(txt, style={"fontSize": "14px"})]
        if items:
            row.append(html.Div("레포트: " + " · ".join(items[:8]),
                                style={"fontSize": "13px", "color": "#44505E", "background": "#F3F5F8",
                                       "borderRadius": "6px", "padding": "4px 8px", "marginTop": "3px"}))
        layer_rows.append(html.Div(row, style={"marginBottom": "10px", "lineHeight": "1.6"}))
    tips = []
    nc = color_of(r, "needs")
    if nc:
        tips.append(html.Li(COLOR_INFO[nc]["tip"]))
    extremes = []
    for key, label, *_ in COMPONENTS:
        n = _num(r.get("comp_%s_needs" % key))
        if n is not None and (n >= HIGH + 10 or n <= LOW - 10):
            extremes.append((abs(n - 50), label, comp_text(key, "needs", n)))
    for _, label, t in sorted(extremes, reverse=True)[:4]:
        tips.append(html.Li("%s: %s 제공" % (label, t)))
    sc = color_of(r, "stress")
    if sc:
        tips.append(html.Li("스트레스 신호: " + COLOR_INFO[sc]["stress"] + " 이런 모습이 보이면 위의 욕구가 채워지고 있는지 먼저 점검해 주세요.",
                            style={"color": "#8A4B08"}))
    warn = []
    for col, label in (("map_confidence", "버크만 맵"), ("orgfocus_confidence", "조직지향점")):
        v = _num(r.get(col))
        if v is not None and v < 0.8:
            warn.append("%s는 PDF 그림에서 자동 인식한 값입니다(신뢰도 %.2f). 원 레포트와 다르면 수기 보정해 주세요." % (label, v))
    blocks = [html.Div(w, style={"background": "#FFF6E0", "border": "1px solid #F0D58C", "borderRadius": "8px",
                                 "padding": "8px 12px", "marginBottom": "10px", "fontSize": "13px"}) for w in warn] + [
        html.Div(style={"display": "flex", "gap": "14px", "flexWrap": "wrap"}, children=[
            html.Div(card([graph(person_map_figure(r))], "%s 님의 버크만 맵" % r["name"],
                          " · ".join([x for x in (r["team"], r["part"], r["role"], rtype + " 진단") if x])), style={"flex": "1", "minWidth": "320px"}),
            html.Div(card(layer_rows + [html.Hr(style={"border": "none", "borderTop": "1px solid #E3E7ED"}),
                                        html.Div("이 팀원과 일할 때", style={"fontWeight": "700", "marginBottom": "4px"}),
                                        html.Ul(tips, style={"fontSize": "14px", "lineHeight": "1.7", "paddingLeft": "18px"})],
                          "성향 요약"), style={"flex": "1.2", "minWidth": "320px"}),
        ])]
    comp_rows = []
    for key, label, *_ in COMPONENTS:
        u, n = _num(r.get("comp_%s_usual" % key)), _num(r.get("comp_%s_needs" % key))
        if u is None and n is None:
            continue
        gap = u is not None and n is not None and abs(u - n) >= GAP_ALERT
        td = {"padding": "6px", "borderBottom": "1px solid #EEF1F4", "fontSize": "13px"}
        comp_rows.append(html.Tr([
            html.Td(html.B(label), style=td), html.Td("-" if u is None else "%.0f" % u, style=td),
            html.Td(comp_text(key, "usual", u), style=td), html.Td("-" if n is None else "%.0f" % n, style=td),
            html.Td(comp_text(key, "needs", n), style=td),
            html.Td("⚠ 차이 큼" if gap else "", style=dict(td, color="#B03A2E", fontWeight="700"))]))
    if comp_rows:
        th = lambda t: html.Th(t, style={"textAlign": "left", "padding": "6px", "borderBottom": "2px solid #E3E7ED", "fontSize": "13px"})
        blocks.append(card(html.Table([html.Thead(html.Tr([th(t) for t in ["구성요소", "평소", "평소 행동", "욕구", "필요한 것", ""]])),
                                       html.Tbody(comp_rows)], style={"width": "100%", "borderCollapse": "collapse"}),
                           "구성요소 (시그니처)", "평소 = 남에게 보이는 모습, 욕구 = 본인에게 필요한 대우. 차이가 크면 오해가 생기기 쉽습니다."))
    ofv = [(c, l, _num(r.get("orgfocus_%s" % c))) for c, l in ORGFOCUS]
    if any(v is not None for _, _, v in ofv):
        blocks.append(card([html.Div([html.Div(l, style={"width": "150px", "fontSize": "13px"}),
                                      html.Div(style={"background": COLOR_HEX[c], "height": "14px", "borderRadius": "7px",
                                                      "width": "%d%%" % int((v or 0) * 0.7)}),
                                      html.Span(" %.0f" % v if v is not None else " -", style={"fontSize": "13px", "marginLeft": "6px"})],
                                     style={"display": "flex", "alignItems": "center", "marginBottom": "6px"})
                            for c, l, v in ofv], "조직지향점"))
    jv = sorted([(_num(r.get("jobint_%s" % k)), l) for k, l in JOBINT if _num(r.get("jobint_%s" % k)) is not None], reverse=True)
    if jv:
        blocks.append(card(html.Div("상위: " + ", ".join("%s(%.0f)" % (l, v) for v, l in jv[:3]) +
                                    "   /   하위: " + ", ".join("%s(%.0f)" % (l, v) for v, l in jv[-2:]),
                                    style={"fontSize": "14px"}), "직업 흥미 (참고)"))
    return blocks


@app.callback(Output("pane-jobint", "children"),
              [Input("dd-scope", "value")],
              [State("auth", "data")])
def on_jobint(scope, token):
    """[직업 흥미 탭] 10개 영역 히트맵 (참고용)"""
    user, df = _ctx(token, scope)
    if df is None:
        return ""
    fig = jobint_figure(df)
    if fig is None:
        return card(html.Div("직업 흥미 데이터가 없습니다.", style=S_MUTED))
    return card(graph(fig), "직업 흥미 (참고용)", "어떤 종류의 활동에 흥미가 있는지에 대한 부수 정보입니다. 업무 배분이나 성장 대화 시 참고하세요.")


if __name__ == "__main__" and not IN_DSS:
    app.run(debug=False, host="127.0.0.1", port=int(os.environ.get("PORT", "8050")))
