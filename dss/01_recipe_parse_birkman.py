# -*- coding: utf-8 -*-
"""
==============================================================================
[Dataiku Python 레시피] 버크만 개인 레포트(JSON / PDF) -> 1인 1행 점수 데이터셋
==============================================================================

이 레시피가 하는 일
  1) 관리 폴더에 올려둔 개인별 버크만 파일(JSON 또는 PDF)을 하나씩 읽습니다.
  2) 각 파일에서 아래 항목을 찾아 "1인 1행" 표로 정리합니다.
       - 버크만 맵 : 흥미 / 평소행동 / 욕구 / 스트레스 의 위치(좌표)와 색(사분면)
       - 레포트 문장 : 맵 설명 페이지의 "• 항목" 문장들 (개인별 화면에 그대로 보여줌)
       - 조직지향점 : 빨강/초록/노랑/파랑 4개 점수
       - 구성요소   : 11개 항목의 평소/욕구 점수 (시그니처, 파일에 있을 때만)
       - 직업 흥미  : 10개 영역 점수 (참고용)
  3) 수기 보정 데이터셋이 있으면 그 값으로 덮어씁니다. (PDF 인식이 틀린 경우 등)
  4) 결과를 birkman_scores (웹앱이 읽는 데이터) 와 birkman_parse_log (파일별 처리 결과) 로 저장합니다.

입력
  - 관리 폴더(Managed Folder)  : birkman_reports        (JSON / PDF 파일을 그대로 업로드)
  - 데이터셋 (선택)             : birkman_manual_input   (수기 보정용. 없으면 건너뜀)
출력
  - 데이터셋 : birkman_scores     (1인 1행)
  - 데이터셋 : birkman_parse_log  (파일별 OK/PARTIAL/ERROR, 누락 항목, 어떤 키에서 읽었는지)

사용 방법
  * Dataiku Python 레시피의 코드 칸에 이 파일 전체를 붙여넣고 실행합니다.
  * 외부 모듈 없이 동작합니다. 단, PDF 를 읽으려면 코드 환경에 pdfplumber 또는 pypdf 가 필요합니다.
  * dataiku 가 없는 PC 에서도 테스트할 수 있습니다:
        python 01_recipe_parse_birkman.py <입력폴더> <출력폴더>

버크만 맵 좌표 기준 (이 데이터셋 안에서는 모두 같은 기준으로 맞춥니다)
  * 왼쪽 아래 끝 = (0, 0), 오른쪽 위 끝 = (100, 100), 가운데 = (50, 50)
  * 왼쪽 = 과제지향, 오른쪽 = 사람지향 / 위 = 외향(직접적), 아래 = 내향(간접적)
  * 사분면: 왼쪽 위 빨강, 오른쪽 위 초록, 왼쪽 아래 노랑, 오른쪽 아래 파랑
  * 화면에는 숫자가 아니라 맵 그림으로만 보여주므로, 좌표는 "대략적인 위치"로만 쓰입니다.
"""
from __future__ import print_function

import io
import json
import math
import os
import re
import sys

import pandas as pd

# =============================================================================
# 1. 설정 (보통은 여기만 수정하면 됩니다)
# =============================================================================
INPUT_FOLDER_NAME = "birkman_reports"         # 개인 레포트를 올려둔 관리 폴더 이름
MANUAL_DATASET_NAME = "birkman_manual_input"  # 수기 보정 데이터셋 이름 (없어도 됨)
OUTPUT_SCORES_NAME = "birkman_scores"         # 결과 데이터셋 이름 (웹앱이 이 이름으로 읽음)
OUTPUT_LOG_NAME = "birkman_parse_log"         # 처리 결과 로그 데이터셋 이름

# 파일명에서 사번을 뽑는 규칙. 권장 파일명: "사번_이름.json" 또는 "사번_이름.pdf"  예) 225007_홍길동.pdf
# 파일 안에 사번이 없을 때 이 규칙으로 조직 정보(org_members)와 연결합니다.
EMP_ID_FROM_FILENAME = r"(\d{5,10})"          # 연속된 숫자 5~10자리
NAME_FROM_FILENAME = r"([가-힣]{2,4})"         # 한글 2~4자

# --- 버크만 맵 좌표 변환 설정 ---------------------------------------------------
# 원본 좌표 형식이 파일마다 달라도 자동으로 0~100 기준으로 맞춥니다.
#   (a) 0~100 형식        : 그대로 사용
#   (b) 0~1 형식          : x100
#   (c) 가운데가 0 인 형식 : 음수가 있으면 이 형식으로 보고, 50 + 값 / 반경 * 50 으로 변환
#                           (사내 추출 JSON 의 map_symbols 가 이 형식: 예 x=0.835, y=-2.157)
MAP_SCALE = "auto"              # "auto" 또는 숫자(모든 좌표에 곱할 값)
MAP_Y_INVERTED = False          # 원본이 '아래로 갈수록 y 가 커지는' 화면 좌표라면 True
# (c) 형식에서 가운데부터 맵 끝까지의 거리(반경).
#   "auto" = max(4, 데이터 전체의 최대 |좌표| 올림값). 원본 맵의 끝 값을 알면 숫자로 고정해도 됩니다.
#   값을 바꿔도 사분면(색)은 바뀌지 않고, 점이 가운데/가장자리 쪽으로 조금 이동할 뿐입니다.
MAP_CENTER_HALF_RANGE = "auto"

# 사내 추출 JSON 의 "confidence"(자동 인식 신뢰도)가 이 값보다 낮으면 parse_note 와 웹앱에 경고 표시
CONFIDENCE_WARN = 0.8

