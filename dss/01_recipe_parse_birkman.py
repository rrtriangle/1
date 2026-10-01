# -*- coding: utf-8 -*-
"""
[Dataiku Python 레시피] 버크만 개인 레포트(JSON / PDF) -> 정규화된 점수 데이터셋

입력
  - 관리 폴더(Managed Folder)  : birkman_reports        (JSON / PDF 파일을 그대로 업로드)
  - 데이터셋 (선택)             : birkman_manual_input   (수기 보정용. 없으면 건너뜀)
출력
  - 데이터셋 : birkman_scores     (1인 1행, 웹앱이 읽는 데이터)
  - 데이터셋 : birkman_parse_log  (파일별 파싱 결과 / 누락 항목 / JSON 키 구조)

* 이 파일 하나만 레시피에 통째로 붙여넣으면 됩니다. (외부 모듈 의존 없음, PDF는 pdfplumber/pypdf/PyPDF2 중 있는 것 사용)
* dataiku 패키지가 없는 PC에서도 테스트할 수 있도록 로컬 모드를 지원합니다.
    python 01_recipe_parse_birkman.py <입력폴더> <출력폴더>
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
# 1. 설정 (필요하면 여기만 수정)
# =============================================================================
INPUT_FOLDER_NAME = "birkman_reports"        # 관리 폴더 이름
MANUAL_DATASET_NAME = "birkman_manual_input"  # 수기 보정 데이터셋 이름 (없어도 됨)
OUTPUT_SCORES_NAME = "birkman_scores"
OUTPUT_LOG_NAME = "birkman_parse_log"

# 파일명에서 사번을 뽑는 정규식. 파일명 규칙 권장: "사번_이름.json"  예) 2023015_홍길동.json
EMP_ID_FROM_FILENAME = r"(\d{5,10})"
# 파일명에서 이름(한글 2~4자)을 뽑는 정규식
NAME_FROM_FILENAME = r"([가-힣]{2,4})"

# 버크만 맵 좌표 스케일. "auto" 이면 사람별로 좌표가 모두 1 이하일 때 x100 합니다.
# 숫자를 넣으면 그 값을 곱합니다. (예: 원본이 0~50 이면 2)
MAP_SCALE = "auto"
# 원본 y축이 위로 갈수록 값이 작아지는(화면 좌표) 형식이면 True
MAP_Y_INVERTED = False
# 원점(0,0)이 맵 중앙인 좌표(음수 포함, 예: x=0.835, y=-2.157)일 때 중앙에서 끝까지의 거리.
# "auto" 이면 max(4, 전체 데이터의 최대 |좌표| 올림) 을 사용합니다.
MAP_CENTER_HALF_RANGE = "auto"
# 자동 인식 신뢰도(confidence)가 이 값보다 낮으면 parse_note 에 표시
CONFIDENCE_WARN = 0.8

# JSON 키 경로를 직접 지정하고 싶을 때 사용 (자동 인식보다 우선).
# 경로는 parse_log 의 json_keys 컬럼에 나오는 표기를 그대로 사용하세요.  예)
# JSON_PATH_OVERRIDES = {
#     "map_usual_x": "report.map.usual.x",
#     "comp_social_energy_usual": "components.Social Energy.usual",
# }
JSON_PATH_OVERRIDES = {}

# =============================================================================
# 2. 버크만 항목 정의 (별칭 = 자동 인식에 쓰이는 단어. 소문자/공백제거 기준)
# =============================================================================
MAP_LAYERS = {
    "interest": ["interest", "interests", "흥미", "관심"],
    "usual": ["usual", "usualbehavior", "평소", "평상시", "일상", "평소행동"],
    "needs": ["need", "needs", "욕구", "니즈"],
    "stress": ["stress", "스트레스"],
}
AXIS_X = ["x", "horizontal", "가로", "수평", "xaxis", "col", "column", "peopletask"]
AXIS_Y = ["y", "vertical", "세로", "수직", "yaxis", "row", "directindirect"]

COLORS = {
    "red": ["red", "레드", "빨강", "빨간", "적색"],
    "green": ["green", "그린", "초록", "녹색"],
    "yellow": ["yellow", "옐로", "옐로우", "노랑", "노란", "황색"],
    "blue": ["blue", "블루", "파랑", "파란", "청색"],
}

# 버크만 11개 구성요소 (신/구 명칭 + 한글 명칭)
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
COMP_USUAL = ["usual", "평소", "평상시", "일상"]
COMP_NEEDS = ["need", "needs", "욕구", "니즈"]

ORG_FOCUS_MARK = ["organizationalfocus", "organizational", "orgfocus", "orgorientation", "orientation",
                  "organization", "조직지향", "조직"]
ORG_FOCUS = {
    "red": ["operation", "technical", "technology", "implement", "실행", "운영", "기술"] + COLORS["red"],
    "green": ["sales", "marketing", "communicat", "영업", "마케팅", "소통"] + COLORS["green"],
    "yellow": ["admin", "fiscal", "finance", "관리", "재무", "행정"] + COLORS["yellow"],
    "blue": ["design", "strateg", "전략", "기획", "설계"] + COLORS["blue"],
}

# 직업 흥미 10개 영역 (부수 정보)
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

NAME_KEYS = ["name", "fullname", "이름", "성명", "participantname", "username"]
EMP_ID_KEYS = ["empid", "employeeid", "employeeno", "empno", "사번", "직원번호", "사원번호"]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def output_columns():
    cols = ["emp_id", "name", "email", "report_type", "org_path", "source_file"]
    for layer in MAP_LAYERS:
        cols += ["map_%s_x" % layer, "map_%s_y" % layer, "map_%s_color" % layer, "map_%s_bullets" % layer]
    for c in COLORS:
        cols.append("orgfocus_%s" % c)
    for key, _ in COMPONENTS:
        cols += ["comp_%s_usual" % key, "comp_%s_needs" % key]
    for key, _ in JOB_INTERESTS:
        cols.append("jobint_%s" % key)
    cols += ["map_confidence", "orgfocus_confidence", "parse_status", "parse_note"]
    return cols


# =============================================================================
# 3. 공통 유틸
# =============================================================================
def norm(s):
    return re.sub(r"[\s_\-\.\(\)/:]+", "", str(s).lower())


def to_number(v):
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
    if not isinstance(v, str):
        return None
    n = norm(v)
    for c, aliases in COLORS.items():
        if n in [norm(a) for a in aliases]:
            return c
    return None


def token_hit(tokens, joined, alias):
    """짧은 별칭(x, y 등)은 토큰 완전일치, 긴 별칭은 부분일치"""
    a = norm(alias)
    if len(a) <= 2:
        return a in tokens
    return a in joined


def any_hit(tokens, joined, aliases):
    return any(token_hit(tokens, joined, a) for a in aliases)


# =============================================================================
# 4. JSON 파싱
# =============================================================================
LIST_LABEL_KEYS = ["name", "title", "label", "key", "type", "category", "component", "area",
                   "symbol", "layer", "id", "이름", "항목", "구분", "영역", "명칭"]


def flatten_json(obj, path=None, out=None):
    """(경로 토큰 리스트, 값) 목록으로 평탄화. 리스트 안의 dict 는 name/label 값을 경로명으로 사용"""
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
    return ".".join(path)


def find_value(flat, must_groups, exclude=None, want="number", prefer=None):
    """must_groups 의 각 그룹에서 하나 이상 별칭이 경로에 있는 값을 찾음"""
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


def extract_bullets(text, limit=12):
    """레포트 문장에서 '• 항목' 들만 뽑아 중복 제거 (줄바꿈으로 끊긴 항목은 이어붙임)"""
    body = re.split(r"\n\s*외향\s*\n|--- page", text)[0]
    items = []
    for chunk in body.split("•")[1:]:
        lines = [l.strip() for l in chunk.strip().split("\n")]
        item = lines[0]
        for l in lines[1:]:
            # 다음 줄이 짧은 꼬리(예: "함")면 이어붙임, 아니면 설명문 시작으로 보고 중단
            if l and len(l) <= 8:
                # 한 글자(예: "원\n함")는 단어가 잘린 것 -> 붙이고, 그 외(예: "보내도록\n해줄 때")는 띄어서 연결
                item += l if len(l) == 1 else " " + l
            else:
                break
        item = re.sub(r"\s+", " ", item).strip(" .")
        if item and item not in items and len(item) <= 60:
            items.append(item)
    return items[:limit]


def lookup_path(flat, dotted):
    target = norm(dotted)
    for path, val in flat:
        if norm(path_str(path)) == target:
            return val
    return None


def parse_json_record(data):
    flat = flatten_json(data)
    rec, used = {}, {}

    # --- 버크만 맵 (좌표 / 색상)
    other_layers = {l: a for l, a in MAP_LAYERS.items()}
    for layer, aliases in MAP_LAYERS.items():
        excl = []
        for ol, oa in other_layers.items():
            if ol != layer:
                excl += oa
        # job interest 영역명과 겹치지 않도록 x/y 축 토큰을 반드시 요구
        for axis, axis_alias in (("x", AXIS_X), ("y", AXIS_Y)):
            v, p = find_value(flat, [aliases, axis_alias], exclude=excl, prefer=["map", "맵", "grid"])
            if v is not None:
                rec["map_%s_%s" % (layer, axis)] = v
                used["map_%s_%s" % (layer, axis)] = p
        c, p = find_value(flat, [aliases], exclude=excl, want="color", prefer=["map", "맵", "color", "색"])
        if c:
            rec["map_%s_color" % layer] = c
            used["map_%s_color" % layer] = p

    # --- 조직지향점
    for color, aliases in ORG_FOCUS.items():
        v, p = find_value(flat, [ORG_FOCUS_MARK, aliases])
        if v is not None:
            rec["orgfocus_%s" % color] = v
            used["orgfocus_%s" % color] = p

    # --- 11개 구성요소 (평소 / 욕구)
    for key, aliases in COMPONENTS:
        v, p = find_value(flat, [aliases, COMP_USUAL], exclude=COMP_NEEDS + ["stress", "스트레스"])
        if v is not None:
            rec["comp_%s_usual" % key] = v
            used["comp_%s_usual" % key] = p
        v, p = find_value(flat, [aliases, COMP_NEEDS], exclude=COMP_USUAL)
        if v is not None:
            rec["comp_%s_needs" % key] = v
            used["comp_%s_needs" % key] = p

    # --- 직업 흥미
    for key, aliases in JOB_INTERESTS:
        v, p = find_value(flat, [aliases], exclude=AXIS_X + AXIS_Y)
        if v is not None:
            rec["jobint_%s" % key] = v
            used["jobint_%s" % key] = p

    # --- 사용자 지정 경로 우선
    for field, dotted in JSON_PATH_OVERRIDES.items():
        val = lookup_path(flat, dotted)
        if val is not None:
            rec[field] = to_color(val) if field.endswith("_color") else to_number(val)
            used[field] = dotted

    # --- 인적사항 (얕은 경로 우선: 리스트 항목의 "name" 이 사람 이름으로 잡히지 않도록)
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

    # --- 레포트 원문 문장(• 항목) : 개인별 화면에서 "레포트 원문" 으로 보여줌
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

    # --- 자동 인식 신뢰도
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

    # --- 조직 경로 (예: ["경영지원센터", "인사팀"])
    if isinstance(data, dict):
        for k, v in data.items():
            if norm(k) in ("orgpath", "조직경로", "소속") and isinstance(v, list):
                rec["org_path"] = " > ".join(str(x) for x in v)

    # --- 레포트 종류: report_type 같은 명시적 키 우선, 없으면 전체 텍스트에서 추정
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

    keys = sorted(set(path_str([re.sub(r"^\d+$", "#", p) for p in path]) for path, _ in flat))
    return rec, used, keys


# =============================================================================
# 5. PDF 파싱 (텍스트 기반, 최선노력)
#    * PDF 안의 버크만 맵은 그림이라 좌표를 읽기 어렵습니다 -> 색상만 추정, 좌표는 수기 보정 권장
# =============================================================================
def pdf_to_text(raw):
    errors = []
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:  # noqa
        errors.append("pdfplumber: %s" % e)
    for mod in ("pypdf", "PyPDF2"):
        try:
            lib = __import__(mod)
            reader = lib.PdfReader(io.BytesIO(raw))
            return "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as e:  # noqa
            errors.append("%s: %s" % (mod, e))
    raise RuntimeError("PDF 텍스트 추출 실패 (코드 환경에 pdfplumber 또는 pypdf 설치 필요) / " + " | ".join(errors))


def _alias_regex(aliases):
    parts = []
    for a in aliases:
        if len(a) <= 2 and re.match(r"^[a-z]+$", a):
            continue
        # 공백/줄바꿈이 섞여도 매칭되도록 글자 사이에 \s* 허용
        parts.append(r"\s*".join(re.escape(ch) for ch in a))
    return "(?:%s)" % "|".join(parts)


def parse_pdf_text(text):
    rec = {}
    t = text.replace(" ", " ")
    low = t.lower()
    if "signature" in low or "시그니처" in low:
        rec["report_type"] = "signature"
    elif "basic" in low or "베이직" in low:
        rec["report_type"] = "basic"
    m = EMAIL_RE.search(t)
    if m:
        rec["email"] = m.group(0).lower()

    # 구성요소: "사회적 에너지 ... 45 ... 82" 형태 (평소, 욕구 순서 가정)
    for key, aliases in COMPONENTS:
        rx = re.compile(_alias_regex(aliases) + r"[^\d\n]{0,40}?(\d{1,2})[^\d\n]{1,20}?(\d{1,2})(?!\d)", re.I)
        mm = rx.search(t)
        if mm:
            rec["comp_%s_usual" % key] = float(mm.group(1))
            rec["comp_%s_needs" % key] = float(mm.group(2))

    # 조직지향점: 키워드 다음 숫자
    of_block = t
    mark = re.search(_alias_regex(ORG_FOCUS_MARK), t, re.I)
    if mark:
        of_block = t[mark.start(): mark.start() + 1500]
    for color, aliases in ORG_FOCUS.items():
        rx = re.compile(_alias_regex([a for a in aliases if a not in COLORS[color]]) + r"[^\d\n]{0,30}?(\d{1,3})(?!\d)", re.I)
        mm = rx.search(of_block) if mark else None
        if mm:
            rec["orgfocus_%s" % color] = float(mm.group(1))

    # 직업 흥미
    for key, aliases in JOB_INTERESTS:
        rx = re.compile(_alias_regex(aliases) + r"[^\d\n]{0,20}?(\d{1,2})(?!\d)", re.I)
        mm = rx.search(t)
        if mm:
            rec["jobint_%s" % key] = float(mm.group(1))

    # 맵 색상: "평소 행동 ... 그린" 같은 문장
    for layer, aliases in MAP_LAYERS.items():
        rx = re.compile(_alias_regex(aliases) + r"[^\n]{0,40}?" + "(" + "|".join(
            re.escape(a) for c in COLORS for a in COLORS[c]) + ")", re.I)
        mm = rx.search(t)
        if mm:
            rec["map_%s_color" % layer] = to_color(mm.group(1))
    return rec


# =============================================================================
# 6. 후처리
# =============================================================================
def apply_filename_info(rec, filename):
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


def finalize(df):
    for c in output_columns():
        if c not in df.columns:
            df[c] = None
    df = df[output_columns()].copy()

    # 맵 좌표 스케일 정규화 -> 0~100
    xy = [c for c in df.columns if c.startswith("map_") and (c.endswith("_x") or c.endswith("_y"))]
    for c in xy:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if MAP_SCALE == "auto":
        # 파일마다 형식이 다를 수 있어 사람(행) 단위로 판단
        #  - 음수가 있으면 '중앙=0' 좌표 -> 50 + v / 반경 * 50
        #  - 모두 0~1 이면 x100
        centered = (df[xy] < 0).any(axis=1)
        if centered.any():
            if MAP_CENTER_HALF_RANGE == "auto":
                half = max(4.0, math.ceil(df.loc[centered, xy].abs().max().max()))
            else:
                half = float(MAP_CENTER_HALF_RANGE)
            print("[INFO] 중앙 원점 좌표 %d명 -> 반경 %.1f 기준으로 0~100 변환" % (centered.sum(), half))
            df.loc[centered, xy] = 50 + df.loc[centered, xy] / half * 50
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

    num_cols = [c for c in df.columns if c.startswith(("orgfocus_", "comp_", "jobint_"))]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df[xy + num_cols] = df[xy + num_cols].clip(lower=0, upper=100)

    # 좌표가 있고 색상이 없으면 좌표로 사분면 색 계산
    for layer in MAP_LAYERS:
        x, y, col = "map_%s_x" % layer, "map_%s_y" % layer, "map_%s_color" % layer
        mask = df[col].isna() & df[x].notna() & df[y].notna()
        df.loc[mask, col] = [quadrant(a, b) for a, b in zip(df.loc[mask, x], df.loc[mask, y])]

    # 레포트 종류 미표기 -> 구성요소 점수가 있으면 signature 로 추정
    comp_cols = [c for c in df.columns if c.startswith("comp_")]
    has_comp = df[comp_cols].notna().sum(axis=1) >= 5
    df.loc[df["report_type"].isna() & has_comp, "report_type"] = "signature"
    df.loc[df["report_type"].isna(), "report_type"] = "basic"

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
    df["parse_note"] = [(" / ".join(x for x in (str(a) if pd.notna(a) else "", b) if x)) for a, b in zip(df["parse_note"], low)]

    for c in ["emp_id", "name", "email", "report_type", "org_path", "source_file", "parse_status", "parse_note"] + \
            ["map_%s_bullets" % l for l in MAP_LAYERS]:
        df[c] = df[c].astype(object).where(df[c].notna(), None)
        df[c] = df[c].map(lambda v: None if v is None else str(v))
    return df


def quadrant(x, y):
    """버크만 맵: 왼쪽=과업지향, 오른쪽=사람지향 / 위=직접적·외향, 아래=간접적·내향"""
    if y >= 50:
        return "red" if x < 50 else "green"
    return "yellow" if x < 50 else "blue"


def missing_summary(rec):
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
    """수기 보정 데이터: emp_id 기준으로 값이 있는 칸만 덮어쓰기, 없는 사람은 추가"""
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
        vals = {c: mrow[c] for c in manual.columns if c in df.columns and pd.notna(mrow[c]) and str(mrow[c]).strip() != ""}
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
# 7. 입출력 (Dataiku / 로컬)
# =============================================================================
class DataikuIO(object):
    def __init__(self):
        import dataiku
        self.dataiku = dataiku
        self.folder = dataiku.Folder(INPUT_FOLDER_NAME)

    def list_files(self):
        return sorted(self.folder.list_paths_in_partition())

    def read(self, path):
        with self.folder.get_download_stream(path) as f:
            return f.read()

    def read_manual(self):
        try:
            return self.dataiku.Dataset(MANUAL_DATASET_NAME).get_dataframe(infer_with_pandas=False)
        except Exception as e:  # noqa
            print("[INFO] 수기 보정 데이터셋 없음/읽기 실패 -> 건너뜀:", e)
            return None

    def write(self, name, df):
        self.dataiku.Dataset(name).write_with_schema(df)


class LocalIO(object):
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
    for enc in ("utf-8-sig", "utf-8", "cp949", "utf-16"):
        try:
            return json.loads(raw.decode(enc))
        except Exception:  # noqa
            continue
    raise ValueError("JSON 디코딩 실패")


def run(io_):
    records, logs = [], []
    for path in io_.list_files():
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".json", ".pdf"):
            continue
        log = {"source_file": path, "file_type": ext[1:], "status": "OK", "missing": "", "message": "",
               "matched_paths": "", "json_keys": ""}
        try:
            raw = io_.read(path)
            if ext == ".json":
                data = decode_json_bytes(raw)
                # 한 파일에 여러 명이 배열로 들어있는 경우도 처리
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
        except Exception as e:  # noqa
            log["status"] = "ERROR"
            log["message"] = str(e)[:500]
        logs.append(log)

    df = pd.DataFrame(records) if records else pd.DataFrame(columns=output_columns())
    df = merge_manual(df, io_.read_manual())
    df = finalize(df)

    # 같은 사람이 여러 파일이면(예: 베이직 후 시그니처 재진단) 채워진 값이 많은 행 우선
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
