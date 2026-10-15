# -*- coding: utf-8 -*-
"""
동우화인켐 버크만 팀 리포트 — Dataiku PDF 추출 / 명부 연결 레시피

사용법: 00_Dataiku_실행안내.txt의 입출력을 먼저 만든 뒤 이 파일 전체를
Python 레시피에 붙여 넣습니다. 파일 경로나 직원 정보를 코드에 넣지 않습니다.

입력: 관리 폴더 XHJjWXOC, 데이터셋 employee_staff_master
출력: 데이터셋 birkman_people, birkman_pdf_files, birkman_issues
      관리 폴더 birkman_cache

이 코드는 원본 PDF/명부를 수정하거나 외부 AI/API로 전송하지 않습니다.
공통 맵 4종 / 흥미 10종 / 강점 설명 / 조직지향점의 도형 정보를 추출합니다.
JSON 예시 파일이나 시그니처 전용 9개 관계요소 점수는 사용하지 않습니다.
"""

# =====================================================================
# 1. 설정: 아래 이름대로 만들었다면 수정할 곳이 없습니다.
# =====================================================================
PDF_FOLDER_ID = "XHJjWXOC"
MASTER_DATASET = "employee_staff_master"
PEOPLE_OUTPUT = "birkman_people"
FILES_OUTPUT = "birkman_pdf_files"
ISSUES_OUTPUT = "birkman_issues"
CACHE_FOLDER = "birkman_cache"  # 관리 폴더의 이름 또는 ID. PDF 폴더와 분리합니다.
FORCE_REEXTRACT = False         # 추출 규칙 변경/전체 재처리가 필요할 때만 True
ALLOWED_EMAIL_DOMAIN = "dwchem.co.kr"
ALLOWED_JOB_TYPES = {"사무직", "기술직", "연구직"}
MAX_PDF_MB = 40                 # 전체 용량이 아닌 '한 PDF'의 비정상 크기 방어값
PROGRESS_EVERY = 25
AUTO_INSTALL_PYMUPDF = True     # PyMuPDF가 없으면 현재 Python 환경에 자동 설치
PYMUPDF_INSTALL_VERSION = "1.26.6"
PACKAGE_INSTALL_TIMEOUT = 300  # 다운로드/설치 전체 제한 시간(초)

# 같은 사번의 서로 다른 PDF가 여러 개일 때만 사용할 파일의 '폴더 내 경로'를
# 명시합니다. 기본은 임의 선택하지 않습니다. 경로는 birkman_pdf_files에서 복사.
# 예: PDF_SELECTION = {"000000": "/3그룹/000000_예시.pdf"}
PDF_SELECTION = {}

# 판독 방식이 달라지면 버전을 변경하여 과거 추출 캐시와 자동으로 구분합니다.
PARSER_VERSION = "2026-10-15.1"   # 기호·조직지향점 판독 보강. 바뀌면 모든 PDF를 한 번 다시 읽습니다.

# =====================================================================
# 2. 도구와 자동 설치: 이미 설치된 PyMuPDF는 재설치하지 않습니다.
#    설치 이름은 PyMuPDF이며 'fitz'라는 별도 패키지는 설치하지 않습니다.
# =====================================================================
import collections
import datetime as dt
import hashlib
import importlib
import importlib.metadata
import json
import math
import re
import subprocess
import sys
import unicodedata


def ensure_pymupdf():
    """필요할 때만 현재 인터프리터의 pip로 설치하고 바로 불러옵니다.

    회사의 기존 pip 저장소/프록시 설정을 따릅니다. 다른 저장소를 강제로
    지정하거나 권한·인증서 제한을 우회하는 설치 옵션은 사용하지 않습니다.
    설치가 실패하면 PDF를 읽기 전에 멈춥니다.
    """
    try:
        installed_version = importlib.metadata.version("PyMuPDF")
    except importlib.metadata.PackageNotFoundError:
        installed_version = None

    if installed_version is None:
        if not AUTO_INSTALL_PYMUPDF:
            raise RuntimeError("PyMuPDF가 없습니다. AUTO_INSTALL_PYMUPDF를 True로 두거나 코드 환경에 설치하세요.")
        if sys.version_info < (3, 10):
            raise RuntimeError("자동 설치할 PyMuPDF 1.26.6은 Python 3.10 이상이 필요합니다. 레시피의 코드 환경을 변경하세요.")
        print("PyMuPDF가 없어 현재 Python 환경에 설치합니다. 설치가 끝나면 PDF 처리를 시작합니다.", flush=True)
        command = [sys.executable, "-m", "pip", "install", "--no-user",
                   "--no-input", "--disable-pip-version-check", "--only-binary=:all:",
                   "--retries", "1", "--timeout", "20",
                   "PyMuPDF=="+PYMUPDF_INSTALL_VERSION]
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    encoding="utf-8", errors="replace", timeout=PACKAGE_INSTALL_TIMEOUT,
                                    check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError("PyMuPDF 설치가 제한 시간을 넘었습니다. 서버의 패키지 저장소 연결 상태를 확인하세요.") from None
        if result.returncode:
            detail = (result.stdout or "").lower()
            if "permission denied" in detail or "not writeable" in detail or "read-only" in detail:
                reason = "현재 Python 환경에 설치할 쓰기 권한이 없습니다."
            elif "externally-managed-environment" in detail:
                reason = "이 Python 환경은 관리 도구를 통해서만 패키지를 설치하도록 설정돼 있습니다."
            elif "no module named pip" in detail:
                reason = "현재 Python 환경에 pip가 없습니다."
            elif any(s in detail for s in ("ssl", "certificate", "proxy", "connection", "timed out")):
                reason = "패키지 저장소 연결·프록시·인증서 설정을 확인해야 합니다."
            elif "no matching distribution" in detail or "could not find a version" in detail:
                reason = "설정된 저장소에서 현재 서버에 맞는 PyMuPDF 1.26.6 설치 파일을 찾지 못했습니다."
            else:
                reason = "현재 서버 환경에서 pip 설치가 완료되지 않았습니다."
            # 저장소 주소/인증정보가 포함될 수 있는 pip 원문 로그를 재출력하지 않습니다.
            raise RuntimeError("PyMuPDF 자동 설치 실패(종료 코드 %s). %s 선배님 또는 관리자에게 코드 환경 설치를 요청하세요."
                               % (result.returncode, reason))
        importlib.invalidate_caches()
        installed_version = PYMUPDF_INSTALL_VERSION
        print("PyMuPDF 설치 완료", flush=True)

    try:
        # 최신 PyMuPDF의 고유 모듈명을 우선 사용해 동명의 다른 fitz와 충돌을 줄입니다.
        try:
            module = importlib.import_module("pymupdf")
        except ModuleNotFoundError as exc:
            if exc.name != "pymupdf":
                raise
            module = importlib.import_module("fitz")  # 기존 구버전의 호환 이름
        if not all(hasattr(module, name) for name in ("open", "Rect", "Page", "VersionBind")):
            raise ImportError("PyMuPDF 모듈 확인 실패")
        if not hasattr(module.Page, "get_drawings"):
            raise ImportError("도형 추출 API가 없는 버전")
    except Exception as exc:
        raise RuntimeError("PyMuPDF는 설치돼 있지만 불러오지 못했습니다(%s). 같은 코드 환경에 PyMuPDF 1.26.6을 설치한 뒤 레시피를 다시 실행하세요."
                           % type(exc).__name__) from None
    print("사용하는 PyMuPDF 버전: "+str(installed_version), flush=True)
    return module