# JSON 의 특정 키를 강제로 지정하고 싶을 때 사용 (자동 인식보다 우선).
# 경로는 birkman_parse_log 의 json_keys 컬럼에 보이는 표기를 그대로 씁니다.  예)
# JSON_PATH_OVERRIDES = {
#     "map_usual_x": "map_symbols.usual.x",
#     "comp_social_energy_usual": "components.사회적 에너지.usual",
# }
JSON_PATH_OVERRIDES = {}

# =============================================================================
# 2. 버크만 항목 정의
#    "별칭" = 파일의 키 이름/문장에서 그 항목을 알아보는 데 쓰는 단어들입니다.
#    (영문 소문자, 공백·밑줄 제거 기준으로 비교. 새 표기가 나오면 목록에 추가하면 됩니다.)
# =============================================================================
# 버크만 맵의 4가지 기호
MAP_LAYERS = {
    "interest": ["interest", "interests", "흥미", "관심"],          # 흥미 (별 기호)
    "usual": ["usual", "usualbehavior", "평소", "평상시", "일상", "평소행동"],  # 평소행동 (다이아몬드)
    "needs": ["need", "needs", "욕구", "니즈"],                     # 욕구 (원)
    "stress": ["stress", "스트레스"],                               # 스트레스행동 (사각형)
}
AXIS_X = ["x", "horizontal", "가로", "수평", "xaxis", "col", "column", "peopletask"]   # 가로 좌표 키
AXIS_Y = ["y", "vertical", "세로", "수직", "yaxis", "row", "directindirect"]          # 세로 좌표 키

# 사분면 색 이름 (레포트 표기: 빨강/초록/노랑/파랑)
COLORS = {
    "red": ["red", "레드", "빨강", "빨간", "적색"],
    "green": ["green", "그린", "초록", "녹색"],
    "yellow": ["yellow", "옐로", "옐로우", "노랑", "노란", "황색"],
    "blue": ["blue", "블루", "파랑", "파란", "청색"],
}

# 버크만 11개 구성요소 (시그니처 레포트). (내부키, [별칭...])
COMPONENTS = [
    ("social_energy", ["socialenergy", "acceptance", "사회적에너지", "수용"]),
    ("physical_energy", ["physicalenergy", "activity", "신체적에너지", "활동"]),
    ("emotional_energy", ["emotionalenergy", "empathy", "감정적에너지", "공감"]),
    ("self_consciousness", ["selfconsciousness", "esteem", "자의식", "자존"]),
    ("assertiveness", ["assertiveness", "authority", "주장성", "주장", "권위", "권한"]),
    ("insistence", ["insistence", "structure", "고집성", "구조", "체계"]),
    ("incentives", ["incentives", "incentive", "advantage", "인센티브", "이익", "보상"]),
    ("restlessness", ["restlessness", "change", "변화", "불안정"]),
    ("thought", ["thought", "사고", "숙고"]),
    ("autonomy", ["autonomy", "freedom", "자율", "자유"]),
    ("challenge", ["challenge", "도전"]),
]
COMP_USUAL = ["usual", "평소", "평상시", "일상"]   # 구성요소의 '평소' 점수를 뜻하는 단어
COMP_NEEDS = ["need", "needs", "욕구", "니즈"]     # 구성요소의 '욕구' 점수를 뜻하는 단어

# 조직지향점: 이 단어가 키 경로에 있어야 조직지향점 점수로 인정 (다른 점수와 섞이지 않게)
ORG_FOCUS_MARK = ["organizationalfocus", "organizational", "orgfocus", "orgorientation", "orientation",
                  "organization", "조직지향", "조직"]
# 조직지향점 4개 영역: 색 이름 또는 영역 이름으로 인식
ORG_FOCUS = {
    "red": ["operation", "technical", "technology", "implement", "실행", "운영", "기술"] + COLORS["red"],
    "green": ["sales", "marketing", "communicat", "영업", "마케팅", "소통"] + COLORS["green"],
    "yellow": ["admin", "fiscal", "finance", "관리", "재무", "행정", "회계"] + COLORS["yellow"],
    "blue": ["design", "strateg", "전략", "기획", "설계", "디자인"] + COLORS["blue"],
}

# 직업 흥미 10개 영역 (부수 정보). 버크만 코리아 표기: 숫자/관리/과학/문학/기술/음악/예술/야외/설득/사회복지
JOB_INTERESTS = [
    ("artistic", ["artistic", "예술"]),
    ("clerical", ["clerical", "사무", "관리"]),
    ("literary", ["literary", "문학", "언어"]),
    ("mechanical", ["mechanical", "기계", "기술"]),
    ("musical", ["musical", "음악"]),
    ("numerical", ["numerical", "수리", "숫자"]),
    ("outdoor", ["outdoor", "야외"]),
    ("persuasive", ["persuasive", "설득"]),
    ("scientific", ["scientific", "과학"]),
    ("social_service", ["socialservice", "사회봉사", "사회복지", "봉사", "복지"]),
]

# 인적사항 키 이름
NAME_KEYS = ["name", "fullname", "이름", "성명", "participantname", "username"]
EMP_ID_KEYS = ["empid", "employeeid", "employeeno", "empno", "사번", "직원번호", "사원번호"]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# PDF 레포트의 맵 설명 페이지 제목 (사내 추출 JSON 의 map_texts.raw 에서 확인한 실제 문구)
#   예) "흥미 탐색(별 기호)", "평소행동 탐색(다이아몬드 기호)", "욕구 탐색(원 기호)", "스트레스행동 탐색(사각 기호)"
PDF_MAP_HEADERS = {
    "interest": r"흥미\s*탐색",
    "usual": r"평소\s*행동\s*탐색",
    "needs": r"욕구\s*탐색",
    "stress": r"스트레스\s*행동\s*탐색",
}


def output_columns():
    """결과 데이터셋(birkman_scores)의 컬럼 순서를 정의합니다. 웹앱은 이 컬럼 이름으로 데이터를 읽습니다."""
    cols = ["emp_id", "name", "email", "report_type", "org_path", "source_file"]
    for layer in MAP_LAYERS:
        # map_usual_x / map_usual_y : 0~100 좌표, map_usual_color : 사분면 색, map_usual_bullets : 레포트 문장
        cols += ["map_%s_x" % layer, "map_%s_y" % layer, "map_%s_color" % layer, "map_%s_bullets" % layer]
    for c in COLORS:
        cols.append("orgfocus_%s" % c)                       # 조직지향점 색별 점수
    for key, _ in COMPONENTS:
        cols += ["comp_%s_usual" % key, "comp_%s_needs" % key]  # 구성요소 평소/욕구
    for key, _ in JOB_INTERESTS:
        cols.append("jobint_%s" % key)                       # 직업 흥미
    cols += ["map_confidence", "orgfocus_confidence", "parse_status", "parse_note"]
    return cols


# =============================================================================
# 3. 공통 도구 함수
# =============================================================================
def norm(s):
    """비교용으로 글자를 정리: 소문자로 바꾸고 공백, 밑줄, 점, 괄호 등을 제거 ("Social Energy" -> "socialenergy")"""
    return re.sub(r"[\s_\-\.\(\)/:]+", "", str(s).lower())