fitz = ensure_pymupdf()

MASTER_COLUMNS = [
    "No", "사번", "성명", "재직상태", "조직명", "본부명", "그룹총괄/공장",
    "그룹명", "팀명", "파트명", "직종명", "직책명", "직위명", "사업장",
    "실근무지", "이메일주소", "출향여부", "출향지역", "근무조", "국적",
]
MAP_KINDS = {"interest": "흥미", "usual": "평소행동", "needs": "욕구",
             "stress": "스트레스행동"}
INTEREST_NAMES = {
    "숫자": "numerical", "관리": "administrative", "과학": "scientific",
    "문학": "literary", "기술": "technical", "음악": "musical",
    "예술": "artistic", "야외": "outdoor", "설득": "persuasive",
    "사회복지": "social_service",
}
COLORS = ("빨강", "초록", "노랑", "파랑")
SOURCE_TIMEZONE = dt.timezone(dt.timedelta(hours=9))


def text(value):
    """빈값과 유니코드를 정리합니다. 사번의 앞자리 0은 삭제하지 않습니다."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def compact(value):
    return re.sub(r"\s+", "", text(value))


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def now():
    return dt.datetime.now(SOURCE_TIMEZONE).isoformat(timespec="seconds")


def issue(code, message, employee_id="", source_path="", level="warning", page=None):
    return {"level": level, "issue_code": code, "employee_id": employee_id,
            "source_path": source_path, "source_page": page, "message": message}


def lines_on_page(page):
    """행과 위치를 함께 읽어 두 단으로 나뉜 설명의 좌우가 섞이지 않게 합니다."""
    rows = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            t = text("".join(span["text"] for span in line["spans"]))
            if t:
                rows.append({"text": t, "rect": fitz.Rect(line["bbox"])})
    return rows


def color_of(rgb):
    """맵의 사분면 색 계열을 판별. 흰색·회색·검정은 제외합니다."""
    if rgb is None or len(rgb) != 3:
        return None
    r, g, b = rgb
    if max(rgb) < 0.25 or max(rgb) - min(rgb) < 0.15:
        return None
    if r > g and r > b:
        return "노랑" if (g-b) / max(r-b, 0.001) > 0.25 else "빨강"
    if g > r and g > b:
        return "초록"
    if b > r and b > g:
        return "파랑"
    return None


def map_bounds(page, drawings):
    """4개 사분면의 위치/접합부를 확인하여 맵 테두리를 찾습니다.

    고정 페이지 번호나 고정 픽셀을 사용하지 않습니다. 각 색의 가장 큰
    정사각형 영역을 찾고 빨강/초록/노랑/파랑의 배치가 맞는지 확인합니다.
    """
    groups = collections.defaultdict(list)
    for d in drawings:
        r = d["rect"]
        c = color_of(d.get("fill"))
        if c and 0.05 * page.rect.width < r.width < 0.49 * page.rect.width:
            if 0.96 < r.width / max(r.height, 0.001) < 1.04:
                groups[c].append(r)
    if not all(groups[c] for c in COLORS):
        return None
    q = {c: max(groups[c], key=lambda r: r.width * r.height) for c in COLORS}
    red, green, yellow, blue = (q[c] for c in COLORS)
    side = red.width
    tol = max(1.0, side * 0.025)
    expected = {
        "빨강": (red.x0, red.y0), "초록": (red.x0 + side, red.y0),
        "노랑": (red.x0, red.y0 + side), "파랑": (red.x0 + side, red.y0 + side),
    }
    for c, r in q.items():
        ex, ey = expected[c]
        if max(abs(r.x0-ex), abs(r.y0-ey), abs(r.width-side), abs(r.height-side)) > tol:
            return None
    box = fitz.Rect(red.x0, red.y0, blue.x1, blue.y1)
    # 설명이 있는 맵인지 확인. 색상 설명표를 맵으로 오인하지 않습니다.
    page_text = compact(page.get_text())
    if not all(w in page_text for w in ("외향", "내향", "과제지향", "사람지향")):
        return None
    return box


def symbol_kind(d):
    """별/다이아몬드/원/사각형을 도형의 선과 곡선으로 구분합니다.

    같은 모양도 PDF를 만든 프로그램에 따라 그리는 방식이 다릅니다.
    사각형: 're' 한 개, 'qu' 한 개, 또는 가로·세로 선 3~5개(닫힌 경로는 마지막 선이 생략될 수 있음)
    마름모: 기울어진 선 3~5개 / 원: 곡선 4개 이상 / 별: 선 8개 이상
    """
    items = d["items"]
    ops = [v[0] for v in items]
    if len(items) >= 8 and all(o == "l" for o in ops):
        return "interest"
    if len(items) >= 4 and all(o == "c" for o in ops):
        return "needs"
    if ops in (["re"], ["qu"]):
        return "stress"
    if 3 <= len(items) <= 5 and all(o == "l" for o in ops):
        lines = [v for v in items if abs(v[1].x-v[2].x) + abs(v[1].y-v[2].y) > 0.05]
        axis = [abs(v[1].x-v[2].x) < 0.1 or abs(v[1].y-v[2].y) < 0.1 for v in lines]
        if lines and all(axis):
            return "stress"
        if lines and not any(axis):
            return "usual"
    return None


def symbols_in_map(drawings, box):
    found = collections.defaultdict(list)
    for d in drawings:
        fill, r = d.get("fill"), d["rect"]
        if fill is None or len(fill) != 3 or min(fill) < 0.96:
            continue
        if not (0.012 < r.width / box.width < 0.10 and
                0.012 < r.height / box.height < 0.10 and
                0.65 < r.width / max(r.height, 0.001) < 1.45):
            continue
        cx, cy = (r.x0+r.x1)/2, (r.y0+r.y1)/2
        # 맵 끝에 있는 기호는 중심이 테두리에 걸리거나 살짝 넘을 수 있습니다(맵 크기의 3%까지 허용).
        mx, my = box.width*0.03, box.height*0.03
        if not (box.x0-mx <= cx <= box.x1+mx and box.y0-my <= cy <= box.y1+my):
            continue
        kind = symbol_kind(d)
        if kind:
            x = max(-1.0, min(1.0, 2 * (cx-box.x0)/box.width - 1))
            y = max(-1.0, min(1.0, 1 - 2 * (cy-box.y0)/box.height))
            record = {"x": round(x, 6), "y": round(y, 6),
                      "pdf_center": [round(cx, 3), round(cy, 3)],
                      "map_rect": [round(v, 3) for v in box]}
            # 같은 도형을 중복 그린 PDF만 병합. 서로 다른 위치는 그대로 남깁니다.
            if not any(math.hypot(x-v["x"], y-v["y"]) < 0.002 for v in found[kind]):
                found[kind].append(record)
    return found


def geometric_color(x, y):
    if abs(x) <= 0.002 or abs(y) <= 0.002:
        return "경계"
    return ("초록" if x > 0 else "빨강") if y > 0 else ("파랑" if x > 0 else "노랑")


def bullets(rows):
    """두 줄 이상의 글머리표를 한 문장으로 연결합니다."""
    result, current = [], []
    for row in sorted(rows, key=lambda z: (round(z["rect"].y0, 1), z["rect"].x0)):
        t = row["text"]
        if re.match(r"^[•●▪·]\s*", t):
            if current:
                result.append(" ".join(current))
            current = [re.sub(r"^[•●▪·]\s*", "", t)]
        elif current:
            # 개인 설명 중간에 있는 '다음과 같은 경향' 소제목은 제외합니다.
            if any(w in compact(t) for w in ("기호위치", "다음과", "경향도", "있습니다:")):
                result.append(" ".join(current)); current = []
            else:
                current.append(t)
    if current:
        result.append(" ".join(current))
    return result


def detail_text(page, rows, box, kind):
    label = MAP_KINDS[kind]
    heading = next((r for r in rows if compact(r["text"]).startswith(label + "탐색")), None)
    if not heading:
        return {"intro": "", "personal": [], "general": [], "reported_color": ""}
    upper = box.y0 - 0.02 * page.rect.height if box else page.rect.height * 0.50
    body = [r for r in rows if heading["rect"].y1 < r["rect"].y0 < upper]
    # 오른쪽 열의 소제목 시작점을 기준으로 위의 도입문 / 아래의 두 열을 구분합니다.
    right_titles = [r for r in body if r["rect"].x0 >= page.rect.width*0.46
                    and ("따르면" in r["text"] or "표시된" in r["text"])]
    if not right_titles:
        return {"intro": " ".join(r["text"] for r in body), "personal": [],
                "general": [], "reported_color": ""}
    split_y = min(r["rect"].y0 for r in right_titles)
    intro = " ".join(r["text"] for r in sorted(body, key=lambda z: z["rect"].y0)
                     if r["rect"].y0 < split_y-1)
    lower = [r for r in body if r["rect"].y0 >= split_y-1]
    general = bullets([r for r in lower if r["rect"].x0 < page.rect.width*0.46])
    personal = bullets([r for r in lower if r["rect"].x0 >= page.rect.width*0.46])
    match = re.search(r"기호는(빨강|초록|노랑|파랑)에", compact(intro))
    return {"intro": intro, "personal": personal, "general": general,
            "reported_color": match.group(1) if match else ""}


def extract_interests(page, rows):
    """점수를 정렬 순서가 아니라 같은 칸의 항목명에 연결합니다."""
    labels = [r for r in rows if compact(r["text"]) in INTEREST_NAMES]
    scores = [r for r in rows if re.fullmatch(r"\d{1,3}\s*%", r["text"])]
    result = {}
    if len(labels) != 10 or len(scores) != 10:
        return result
    assignments = []
    for label in labels:
        lr = label["rect"]
        # 이 양식의 점수 원은 항목명 왼쪽에 있습니다. 축척 변화도 허용합니다.
        matches = [s for s in scores
                   if 0 < lr.x0-(s["rect"].x0+s["rect"].x1)/2 < page.rect.width*0.20
                   and -page.rect.height*0.02 < (s["rect"].y0+s["rect"].y1)/2-lr.y0 < page.rect.height*0.11]
        if len(matches) != 1:
            return {}
        s = matches[0]
        score = int(re.sub(r"\D", "", s["text"]))
        if not 0 <= score <= 100:
            return {}
        assignments.append(id(s))
        result[compact(label["text"])] = score
    if len(set(assignments)) != 10 or len(result) != 10:
        return {}
    return result


def extract_strengths(page, rows):
    end = next((r for r in rows if "표시하시기바랍니다" in compact(r["text"])), None)
    if not end:
        return []
    selected = sorted([r for r in rows if end["rect"].y1 < r["rect"].y0 < page.rect.height*0.93],
                      key=lambda z: (z["rect"].y0, z["rect"].x0))
    result, current = [], []
    for r in selected:
        current.append(re.sub(r"^[○●•]\s*", "", r["text"]))
        if r["text"].endswith((".", "。")):
            result.append(" ".join(current)); current = []
    if current:
        result.append(" ".join(current))
    return result


ORIENTATION_LABEL = re.compile(r"^(빨강|초록|노랑|파랑)\s*[-–—－:·]?\s*(기획|영업|운영|관리)")


def extract_orientation(page, rows, drawings):
    """네 막대를 같은 표 안에서 읽습니다. 길이는 PDF 단위이며 공식 점수가 아닙니다.

    - 표의 네 이름표(빨강-운영/기술 등)는 색마다 가장 위에 있는 것을 씁니다.
      아래쪽 '색상별 조직지향점' 설명 칸의 같은 이름은 자연히 제외됩니다.
    - 막대는 이름표와 같은 줄, 이름표 오른쪽에 있는 같은 색 도형입니다.
      한 막대가 여러 조각으로 그려졌거나 중복 그려졌으면 합쳐서 한 막대로 봅니다.
    - 안 보이는 막대를 0으로 채우지 않습니다.
    """
    labels = {}
    for r in sorted(rows, key=lambda z: z["rect"].y0):
        m = ORIENTATION_LABEL.match(compact(r["text"]))
        if m and m.group(1) not in labels:
            labels[m.group(1)] = r
    if len(labels) != 4:
        return {"_reason": "이름표 %d개만 찾음(%s)" % (len(labels), ",".join(labels) or "없음")}
    bars = {}
    for color, label in labels.items():
        lr = label["rect"]
        cy = (lr.y0+lr.y1)/2
        ds = [d["rect"] for d in drawings if color_of(d.get("fill")) == color and
              d["rect"].x0 >= lr.x1 and
              abs((d["rect"].y0+d["rect"].y1)/2-cy) < page.rect.height*0.02 and
              d["rect"].width > 0 and 0 < d["rect"].height < page.rect.height*0.06]
        if not ds:
            return {"_reason": "%s 막대를 찾지 못함" % color}
        r = fitz.Rect(ds[0])
        for extra in ds[1:]:
            r |= extra
        bars[color] = {"label": label["text"], "pdf_rect": [round(v, 3) for v in r], "pieces": len(ds)}
    rects = [fitz.Rect(v["pdf_rect"]) for v in bars.values()]
    centers = [(r.x0+r.x1)/2 for r in rects]
    heights = [r.height for r in rects]
    if max(centers)-min(centers) > page.rect.width*0.01:
        return {"_reason": "막대 가운데 위치가 서로 다름"}
    if max(heights)/min(heights) > 1.3:
        return {"_reason": "막대 높이가 서로 다름"}
    return {"method": "vector_geometry_only", "bars": bars,
            "note": "원본 막대 도형. 공식 점수가 아님. 개인 내 길이 비교와 최상위 분야 인원 집계에 사용."}


def extract_pdf(pdf_bytes):
    """파일 이름/조직과 무관한 순수 PDF 추출. 이 결과만 해시 캐시에 저장합니다."""
    out = {"parser_version": PARSER_VERSION, "report_type": "unknown", "report_name": "",
           "print_date": "", "page_count": 0, "maps": {}, "interests": {},
           "strengths": [], "orientation": {}, "issues": [], "extraction_status": "failed"}
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        if doc.needs_pass:
            out["issues"].append(issue("PDF_ENCRYPTED", "암호가 걸린 PDF입니다.")); return out
        out["page_count"] = len(doc)
        # PDF 회전 표시는 메모리에서만 해제합니다. 원본 파일은 변경하지 않습니다.
        for page in doc:
            if page.rotation:
                page.set_rotation(0)
        texts = [page.get_text() for page in doc]
        if sum(len(compact(t)) for t in texts) < 100:
            out["issues"].append(issue("PDF_IMAGE_ONLY", "텍스트를 읽을 수 없는 이미지 PDF입니다. 텍스트 원본이 필요합니다.")); return out
        combined = "\n".join(texts)
        types = set(re.findall(r"버크만\s*(시그니처|베이직)\s*리포트\s*[|｜]", combined))
        if len(types) == 1:
            out["report_type"] = "signature" if "시그니처" in types else "basic"
        else:
            out["issues"].append(issue("REPORT_TYPE_UNKNOWN", "베이직/시그니처 종류를 확인하세요."))
        m = re.search(r"작성되었습니다\s*:\s*([^\n]+)", texts[0])
        if m:
            out["report_name"] = re.sub(r"\s*\([A-Za-z0-9]+\)\s*$", "", m.group(1)).strip()
        m = re.search(r"인쇄일\s*[|｜]?\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", texts[0])
        if m:
            out["print_date"] = "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
        map_candidates = collections.defaultdict(list)
        overview_candidates = collections.defaultdict(list)
        interest_candidates = []
        orientation_pages, orientation_reasons = [], []
        for index, page in enumerate(doc):
            t, number = compact(texts[index]), index+1
            detail = next((k for k, label in MAP_KINDS.items() if label+"탐색(" in t), None)
            need_map = detail or ("버크만맵" in t and all(w in t for w in ("과제지향", "사람지향")))
            need_interests = "버크만흥미" in t and len(re.findall(r"\d{1,3}%", t)) >= 10
            need_strengths = "버크만인사이트" in t and "강점" in t
            need_orientation = "조직지향점" in t and ("유사성" in t or "유사정도" in t)
            if not any((need_map, need_interests, need_strengths, need_orientation)):
                continue
            rows = lines_on_page(page)
            drawings = page.get_drawings() if need_map or need_orientation else []
            if need_map:
                box = map_bounds(page, drawings)
                symbols = symbols_in_map(drawings, box) if box else {}
                if detail and not box:
                    out["issues"].append(issue("MAP_BOX_NOT_FOUND", MAP_KINDS[detail]+" 상세 페이지에서 맵 테두리(네 색 사분면)를 찾지 못했습니다.", page=number))
                if detail:
                    desc = detail_text(page, rows, box, detail)
                    candidates = symbols.get(detail, [])
                    # 거의 같은 자리(맵의 1% 이내)에 두 번 그린 기호는 하나로 봅니다.
                    if len(candidates) > 1 and all(math.hypot(c["x"]-candidates[0]["x"], c["y"]-candidates[0]["y"]) < 0.02 for c in candidates):
                        candidates = candidates[:1]
                    if len(candidates) == 1:
                        record = dict(candidates[0], page=number, description=desc,
                                      method="detail_page_vector")
                        map_candidates[detail].append(record)
                    else:
                        out["issues"].append(issue("MAP_SYMBOL_NOT_UNIQUE", MAP_KINDS[detail]+" 기호를 하나로 확정하지 못했습니다(찾은 기호 %d개)." % len(candidates), page=number))
                else:
                    for k, candidates in symbols.items():
                        if len(candidates) == 1:
                            overview_candidates[k].append(dict(candidates[0], page=number))
            if need_interests:
                values = extract_interests(page, rows)
                if len(values) == 10:
                    interest_candidates.append({"page": number, "values": values})
            if need_strengths:
                strengths = extract_strengths(page, rows)
                if strengths and not out["strengths"]:
                    out["strengths"], out["strengths_page"] = strengths, number
            if need_orientation:
                orientation_pages.append(number)
                orientation = extract_orientation(page, rows, drawings)
                if orientation and "_reason" not in orientation:
                    out["orientation"] = dict(orientation, page=number)
                elif orientation:
                    orientation_reasons.append("%d쪽: %s" % (number, orientation["_reason"]))
        for k in MAP_KINDS:
            choices = map_candidates[k]
            summaries = overview_candidates[k]
            if len(choices) > 1 and all(math.hypot(c["x"]-choices[0]["x"], c["y"]-choices[0]["y"]) < 0.02 for c in choices):
                choices = choices[:1]
            if len(choices) != 1:
                # 상세 맵을 못 읽었어도 전체 맵에서 한 위치로 확정되면 그 좌표를 씁니다(설명 원문은 없음).
                spots = {(round(s["x"], 2), round(s["y"], 2)) for s in summaries}
                if not choices and summaries and len(spots) == 1:
                    chosen = dict(summaries[0], method="overview_page_vector", overview_check="overview_only",
                                  description={"intro": "", "personal": [], "general": [], "reported_color": ""})
                    out["issues"].append(issue("MAP_OVERVIEW_ONLY", MAP_KINDS[k]+" 상세 맵 대신 전체 맵의 위치를 사용했습니다.", level="info", page=chosen["page"]))
                else:
                    out["issues"].append(issue("MAP_MISSING", MAP_KINDS[k]+" 상세 맵을 확정하지 못했습니다.")); continue
            else:
                chosen = choices[0]
            q = geometric_color(chosen["x"], chosen["y"])
            reported = chosen["description"]["reported_color"]
            if reported and q != "경계" and q != reported:
                near_axis = abs(chosen["x"]) < 0.10 or abs(chosen["y"]) < 0.10
                if near_axis:
                    # 가운데 선 가까이에서는 기호 그림의 중심과 원문 색이 엇갈릴 수 있습니다. 좌표는 두고 색은 원문을 따릅니다.
                    out["issues"].append(issue("MAP_COLOR_NEAR_AXIS", MAP_KINDS[k]+" 가운데 선 근처라 색은 원문 설명(%s)을 따랐습니다." % reported, level="info", page=chosen["page"]))
                    q = reported
                else:
                    out["issues"].append(issue("MAP_COLOR_CONFLICT", MAP_KINDS[k]+" 좌표와 원문 색상 설명이 다릅니다.", page=chosen["page"])); continue
            if chosen.get("overview_check") != "overview_only":
                if summaries:
                    far = max(math.hypot(chosen["x"]-s["x"], chosen["y"]-s["y"]) for s in summaries)
                    chosen["overview_check"] = "matched" if far <= 0.05 else "differs"
                    chosen["overview_pages"] = [s["page"] for s in summaries]
                    if far > 0.05:
                        # 전체 맵은 겹치는 기호를 조금 비켜 그리기도 합니다. 상세 맵 좌표를 그대로 씁니다.
                        out["issues"].append(issue("MAP_OVERVIEW_DIFFERS", MAP_KINDS[k]+" 전체 맵 위치와 차이가 있어 상세 맵 좌표를 사용했습니다.", level="info", page=chosen["page"]))
                else:
                    chosen["overview_check"] = "unavailable"
            chosen["quadrant"] = q
            out["maps"][k] = chosen
        if len(interest_candidates) == 1:
            out["interests"] = interest_candidates[0]["values"]
            out["interests_page"] = interest_candidates[0]["page"]
        else:
            out["issues"].append(issue("INTERESTS_INCOMPLETE", "흥미 10개 항목과 점수의 연결을 확정하지 못했습니다."))
        if not out["strengths"]:
            out["issues"].append(issue("STRENGTHS_MISSING", "강점 설명을 추출하지 못했습니다."))
        if not out["orientation"]:
            reason = ("조직지향점 페이지는 찾았지만 네 막대를 확정하지 못했습니다. " + " / ".join(orientation_reasons) if orientation_pages
                      else "조직지향점 페이지의 제목/표 문구를 찾지 못했습니다.")
            out["issues"].append(issue("ORIENTATION_UNAVAILABLE", reason, level="info",
                                       page=orientation_pages[0] if orientation_pages else None))
        n_maps, n_interests = len(out["maps"]), len(out["interests"])
        if n_maps == 4 and n_interests == 10 and out["strengths"]:
            out["extraction_status"] = "complete"
        elif n_maps or n_interests or out["orientation"]:
            out["extraction_status"] = "partial"
    return out


def file_identity(path):
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    m = re.fullmatch(r"([A-Za-z0-9-]+)_(.+)\.pdf", text(base), flags=re.I)
    if not m:
        return "", ""
    return m.group(1), text(m.group(2))


def org_key(level, values):
    # 열 이름까지 포함하므로 빈 중간 조직이 있거나 이름에 구분자가 있어도 안전합니다.
    return level+"_"+hashlib.sha256(dumps(values).encode("utf-8")).hexdigest()[:24]


def prepare_roster(records, schema):
    """직원당 한 행을 유지합니다. 중복 사번은 임의로 합치지 않고 연결을 보류합니다."""
    canonical = {compact(c): c for c in MASTER_COLUMNS}
    mapping = {s["name"]: canonical.get(compact(s["name"]), s["name"]) for s in schema}
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("명부 열 이름을 정리한 뒤 중복되는 열이 있습니다.")
    missing = set(MASTER_COLUMNS[1:]) - set(mapping.values())
    if missing:
        raise ValueError("명부에 필요한 열이 없습니다: "+", ".join(sorted(missing)))
    id_field = next(s for s in schema if mapping[s["name"]] == "사번")
    if id_field["type"] != "string":
        raise ValueError("명부의 사번 스키마를 string으로 변경하세요. 이미 사라진 앞자리 0은 원본 엑셀에서 복원해야 합니다.")
    rows, issues = [], []
    for index, raw in enumerate(records, 1):
        normalized = {mapping.get(k, k): text(v) for k, v in raw.items()}
        if not any(normalized.values()):
            continue
        r = {c: normalized.get(c, "") for c in MASTER_COLUMNS}
        r["master_row"] = index
        r["employee_id"] = r["사번"]
        r["email_normalized"] = r["이메일주소"].lower()
        r["is_ta"] = compact(r["직위명"]).upper() == "T/A"
        r["is_seconded"] = r["출향여부"].upper() == "Y"
        r["is_on_leave"] = r["재직상태"] == "휴직"
        r["in_staff_scope"] = r["직종명"] in ALLOWED_JOB_TYPES
        r["identity_ok"] = bool(re.fullmatch(r"[A-Za-z0-9-]+", r["employee_id"]) and r["성명"])
        r["email_mapping_ok"] = bool(re.fullmatch(r"[^\s@]+@"+re.escape(ALLOWED_EMAIL_DOMAIN), r["email_normalized"]))
        ancestry = [(c, r[c]) for c in ("본부명", "그룹총괄/공장", "그룹명", "팀명")]
        r["team_key"] = org_key("team", ancestry) if r["팀명"] else ""
        r["part_key"] = org_key("part", ancestry+[("파트명", r["파트명"])]) if r["파트명"] else ""
        r["org_path"] = " > ".join(r[c] for c in ("본부명", "그룹명", "팀명", "파트명") if r[c])
        r["org_path_full"] = " > ".join(v for _, v in ancestry+[("파트명", r["파트명"])] if v)
        r["org_status"] = "resolved" if (r["team_key"] or r["part_key"]) else "team_unassigned"
        r["leader_candidate"] = {"팀장": "team_leader", "파트장": "part_leader"}.get(r["직책명"], "member")
        # leader_candidate는 관리자 설정의 참고값일 뿐 실제 권한을 부여하지 않습니다.
        if not r["identity_ok"]:
            issues.append(issue("MASTER_ID_INVALID", "사번 또는 성명이 비어 있거나 사번 형식이 잘못되었습니다.", r["employee_id"]))
        if not r["email_mapping_ok"]:
            issues.append(issue("EMAIL_INVALID", "회사 이메일이 비어 있거나 형식/도메인을 확인해야 합니다.", r["employee_id"]))
        if not r["team_key"] and not r["part_key"]:
            # 팀 없이 그룹 바로 아래 있는 파트는 정상입니다. 팀·파트가 모두 없을 때만 알립니다(그룹 직속 등).
            issues.append(issue("TEAM_UNASSIGNED", "팀명과 파트명이 모두 없습니다. 그룹 직속 등으로 집계합니다.", r["employee_id"], level="info"))
        if r["재직상태"] not in ("재직", "휴직"):
            issues.append(issue("EMPLOYMENT_STATUS_UNKNOWN", "재직상태 값이 재직/휴직 이외입니다.", r["employee_id"]))
        rows.append(r)
    for field, code in (("employee_id", "MASTER_ID_DUPLICATE"), ("email_normalized", "EMAIL_DUPLICATE")):
        counts = collections.Counter(r[field] for r in rows if r[field])
        for r in rows:
            if r[field] and counts[r[field]] > 1:
                if field == "employee_id":
                    r["identity_ok"] = False
                r["email_mapping_ok"] = False
                issues.append(issue(code, "명부에서 중복된 "+("사번입니다. PDF 연결을 보류합니다." if field == "employee_id" else "이메일입니다. 계정 연결을 보류합니다."), r["employee_id"]))
    # 한 조직 경로가 여러 사업장에 걸쳐 있으면 동일 조직인지 확인할 수 있게 알립니다.
    sites = collections.defaultdict(set)
    for r in rows:
        if r["team_key"] and r["사업장"]:
            sites[r["team_key"]].add(r["사업장"])
    for r in rows:
        if len(sites[r["team_key"]]) > 1:
            r["org_status"] = "review_multi_site"
            issues.append(issue("ORG_MULTI_SITE", "같은 팀 경로에 여러 사업장이 있습니다. 동일 팀인지 확인 후 조직 조회에 사용하세요.", r["employee_id"], level="info"))
    return rows, issues


def connect_people(roster, files):
    """현재 폴더의 파일만 명부와 연결. 삭제된 파일의 캐시는 집계에 재사용하지 않습니다."""
    issues, people = [], []
    by_id = collections.defaultdict(list)
    roster_ids = {r["employee_id"] for r in roster if r["employee_id"]}
    for f in files:
        if f["employee_id"]:
            by_id[f["employee_id"]].append(f)
            if f["employee_id"] not in roster_ids:
                issues.append(issue("PDF_NOT_IN_MASTER", "PDF 사번이 현재 명부에 없습니다.", f["employee_id"], f["source_path"]))
    for r in roster:
        person = dict(r)
        person.update({"source_path": "", "source_sha256": "", "report_type": "",
                       "print_date": "", "diagnosis_status": "missing", "analysis_eligible": False,
                       "pdf_count": len(by_id[r["employee_id"]]), "valid_map_count": 0,
                       "valid_interest_count": 0, "strengths_json": "[]", "orientation_json": "{}",
                       "evidence_json": "{}"})
        for k in MAP_KINDS:
            person.update({k+"_x": None, k+"_y": None, k+"_quadrant": "", k+"_page": None,
                           k+"_valid": False, k+"_intro": "", k+"_personal_json": "[]"})
        for suffix in INTEREST_NAMES.values():
            person["interest_"+suffix] = None
        choices = by_id[r["employee_id"]]
        selected = None
        if not r["identity_ok"]:
            person["diagnosis_status"] = "master_review"
        elif r["is_ta"]:
            person["diagnosis_status"] = "excluded_ta"
            if choices:
                issues.append(issue("TA_HAS_PDF", "T/A로 등록된 직원의 PDF가 있습니다. 직위명과 파일을 확인하세요.", r["employee_id"]))
        elif choices:
            explicit = PDF_SELECTION.get(r["employee_id"])
            if explicit:
                pick = [f for f in choices if f["source_path"] == explicit]
                if len(pick) == 1:
                    selected = pick[0]
                else:
                    person["diagnosis_status"] = "duplicate_review"
                    issues.append(issue("SELECTION_NOT_FOUND", "PDF_SELECTION 경로가 현재 폴더에 없습니다.", r["employee_id"]))
            elif len(choices) == 1:
                selected = choices[0]
            elif all(f["sha256"] for f in choices) and len({f["sha256"] for f in choices}) == 1:
                selected = sorted(choices, key=lambda f: f["source_path"])[0]
                issues.append(issue("IDENTICAL_PDF_COPIES", "동일한 내용의 PDF 사본이 여러 개여서 한 번만 반영했습니다.", r["employee_id"], selected["source_path"], level="info"))
            else:
                person["diagnosis_status"] = "duplicate_review"
                issues.append(issue("MULTIPLE_PDF_REPORTS", "서로 다른 PDF가 여러 개입니다. 사용할 파일을 확인하세요.", r["employee_id"]))
        if selected:
            payload = selected["payload"]
            person.update({"source_path": selected["source_path"], "source_sha256": selected["sha256"],
                           "report_type": payload["report_type"], "print_date": payload["print_date"]})
            # 직원 식별은 PDF 본문에 적힌 이름이 아니라 파일명으로 합니다.
            # 진단 당시 이름을 잘못 입력한 경우 PDF의 report_name은 오타일 수
            # 있으므로, 여기서 자동 연결을 보류하는 기준으로 사용하지 않습니다.
            # 사번은 파일명에서 읽어 명부와 연결하고, 성명은 파일명과 명부만
            # 비교합니다. PDF 안의 report_name은 참고용 출력으로만 보존합니다.
            filename_name = selected["filename_name"]
            mismatch = bool(filename_name and compact(filename_name) != compact(r["성명"]))
            if mismatch:
                person["diagnosis_status"] = "name_review"
                issues.append(issue("NAME_MISMATCH", "명부와 PDF 파일명의 성명이 다릅니다. 파일명을 확인하세요. PDF 본문 이름은 연결 기준으로 사용하지 않습니다.", r["employee_id"], selected["source_path"]))
            else:
                person["diagnosis_status"] = payload["extraction_status"]
                valid = payload["extraction_status"] in ("complete", "partial")
                person["analysis_eligible"] = bool(valid and r["in_staff_scope"] and r["재직상태"] in ("재직", "휴직"))
                person["valid_map_count"] = len(payload["maps"])
                person["valid_interest_count"] = len(payload["interests"])
                for k, v in payload["maps"].items():
                    person.update({k+"_x": v["x"], k+"_y": v["y"], k+"_quadrant": v["quadrant"],
                                   k+"_page": v["page"], k+"_valid": person["analysis_eligible"],
                                   k+"_intro": v["description"]["intro"],
                                   k+"_personal_json": dumps(v["description"]["personal"])})
                for label, score in payload["interests"].items():
                    person["interest_"+INTEREST_NAMES[label]] = score
                person["strengths_json"] = dumps(payload["strengths"])
                person["orientation_json"] = dumps(payload["orientation"])
                person["evidence_json"] = dumps({
                    "maps": {k: {a: b for a, b in v.items() if a != "description"} for k, v in payload["maps"].items()},
                    "interests_page": payload.get("interests_page"),
                    "strengths_page": payload.get("strengths_page"), "parser_version": PARSER_VERSION})
        people.append(person)
    return people, issues


def read_bounded(stream):
    data = stream.read(MAX_PDF_MB * 1024 * 1024 + 1)
    if len(data) > MAX_PDF_MB * 1024 * 1024:
        raise ValueError("개별 PDF 크기 제한 초과")
    return data


def process_files(folder, cache, paths, progress=True):
    """PDF는 한 파일씩 읽어 전체 원본을 동시에 메모리에 올리지 않습니다."""
    files, issues = [], []
    try:
        existing = {p.lstrip("/") for p in cache.list_paths_in_partition()}
    except Exception as exc:
        raise RuntimeError("birkman_cache 폴더를 읽을 수 없습니다. 레시피 출력 연결과 폴더 권한을 확인하세요.") from exc
    memory_cache = {}
    hits = 0
    for index, path in enumerate(paths, 1):
        eid, filename_name = file_identity(path)
        record = {"source_path": path, "employee_id": eid, "filename_name": filename_name,
                  "sha256": "", "file_bytes": 0, "cache_hit": False,
                  "payload": {"report_type": "unknown", "report_name": "", "print_date": "",
                              "page_count": 0, "maps": {}, "interests": {}, "strengths": [],
                              "orientation": {}, "issues": [], "extraction_status": "failed"}}
        if not eid:
            issues.append(issue("FILENAME_INVALID", "파일명이 사번_성명.pdf 형식이 아닙니다.", source_path=path))
        try:
            with folder.get_download_stream(path) as stream:
                data = read_bounded(stream)
            digest = hashlib.sha256(data).hexdigest()
            record["sha256"], record["file_bytes"] = digest, len(data)
            cache_path = "parsed/"+PARSER_VERSION+"/"+digest+".json"
            payload = memory_cache.get(digest)
            if payload is not None:
                record["cache_hit"] = True
            elif not FORCE_REEXTRACT and cache_path in existing:
                try:
                    with cache.get_download_stream(cache_path) as stream:
                        envelope = json.loads(stream.read().decode("utf-8"))
                    if envelope["sha256"] == digest and envelope["parser_version"] == PARSER_VERSION:
                        payload = envelope["payload"]
                        required = {"maps", "interests", "strengths", "orientation", "issues",
                                    "report_type", "report_name", "print_date", "page_count", "extraction_status"}
                        if not required.issubset(payload) or not isinstance(payload.get("maps"), dict):
                            raise ValueError("잘못된 캐시 구조")
                        record["cache_hit"] = True
                except Exception:
                    payload = None
                    issues.append(issue("CACHE_REBUILT", "캐시를 읽지 못해 PDF를 다시 추출했습니다.", eid, path, "info"))
            if payload is None:
                payload = extract_pdf(data)
                envelope = {"sha256": digest, "parser_version": PARSER_VERSION, "payload": payload}
                # 해시별 경로로 저장. PDF 원본 폴더에는 어떤 파일도 쓰지 않습니다.
                try:
                    with cache.get_writer(cache_path) as writer:
                        writer.write(dumps(envelope).encode("utf-8"))
                except Exception as exc:
                    # 권한/저장 장애는 개별 PDF 불량과 달라 전체 실행을 중단합니다.
                    raise RuntimeError("CACHE_WRITE_FAILED") from exc
            memory_cache[digest] = payload
            record["payload"] = payload
            if record["cache_hit"]:
                hits += 1
            for original in payload["issues"]:
                issues.append(dict(original, employee_id=eid, source_path=path))
        except RuntimeError as exc:
            if str(exc) == "CACHE_WRITE_FAILED":
                raise RuntimeError("캐시 폴더 쓰기에 실패했습니다. 폴더의 쓰기 권한과 남은 용량을 확인하세요.") from exc
            issues.append(issue("PDF_READ_ERROR", "PDF 처리 실패: "+type(exc).__name__, eid, path))
        except Exception as exc:
            issues.append(issue("PDF_READ_ERROR", "PDF 처리 실패: "+type(exc).__name__, eid, path))
        files.append(record)
        if progress and (index % PROGRESS_EVERY == 0 or index == len(paths)):
            print("PDF 처리 %d/%d · 캐시 재사용 %d" % (index, len(paths), hits), flush=True)
    return files, issues


# =====================================================================
# 3. 출력 스키마: 빈 결과에서도 열 형식이 바뀌지 않도록 명시합니다.
#    좌표: -1~+1. x 왼쪽=과제 / 오른쪽=사람, y 위=외향 / 아래=내향.
#    흥미 점수는 원본에 인쇄된 숫자. 서로 합쳐 100%로 만들지 않습니다.
# =====================================================================
PEOPLE_SCHEMA = [(c, "string") for c in MASTER_COLUMNS] + [
    ("run_id", "string"), ("processed_at", "string"), ("parser_version", "string"),
    ("master_row", "bigint"), ("employee_id", "string"), ("email_normalized", "string"),
    ("is_ta", "boolean"), ("is_seconded", "boolean"), ("is_on_leave", "boolean"),
    ("in_staff_scope", "boolean"), ("identity_ok", "boolean"), ("email_mapping_ok", "boolean"),
    ("team_key", "string"), ("part_key", "string"), ("org_path", "string"),
    ("org_path_full", "string"), ("org_status", "string"), ("leader_candidate", "string"),
    ("source_path", "string"), ("source_sha256", "string"), ("report_type", "string"),
    ("print_date", "string"), ("diagnosis_status", "string"), ("analysis_eligible", "boolean"),
    ("pdf_count", "bigint"), ("valid_map_count", "bigint"), ("valid_interest_count", "bigint"),
    ("strengths_json", "string"), ("orientation_json", "string"), ("evidence_json", "string"),
]
for _k in MAP_KINDS:
    PEOPLE_SCHEMA.extend([(_k+"_x", "double"), (_k+"_y", "double"), (_k+"_quadrant", "string"),
                          (_k+"_page", "bigint"), (_k+"_valid", "boolean"), (_k+"_intro", "string"),
                          (_k+"_personal_json", "string")])
PEOPLE_SCHEMA.extend(("interest_"+s, "bigint") for s in INTEREST_NAMES.values())
FILES_SCHEMA = [(n, "string") for n in (
    "run_id", "processed_at", "parser_version", "source_path", "employee_id", "filename_name", "sha256",
    "report_name", "report_type", "print_date", "extraction_status", "payload_json")]
FILES_SCHEMA += [(n, "bigint") for n in ("file_bytes", "page_count", "valid_map_count", "valid_interest_count")]
FILES_SCHEMA += [("cache_hit", "boolean")]
ISSUES_SCHEMA = [(n, "string") for n in (
    "run_id", "processed_at", "level", "issue_code", "employee_id", "source_path", "message")]
ISSUES_SCHEMA += [("source_page", "bigint")]


def write_rows(dataset, rows, schema):
    dataset.write_schema([{"name": n, "type": t} for n, t in schema])
    with dataset.get_writer() as writer:
        for row in rows:
            writer.write_row_dict({n: row.get(n) for n, _ in schema})


def run_dataiku():
    # Dataiku 외부에서 파일을 점검할 때 이 함수는 실행되지 않습니다.
    import dataiku
    master = dataiku.Dataset(MASTER_DATASET)
    source = dataiku.Folder(PDF_FOLDER_ID)
    cache = dataiku.Folder(CACHE_FOLDER)
    outputs = {n: dataiku.Dataset(n) for n in (PEOPLE_OUTPUT, FILES_OUTPUT, ISSUES_OUTPUT)}
    for output in outputs.values():
        # 장시간 PDF를 읽기 전에 출력 데이터셋의 존재/Flow 연결을 확인합니다.
        output.read_schema(raise_if_empty=False)
    # 같은 폴더를 실수로 캐시 출력에 지정하면 중단합니다.
    if source.get_info()["id"] == cache.get_info()["id"]:
        raise ValueError("PDF 원본 폴더와 캐시 출력 폴더는 별도여야 합니다.")
    schema = master.read_schema()
    roster, roster_issues = prepare_roster(list(master.iter_rows()), schema)
    if not roster:
        raise ValueError("직원 명부에 행이 없습니다. 기존 출력은 갱신하지 않았습니다.")
    paths = sorted(p for p in source.list_paths_in_partition()
                   if p.lower().endswith(".pdf") and not p.rsplit("/", 1)[-1].startswith("._"))
    if not paths:
        raise ValueError("입력 폴더에서 PDF를 찾지 못했습니다. 폴더 ID와 업로드 상태를 확인하세요.")
    run_id = dt.datetime.now(SOURCE_TIMEZONE).strftime("%Y%m%dT%H%M%S%f")
    timestamp = now()
    print("직원 명부 %d행 / PDF %d개 처리 시작" % (len(roster), len(paths)), flush=True)
    files, file_issues = process_files(source, cache, paths)
    people, join_issues = connect_people(roster, files)
    issues = roster_issues + file_issues + join_issues
    for person in people:
        person.update(run_id=run_id, processed_at=timestamp, parser_version=PARSER_VERSION)
    file_rows = []
    for f in files:
        p = f["payload"]
        row = {k: v for k, v in f.items() if k != "payload"}
        row.update({k: p[k] for k in ("report_name", "report_type", "print_date", "page_count", "extraction_status")})
        row.update(run_id=run_id, processed_at=timestamp, parser_version=PARSER_VERSION,
                   valid_map_count=len(p["maps"]), valid_interest_count=len(p["interests"]), payload_json=dumps(p))
        file_rows.append(row)
    for row in issues:
        row.update(run_id=run_id, processed_at=timestamp)
    # 출처/확인 목록을 먼저, 웹앱의 주 입력인 people은 마지막에 갱신합니다.
    # 출력 3개는 하나의 DB 트랜잭션은 아닙니다. 웹앱은 완료된 run_id만 사용해야 합니다.
    write_rows(outputs[FILES_OUTPUT], file_rows, FILES_SCHEMA)
    write_rows(outputs[ISSUES_OUTPUT], issues, ISSUES_SCHEMA)
    write_rows(outputs[PEOPLE_OUTPUT], people, PEOPLE_SCHEMA)
    summary = {
        "run_id": run_id, "명부행수": len(people), "PDF개수": len(files),
        "PDF추출상태": dict(collections.Counter(f["payload"]["extraction_status"] for f in files)),
        "명부연결상태": dict(collections.Counter(p["diagnosis_status"] for p in people)),
        "분석가능인원": sum(p["analysis_eligible"] for p in people),
        "확인항목수": len(issues), "캐시재사용파일수": sum(f["cache_hit"] for f in files),
    }
    # 쓰기를 모두 끝낸 다음에만 완료 표식을 갱신합니다. 중간 실패 시 새 완료 표식 없음.
    with cache.get_writer("latest_success.json") as writer:
        writer.write(dumps(summary).encode("utf-8"))
    print("실행 완료\n"+json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    run_dataiku()