def to_number(v):
    """값을 숫자로 변환. 숫자나 "45", "45%" 같은 글자는 float 로, 그 외는 None"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*%?\s*$", v)
        if m:
            return float(m.group(1))
    return None


def to_color(v):
    """"노랑", "Yellow" 같은 색 이름을 내부 표기(red/green/yellow/blue)로 변환. 색이 아니면 None"""
    if not isinstance(v, str):
        return None
    n = norm(v)
    for c, aliases in COLORS.items():
        if n in [norm(a) for a in aliases]:
            return c
    return None


def token_hit(tokens, joined, alias):
    """경로에 별칭이 있는지 확인. 짧은 별칭(x, y)은 키 이름이 정확히 같을 때만, 긴 별칭은 일부만 포함돼도 인정"""
    a = norm(alias)
    if len(a) <= 2:
        return a in tokens
    return a in joined


def any_hit(tokens, joined, aliases):
    """별칭 목록 중 하나라도 경로에 있으면 True"""
    return any(token_hit(tokens, joined, a) for a in aliases)


def extract_bullets(text, limit=12):
    """레포트 문장에서 '• 항목'만 뽑아 중복을 제거합니다.
    PDF 줄바꿈 때문에 끊긴 항목은 이어 붙입니다.
      - "원\\n함"           -> "원함"           (한 글자 꼬리: 단어가 잘린 것)
      - "보내도록\\n해줄 때" -> "보내도록 해줄 때" (짧은 꼬리: 띄어서 연결)
    """
    # 맵 그림의 축 이름("외향/사람지향…")이나 다음 페이지 표시가 나오면 그 앞까지만 사용
    body = re.split(r"\n\s*외향\s*\n|--- page", text)[0]
    items = []
    for chunk in body.split("•")[1:]:
        lines = [l.strip() for l in chunk.strip().split("\n")]
        item = lines[0]
        for l in lines[1:]:
            if l and len(l) <= 8:          # 짧은 다음 줄 = 앞 항목의 꼬리
                item += l if len(l) == 1 else " " + l
            else:                          # 긴 줄 = 새 설명문 시작 -> 중단
                break
        item = re.sub(r"\s+", " ", item).strip(" .")
        if item and item not in items and len(item) <= 60:
            items.append(item)
    return items[:limit]


# =============================================================================
# 4. JSON 파일 읽기
#    키 이름이 파일마다 달라도 "별칭"으로 자동 인식합니다.
#    어떤 키에서 무엇을 읽었는지는 birkman_parse_log.matched_paths 에 남습니다.
# =============================================================================
# 리스트 안의 항목을 구분하는 이름표 키. 예) interests: [{"name": "숫자", "score": 99}] -> 경로 "interests.숫자.score"
LIST_LABEL_KEYS = ["name", "title", "label", "key", "type", "category", "component", "area",
                   "symbol", "layer", "id", "이름", "항목", "구분", "영역", "명칭"]


def flatten_json(obj, path=None, out=None):
    """중첩된 JSON 을 (경로, 값) 목록으로 펼칩니다.
    예) {"map_symbols": {"usual": {"x": 0.8}}} -> (["map_symbols", "usual", "x"], 0.8)
    리스트 안의 dict 는 순번 대신 name/label 값을 경로에 넣어 "interests.숫자.score" 처럼 만듭니다.
    """
    if path is None:
        path = []
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten_json(v, path + [str(k)], out)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            label = None
            if isinstance(item, dict):
                for lk in item:
                    if norm(lk) in [norm(x) for x in LIST_LABEL_KEYS] and isinstance(item[lk], str):
                        label = item[lk]
                        break
            flatten_json(item, path + [label if label else str(i)], out)
    else:
        out.append((path, obj))
    return out


def path_str(path):
    """경로 리스트를 "a.b.c" 형태의 글자로"""
    return ".".join(path)


def find_value(flat, must_groups, exclude=None, want="number", prefer=None):
    """펼친 JSON 에서 조건에 맞는 값을 하나 찾습니다.
    - must_groups : 각 그룹의 별칭이 하나 이상 경로에 있어야 함 (예: [평소 별칭, x 별칭])
    - exclude     : 이 별칭이 경로에 있으면 제외 (예: 평소를 찾을 때 욕구/스트레스 경로 제외)
    - want        : "number" 면 숫자, "color" 면 색 이름을 찾음
    - prefer      : 이 단어가 경로에 있으면 우선 (예: "map")
    후보가 여럿이면 우선 단어가 많고 경로가 짧은 것을 고릅니다. 반환: (값, 찾은 경로)
    """
    exclude = exclude or []
    prefer = prefer or []
    cands = []
    for path, val in flat:
        tokens = set(norm(p) for p in path)
        joined = norm("".join(path))
        if not all(any_hit(tokens, joined, g) for g in must_groups):
            continue
        if exclude and any_hit(tokens, joined, exclude):
            continue
        v = to_number(val) if want == "number" else to_color(val)
        if v is None:
            continue
        score = len(path) - 3 * sum(1 for p in prefer if token_hit(tokens, joined, p))
        cands.append((score, path_str(path), v))
    if not cands:
        return None, None
    cands.sort(key=lambda x: (x[0], x[1]))
    return cands[0][2], cands[0][1]


def lookup_path(flat, dotted):
    """JSON_PATH_OVERRIDES 에 적은 "a.b.c" 경로의 값을 그대로 가져옴"""
    target = norm(dotted)
    for path, val in flat:
        if norm(path_str(path)) == target:
            return val
    return None


def parse_json_record(data):
    """JSON 한 사람분을 읽어 결과 행(dict)으로 만듭니다.
    반환: (rec = 결과값, used = 항목별로 읽은 키 경로, keys = 파일의 전체 키 목록)
    """
    flat = flatten_json(data)
    rec, used = {}, {}

    # --- (1) 버크만 맵: 기호별 x/y 좌표와 사분면 색 -------------------------------
    for layer, aliases in MAP_LAYERS.items():
        # 다른 기호의 별칭이 들어간 경로는 제외 (예: 평소를 찾을 때 needs/stress 경로 제외)
        excl = [a for ol, oa in MAP_LAYERS.items() if ol != layer for a in oa]
        # x/y 키가 반드시 있어야 좌표로 인정 (직업 흥미 점수와 섞이지 않게)
        for axis, axis_alias in (("x", AXIS_X), ("y", AXIS_Y)):
            v, p = find_value(flat, [aliases, axis_alias], exclude=excl, prefer=["map", "맵", "grid", "symbol"])
            if v is not None:
                rec["map_%s_%s" % (layer, axis)] = v
                used["map_%s_%s" % (layer, axis)] = p
        # 색: "노랑" 같은 값을 가진 키 (map_texts.*.color, map_symbols.*.quadrant 등)
        c, p = find_value(flat, [aliases], exclude=excl, want="color", prefer=["map", "맵", "color", "색", "quadrant"])
        if c:
            rec["map_%s_color" % layer] = c
            used["map_%s_color" % layer] = p

    # --- (2) 조직지향점: org_orientation.scores.노랑 = 70 같은 값 -----------------
    for color, aliases in ORG_FOCUS.items():
        v, p = find_value(flat, [ORG_FOCUS_MARK, aliases])
        if v is not None:
            rec["orgfocus_%s" % color] = v
            used["orgfocus_%s" % color] = p

    # --- (3) 구성요소 11개: 평소 / 욕구 점수 (파일에 있을 때만) --------------------
    for key, aliases in COMPONENTS:
        v, p = find_value(flat, [aliases, COMP_USUAL], exclude=COMP_NEEDS + ["stress", "스트레스"])
        if v is not None:
            rec["comp_%s_usual" % key] = v
            used["comp_%s_usual" % key] = p
        v, p = find_value(flat, [aliases, COMP_NEEDS], exclude=COMP_USUAL)
        if v is not None:
            rec["comp_%s_needs" % key] = v
            used["comp_%s_needs" % key] = p

    # --- (4) 직업 흥미 10개: interests.숫자.score = 99 같은 값 --------------------
    for key, aliases in JOB_INTERESTS:
        v, p = find_value(flat, [aliases], exclude=AXIS_X + AXIS_Y)
        if v is not None:
            rec["jobint_%s" % key] = v
            used["jobint_%s" % key] = p

    # --- (5) 사용자가 직접 지정한 경로가 있으면 그것을 최우선 ----------------------
    for field, dotted in JSON_PATH_OVERRIDES.items():
        val = lookup_path(flat, dotted)
        if val is not None:
            rec[field] = to_color(val) if field.endswith("_color") else to_number(val)
            used[field] = dotted

    # --- (6) 인적사항: 이름 / 사번 / 메일 ------------------------------------------
    # 얕은 경로부터 확인 (리스트 항목의 "name"(예: 흥미 영역명)이 사람 이름으로 잡히지 않게)
    for path, val in sorted(flat, key=lambda pv: len(pv[0])):
        if not isinstance(val, (str, int)) or isinstance(val, bool):
            continue
        if len(path) > 3:
            continue
        last = norm(path[-1]) if path else ""
        if "name" not in rec and isinstance(val, str) and last in [norm(k) for k in NAME_KEYS]:
            rec["name"] = val.strip()
        if "emp_id" not in rec and last in [norm(k) for k in EMP_ID_KEYS]:
            rec["emp_id"] = str(val).strip()
        if "email" not in rec and isinstance(val, str) and EMAIL_RE.fullmatch(val.strip()):
            rec["email"] = val.strip().lower()

    # --- (7) 레포트 문장(• 항목): 개인별 화면에 "레포트" 문장으로 표시 -------------
    for layer, aliases in MAP_LAYERS.items():
        excl = [a for ol, oa in MAP_LAYERS.items() if ol != layer for a in oa]
        texts = []
        for path, val in flat:
            if not isinstance(val, str) or len(val) < 30 or "•" not in val:
                continue
            tokens = set(norm(x) for x in path)
            joined = norm("".join(path))
            if any_hit(tokens, joined, aliases) and not any_hit(tokens, joined, excl):
                texts.append(val)
        if texts:
            rec["map_%s_bullets" % layer] = "\n".join(extract_bullets(max(texts, key=len)))

    # --- (8) 자동 인식 신뢰도(confidence): 낮으면 화면에 "원 레포트 확인" 경고 ------
    confs = {"map": [], "org": []}
    for path, val in flat:
        tokens = set(norm(x) for x in path)
        joined = norm("".join(path))
        if not any_hit(tokens, joined, ["confidence", "신뢰도"]) or to_number(val) is None:
            continue
        if any_hit(tokens, joined, ORG_FOCUS_MARK):
            confs["org"].append(to_number(val))
        elif any(any_hit(tokens, joined, a) for a in MAP_LAYERS.values()):
            confs["map"].append(to_number(val))
    if confs["map"]:
        rec["map_confidence"] = min(confs["map"])
    if confs["org"]:
        rec["orgfocus_confidence"] = min(confs["org"])

    # --- (9) 소속 경로 (예: ["경영지원센터", "인사팀"]) — 참고용. 권한은 조직 엑셀 기준 ----
    if isinstance(data, dict):
        for k, v in data.items():
            if norm(k) in ("orgpath", "조직경로", "소속") and isinstance(v, list):
                rec["org_path"] = " > ".join(str(x) for x in v)

    # --- (10) 레포트 종류: report_type 같은 명시적 키를 우선, 없으면 전체 내용에서 추정 ----
    explicit = None
    for path, val in flat:
        if len(path) <= 2 and isinstance(val, str) and norm(path[-1]) in (
                "reporttype", "report", "type", "진단종류", "리포트종류", "레포트종류", "버전", "version"):
            explicit = val.lower()
            break
    blob = explicit if explicit else json.dumps(data, ensure_ascii=False).lower()
    if "signature" in blob or "시그니처" in blob:
        rec["report_type"] = "signature"
    elif "basic" in blob or "베이직" in blob:
        rec["report_type"] = "basic"

    # 파일의 전체 키 목록 (값 없이 경로만). 형식이 바뀌었을 때 확인용으로 로그에 남김
    keys = sorted(set(path_str([re.sub(r"^\d+$", "#", p) for p in path]) for path, _ in flat))
    return rec, used, keys


# =============================================================================
# 5. PDF 파일 읽기 (JSON 이 없을 때 사용)
#    PDF 에서 '글자'를 꺼내 아래 항목을 찾습니다. (스캔 이미지 PDF 는 불가)
#      - 맵 4개 기호의 색 + 레포트 문장 : 맵 설명 페이지 문장으로 확실하게 인식
#      - 직업 흥미 / 구성요소           : "항목명 숫자" 형태의 줄이 있을 때만 인식
#      - 맵 좌표 / 조직지향점           : PDF 안에서 그림(도형/이미지)이라 글자로는 읽을 수 없음
#                                       -> 맵은 사분면 색으로 대략 위치 표시, 조직지향점은 수기 보정
# =============================================================================
def pdf_to_text(raw):
    """PDF 의 글자를 페이지 순서대로 꺼냅니다. 페이지 사이에는 "--- page N ---" 표시를 넣습니다.
    코드 환경에 있는 라이브러리(pdfplumber -> pypdf -> PyPDF2 순서)를 사용합니다.
    """
    errors = []
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            return "\n".join("%s\n--- page %d ---" % (p.extract_text() or "", i + 1) for i, p in enumerate(pdf.pages))
    except Exception as e:  # noqa
        errors.append("pdfplumber: %s" % e)
    for mod in ("pypdf", "PyPDF2"):
        try:
            lib = __import__(mod)
            reader = lib.PdfReader(io.BytesIO(raw))
            return "\n".join("%s\n--- page %d ---" % (p.extract_text() or "", i + 1) for i, p in enumerate(reader.pages))
        except Exception as e:  # noqa
            errors.append("%s: %s" % (mod, e))
    raise RuntimeError("PDF 텍스트 추출 실패 (코드 환경에 pdfplumber 또는 pypdf 설치 필요) / " + " | ".join(errors))


def _alias_regex(aliases):
    """별칭 목록을 정규식으로 변환. PDF 에서 글자 사이에 공백/줄바꿈이 끼어도 찾을 수 있게 함"""
    parts = []
    for a in aliases:
        if len(a) <= 2 and re.match(r"^[a-z]+$", a):
            continue
        parts.append(r"\s*".join(re.escape(ch) for ch in a))
    return "(?:%s)" % "|".join(parts)


_COLOR_WORDS = "(빨강|초록|노랑|파랑|red|green|yellow|blue)"


def parse_pdf_map_sections(t, rec):
    """맵 설명 페이지("흥미 탐색(별 기호)" 등)를 찾아 기호별 색과 "• 항목" 문장을 읽습니다.
    색은 "당신의 별 기호는 노랑에 위치합니다" 처럼 '기호는' 바로 뒤에 나오는 색 이름을 사용합니다.
    """
    # 각 제목이 나오는 위치를 찾고, 다음 제목 전까지를 그 기호의 설명으로 봅니다.
    starts = []
    for layer, rx in PDF_MAP_HEADERS.items():
        m = re.search(rx, t)
        if m:
            starts.append((m.start(), layer))
    starts.sort()
    for i, (pos, layer) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else min(len(t), pos + 3000)
        section = t[pos:end]
        m = re.search(r"기호는\s*" + _COLOR_WORDS, section)
        if m:
            rec["map_%s_color" % layer] = to_color(m.group(1))
        bullets = extract_bullets(section)
        if bullets:
            rec["map_%s_bullets" % layer] = "\n".join(bullets)


def parse_pdf_text(text):
    """PDF 에서 꺼낸 글자로 결과 행(dict)을 만듭니다. 못 찾은 항목은 비워 둡니다(수기 보정 가능)."""
    rec = {}
    t = text.replace(" ", " ")
    low = t.lower()

    # 레포트 종류 (머리말/꼬리말의 "버크만 시그니처 리포트" 등)
    if "signature" in low or "시그니처" in low:
        rec["report_type"] = "signature"
    elif "basic" in low or "베이직" in low:
        rec["report_type"] = "basic"
    m = EMAIL_RE.search(t)
    if m:
        rec["email"] = m.group(0).lower()

    # 맵 색 + 레포트 문장
    parse_pdf_map_sections(t, rec)

    # 구성요소: 한 줄에 "항목명 평소점수 욕구점수" 가 있는 경우만 인식 (잘못 읽는 것을 막기 위해 엄격하게)
    for key, aliases in COMPONENTS:
        rx = re.compile(r"^\s*" + _alias_regex(aliases) + r"[^\d\n]{0,20}?(\d{1,2})[^\d\n]{1,15}?(\d{1,2})\s*$",
                        re.I | re.M)
        mm = rx.search(t)
        if mm:
            rec["comp_%s_usual" % key] = float(mm.group(1))
            rec["comp_%s_needs" % key] = float(mm.group(2))

    # 직업 흥미: 한 줄에 "숫자 99" 처럼 항목명과 점수만 있는 경우만 인식
    for key, aliases in JOB_INTERESTS:
        rx = re.compile(r"^\s*" + _alias_regex(aliases) + r"\s*[:：]?\s*(\d{1,2})\s*$", re.I | re.M)
        mm = rx.search(t)
        if mm:
            rec["jobint_%s" % key] = float(mm.group(1))

    # 조직지향점: "조직지향" 제목 근처에서 "영역명 숫자" 줄이 있으면 인식 (보통은 그림이라 없음)
    mark = re.search(_alias_regex(ORG_FOCUS_MARK), t, re.I)
    if mark:
        block = t[mark.start(): mark.start() + 1500]
        for color, aliases in ORG_FOCUS.items():
            rx = re.compile(r"^\s*" + _alias_regex(aliases) + r"[^\d\n]{0,15}?(\d{1,3})\s*%?\s*$", re.I | re.M)
            mm = rx.search(block)
            if mm:
                rec["orgfocus_%s" % color] = float(mm.group(1))
    return rec


# =============================================================================
# 6. 정리 / 보정
# =============================================================================
def apply_filename_info(rec, filename):
    """파일 안에 사번/이름/레포트 종류가 없으면 파일명("225007_홍길동_시그니처.pdf")에서 채웁니다."""
    base = os.path.splitext(os.path.basename(filename))[0]
    if not rec.get("emp_id"):
        m = re.search(EMP_ID_FROM_FILENAME, base)
        if m:
            rec["emp_id"] = m.group(1)
    if not rec.get("name"):
        m = re.search(NAME_FROM_FILENAME, base.replace("시그니처", "").replace("베이직", ""))
        if m:
            rec["name"] = m.group(1)
    if not rec.get("report_type"):
        lb = base.lower()
        if "signature" in lb or "시그니처" in lb:
            rec["report_type"] = "signature"
        elif "basic" in lb or "베이직" in lb:
            rec["report_type"] = "basic"
    return rec


def quadrant(x, y):
    """0~100 좌표로 사분면 색 계산. 왼쪽 위 빨강 / 오른쪽 위 초록 / 왼쪽 아래 노랑 / 오른쪽 아래 파랑"""
    if y >= 50:
        return "red" if x < 50 else "green"
    return "yellow" if x < 50 else "blue"


def finalize(df):
    """모든 사람의 결과를 같은 기준으로 맞춥니다.
    - 컬럼 순서 통일
    - 맵 좌표를 0~100 (왼쪽 아래 0,0 / 오른쪽 위 100,100) 으로 변환
    - 점수를 0~100 범위로 제한
    - 좌표만 있고 색이 없으면 좌표로 색 계산
    - 레포트 종류가 비어 있으면 추정
    - 신뢰도 낮은 항목을 parse_note 에 표시
    """
    for c in output_columns():
        if c not in df.columns:
            df[c] = None
    df = df[output_columns()].copy()

    # --- 맵 좌표 변환 (사람마다 원본 형식이 다를 수 있어 사람(행) 단위로 판단) ---
    xy = [c for c in df.columns if c.startswith("map_") and (c.endswith("_x") or c.endswith("_y"))]
    for c in xy:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if MAP_SCALE == "auto":
        # (c) 가운데가 0 인 형식: 음수 좌표가 하나라도 있는 사람
        centered = (df[xy] < 0).any(axis=1)
        if centered.any():
            if MAP_CENTER_HALF_RANGE == "auto":
                half = max(4.0, math.ceil(df.loc[centered, xy].abs().max().max()))
            else:
                half = float(MAP_CENTER_HALF_RANGE)
            print("[INFO] 가운데=0 좌표 %d명 -> 반경 %.1f 기준으로 0~100 변환" % (centered.sum(), half))
            df.loc[centered, xy] = 50 + df.loc[centered, xy] / half * 50
        # (b) 0~1 형식: 좌표가 모두 1 이하인 사람은 x100
        row_max = df[xy].max(axis=1)
        factor = row_max.map(lambda m: 100.0 if (pd.notna(m) and m <= 1.0) else 1.0)
        factor[centered] = 1.0
        df[xy] = df[xy].mul(factor, axis=0)
    else:
        df[xy] = df[xy] * float(MAP_SCALE)
    if MAP_Y_INVERTED:
        for c in xy:
            if c.endswith("_y"):
                df[c] = 100 - df[c]

    # --- 점수는 0~100 범위로 제한 ---
    num_cols = [c for c in df.columns if c.startswith(("orgfocus_", "comp_", "jobint_"))]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df[xy + num_cols] = df[xy + num_cols].clip(lower=0, upper=100)

    # --- 좌표가 있고 색이 없으면 좌표로 사분면 색 계산 ---
    for layer in MAP_LAYERS:
        x, y, col = "map_%s_x" % layer, "map_%s_y" % layer, "map_%s_color" % layer
        mask = df[col].isna() & df[x].notna() & df[y].notna()
        df.loc[mask, col] = [quadrant(a, b) for a, b in zip(df.loc[mask, x], df.loc[mask, y])]

    # --- 레포트 종류가 비었으면: 구성요소 점수가 5개 이상이면 시그니처, 아니면 베이직 ---
    comp_cols = [c for c in df.columns if c.startswith("comp_")]
    has_comp = df[comp_cols].notna().sum(axis=1) >= 5
    df.loc[df["report_type"].isna() & has_comp, "report_type"] = "signature"
    df.loc[df["report_type"].isna(), "report_type"] = "basic"

    # --- 자동 인식 신뢰도가 낮은 항목을 parse_note 에 추가 ---
    for c in ("map_confidence", "orgfocus_confidence"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    low = []
    for _, r in df.iterrows():
        n = []
        if pd.notna(r["map_confidence"]) and r["map_confidence"] < CONFIDENCE_WARN:
            n.append("맵 인식 신뢰도 %.2f" % r["map_confidence"])
        if pd.notna(r["orgfocus_confidence"]) and r["orgfocus_confidence"] < CONFIDENCE_WARN:
            n.append("조직지향점 인식 신뢰도 %.2f" % r["orgfocus_confidence"])
        low.append(" / ".join(n))
    df["parse_note"] = [(" / ".join(x for x in (str(a) if pd.notna(a) else "", b) if x))
                        for a, b in zip(df["parse_note"], low)]

    # --- 글자 컬럼은 글자형으로 통일 (Dataiku 스키마가 섞이지 않게) ---
    for c in ["emp_id", "name", "email", "report_type", "org_path", "source_file", "parse_status", "parse_note"] + \
            ["map_%s_bullets" % l for l in MAP_LAYERS]:
        df[c] = df[c].astype(object).where(df[c].notna(), None)
        df[c] = df[c].map(lambda v: None if v is None else str(v))
    return df


def missing_summary(rec):
    """주요 항목 중 빠진 것을 목록으로 반환 (parse_log 의 missing / status 판단에 사용)"""
    miss = []
    if not any(rec.get("map_usual_%s" % a) is not None for a in "xy") and not rec.get("map_usual_color"):
        miss.append("맵(평소)")
    if not any(rec.get("orgfocus_%s" % c) is not None for c in COLORS):
        miss.append("조직지향점")
    if rec.get("report_type") == "signature" and \
            sum(1 for k, _ in COMPONENTS if rec.get("comp_%s_usual" % k) is not None) < 5:
        miss.append("구성요소")
    if not rec.get("emp_id") and not rec.get("email"):
        miss.append("사번/이메일")
    return miss


def merge_manual(df, manual):
    """수기 보정 데이터셋을 반영합니다.
    - emp_id(사번)가 같은 사람의 '값을 넣은 칸만' 덮어씁니다. (빈 칸은 기존 값 유지)
    - 파일이 없는 사람이면 새 행으로 추가합니다.
    """
    if manual is None or len(manual) == 0:
        return df
    manual = manual.copy()
    manual.columns = [str(c).strip() for c in manual.columns]
    if "emp_id" not in manual.columns:
        print("[WARN] 수기 보정 데이터에 emp_id 컬럼이 없어 무시합니다.")
        return df
    manual["emp_id"] = manual["emp_id"].astype(str).str.strip()
    df = df.copy()
    df["emp_id"] = df["emp_id"].astype(object)
    for _, mrow in manual.iterrows():
        eid = mrow["emp_id"]
        if not eid or eid == "nan":
            continue
        idx = df.index[df["emp_id"] == eid]
        vals = {c: mrow[c] for c in manual.columns
                if c in df.columns and pd.notna(mrow[c]) and str(mrow[c]).strip() != ""}
        if len(idx) == 0:
            vals.setdefault("source_file", "manual_input")
            vals["parse_status"] = "MANUAL"
            df = pd.concat([df, pd.DataFrame([vals])], ignore_index=True)
        else:
            for c, v in vals.items():
                df.loc[idx, c] = v
            df.loc[idx, "parse_note"] = (df.loc[idx, "parse_note"].fillna("") + " / 수기보정 반영").str.strip(" /")
    return df


# =============================================================================
# 7. 파일 읽기/쓰기 (Dataiku 환경 / PC 로컬 테스트 환경)
#    같은 처리 로직을 두 환경에서 쓰기 위해 입출력만 분리했습니다.
# =============================================================================
class DataikuIO(object):
    """Dataiku 안에서 실행될 때: 관리 폴더에서 파일을 읽고 데이터셋으로 저장"""

    def __init__(self):
        import dataiku
        self.dataiku = dataiku
        self.folder = dataiku.Folder(INPUT_FOLDER_NAME)

    def list_files(self):
        """폴더 안의 모든 파일 경로 (하위 폴더 포함)"""
        return sorted(self.folder.list_paths_in_partition())

    def read(self, path):
        """파일 내용을 bytes 로 읽음"""
        with self.folder.get_download_stream(path) as f:
            return f.read()

    def read_manual(self):
        """수기 보정 데이터셋 읽기. 레시피 입력에 추가하지 않았거나 없으면 None (건너뜀)"""
        try:
            return self.dataiku.Dataset(MANUAL_DATASET_NAME).get_dataframe(infer_with_pandas=False)
        except Exception as e:  # noqa
            print("[INFO] 수기 보정 데이터셋 없음/읽기 실패 -> 건너뜀:", e)
            return None

    def write(self, name, df):
        """결과를 출력 데이터셋에 저장 (스키마 자동 생성)"""
        self.dataiku.Dataset(name).write_with_schema(df)


class LocalIO(object):
    """PC 에서 테스트할 때: 일반 폴더에서 파일을 읽고 CSV 로 저장"""

    def __init__(self, in_dir, out_dir):
        self.in_dir, self.out_dir = in_dir, out_dir

    def list_files(self):
        out = []
        for root, _, files in os.walk(self.in_dir):
            for f in files:
                out.append(os.path.relpath(os.path.join(root, f), self.in_dir))
        return sorted(out)

    def read(self, path):
        with open(os.path.join(self.in_dir, path), "rb") as f:
            return f.read()

    def read_manual(self):
        p = os.path.join(self.out_dir, MANUAL_DATASET_NAME + ".csv")
        return pd.read_csv(p, dtype=str) if os.path.exists(p) else None

    def write(self, name, df):
        if not os.path.isdir(self.out_dir):
            os.makedirs(self.out_dir)
        df.to_csv(os.path.join(self.out_dir, name + ".csv"), index=False, encoding="utf-8-sig")


def decode_json_bytes(raw):
    """JSON 파일을 여러 인코딩(UTF-8, CP949 등)으로 시도해서 읽음"""
    for enc in ("utf-8-sig", "utf-8", "cp949", "utf-16"):
        try:
            return json.loads(raw.decode(enc))
        except Exception:  # noqa
            continue
    raise ValueError("JSON 디코딩 실패")


# =============================================================================
# 8. 전체 실행 흐름
# =============================================================================
def run(io_):
    """폴더의 모든 파일을 처리해서 결과/로그 데이터셋을 저장합니다.
    파일 하나가 실패해도 나머지는 계속 처리하고, 실패 이유는 로그에 남깁니다.
    """
    records, logs = [], []
    for path in io_.list_files():
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".json", ".pdf"):
            continue          # JSON / PDF 외 파일은 무시
        log = {"source_file": path, "file_type": ext[1:], "status": "OK", "missing": "", "message": "",
               "matched_paths": "", "json_keys": ""}
        try:
            raw = io_.read(path)
            if ext == ".json":
                data = decode_json_bytes(raw)
                # 한 파일에 여러 명이 [ {...}, {...} ] 배열로 들어있는 경우도 처리
                items = data if isinstance(data, list) and data and isinstance(data[0], dict) else [data]
                for i, item in enumerate(items):
                    rec, used, keys = parse_json_record(item)
                    rec["source_file"] = path if len(items) == 1 else "%s#%d" % (path, i)
                    if len(items) == 1:
                        apply_filename_info(rec, path)
                    miss = missing_summary(rec)
                    rec["parse_status"] = "OK" if not miss else "PARTIAL"
                    rec["parse_note"] = ("누락: " + ", ".join(miss)) if miss else ""
                    records.append(rec)
                    if i == 0:
                        log["json_keys"] = "\n".join(keys)
                        log["matched_paths"] = "\n".join("%s <- %s" % (k, v) for k, v in sorted(used.items()))
                    if miss:
                        log["status"] = "PARTIAL"
                        log["missing"] = ", ".join(miss)
                if len(items) > 1:
                    log["message"] = "%d명 포함" % len(items)
            else:
                text = pdf_to_text(raw)
                rec = parse_pdf_text(text)
                rec["source_file"] = path
                apply_filename_info(rec, path)
                miss = missing_summary(rec)
                rec["parse_status"] = "OK" if not miss else "PARTIAL"
                rec["parse_note"] = ("누락: " + ", ".join(miss)) if miss else ""
                records.append(rec)
                log["status"] = rec["parse_status"]
                log["missing"] = ", ".join(miss)
                log["message"] = "텍스트 %d자 추출" % len(text)
                if len(text.replace("--- page", "").strip()) < 200:
                    log["message"] += " (글자가 거의 없음: 스캔 이미지 PDF 일 수 있음)"
        except Exception as e:  # noqa
            log["status"] = "ERROR"
            log["message"] = str(e)[:500]
        logs.append(log)

    df = pd.DataFrame(records) if records else pd.DataFrame(columns=output_columns())
    df = merge_manual(df, io_.read_manual())
    df = finalize(df)

    # 같은 사람 파일이 여러 개면(예: JSON 과 PDF 둘 다, 재진단) 채워진 값이 가장 많은 것 하나만 남김
    df["_filled"] = df.notna().sum(axis=1)
    df["_key"] = df["emp_id"].fillna(df["email"]).fillna(df["source_file"])
    df = df.sort_values("_filled", ascending=False).drop_duplicates("_key").drop(columns=["_filled", "_key"])
    df = df.sort_values(["emp_id", "name"], na_position="last").reset_index(drop=True)

    log_df = pd.DataFrame(logs, columns=["source_file", "file_type", "status", "missing", "message",
                                         "matched_paths", "json_keys"])
    io_.write(OUTPUT_SCORES_NAME, df)
    io_.write(OUTPUT_LOG_NAME, log_df)
    print("[DONE] 인원 %d명 / 파일 %d개 (OK %d, PARTIAL %d, ERROR %d)" % (
        len(df), len(log_df), (log_df.status == "OK").sum(), (log_df.status == "PARTIAL").sum(),
        (log_df.status == "ERROR").sum()))
    return df, log_df


# Dataiku 안이면 바로 실행, PC 에서는 명령줄 인자(입력폴더, 출력폴더)로 실행
try:
    import dataiku  # noqa: F401
    _IN_DSS = True
except ImportError:
    _IN_DSS = False

if _IN_DSS:
    run(DataikuIO())
elif __name__ == "__main__":
    if len(sys.argv) < 3:
        print("사용법: python 01_recipe_parse_birkman.py <입력폴더> <출력폴더>")
        sys.exit(1)
    run(LocalIO(sys.argv[1], sys.argv[2]))
