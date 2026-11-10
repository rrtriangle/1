"""교육 분류 규칙과 교육명 유사도 분석.

자동 분류는 '제안'이다. 결과는 course_master.confirmed=0 으로 저장되고 담당자가 검토·확정한다.
근거가 없으면 추정하지 않고 '미분류'로 남긴다.
"""
from __future__ import annotations

import re

import pandas as pd

# 분야 키워드. 위에서부터 먼저 일치하는 분야를 적용한다.
FIELD_KEYWORDS = [
    ("어학", r"영어|중국어|일본어|어학|회화|토익|toeic|opic|오픽|hsk|jlpt|english|비즈니스\s*일본어"),
    ("리더십", r"리더십|리더쉽|leadership|leader|팀장|코칭|신임\s*(팀장|직책자)|직책자"),
    ("소통", r"소통|커뮤니케이션|communication|협업|갈등|피드백|대화법"),
    ("문제해결", r"문제\s*해결|problem|로지컬|논리적\s*사고|6\s*시그마|식스시그마|triz|트리즈"),
    ("업무능력", r"보고서|문서\s*작성|기획력|엑셀|excel|파워포인트|ppt|프레젠테이션|시간\s*관리|업무\s*(능력|스킬)"),
    ("법정·공통", r"법정|성희롱|개인정보|산업안전|안전보건|장애인\s*인식|직장\s*내\s*괴롭힘|윤리|컴플라이언스"),
    ("직무", r"직무|공정|품질|설비|생산|회계|재무|인사|구매|영업|마케팅|설계|분석|개발|python|파이썬|sql|데이터|통계|검사|측정|용접|전기|기계|화학|자격"),
]
LANG_RE = re.compile(FIELD_KEYWORDS[0][1], re.I)
JOB_RE = re.compile(FIELD_KEYWORDS[-1][1], re.I)
ONLINE_RE = re.compile(r"온라인|이러닝|e-?learning|사이버|인터넷\s*강의|동영상|비대면|zoom", re.I)
OFFLINE_RE = re.compile(r"오프라인|집합|대면|현장|실습", re.I)


def std_institution(raw) -> str:
    """교육기관 표준명 제안. 법인 표기만 정리하고, 휴넷 계열은 '휴넷'으로 통일한다."""
    if raw is None or str(raw).strip() == "" or str(raw) == "nan":
        return "미확인"
    s = re.sub(r"\(주\)|㈜|주식회사|\(사\)|\(재\)", "", str(raw)).strip()
    s = re.sub(r"\s+", " ", s)
    if re.search(r"휴넷|hunet", s, re.I):
        return "휴넷"
    return s or "미확인"


def classify_course(name: str, institution_raw=None, type_raw=None) -> dict:
    """원본 교육명·교육기관·원본 유형값으로 분류를 제안한다."""
    name = re.sub(r"\s+", " ", str(name)).strip()
    inst = std_institution(institution_raw)
    t = "" if type_raw is None else str(type_raw)
    notes = []

    if inst == "휴넷":
        # 기획안 5.1: 휴넷은 사내 온라인으로 분류. 분야는 교육명으로 직무/어학만 판별하고 불확실하면 미분류.
        operation, method = "사내", "온라인"
        if LANG_RE.search(name):
            field = "어학"
        elif JOB_RE.search(name):
            field = "직무"
        else:
            field = "미분류"
        notes.append("휴넷 규칙(사내·온라인)")
    else:
        operation = "사내" if "사내" in t else "사외" if "사외" in t else "미분류"
        text = f"{name} {t}"
        method = "온라인" if ONLINE_RE.search(text) else "오프라인" if OFFLINE_RE.search(text) else "미분류"
        field = "미분류"
        for f, pat in FIELD_KEYWORDS:
            if re.search(pat, name, re.I):
                field = f
                notes.append(f"교육명 키워드→{f}")
                break
    return {"std_name": name, "field": field, "operation": operation, "method": method,
            "rule_note": "; ".join(notes) or None}


def similar_courses(names: list[str], threshold: float = 0.6, top: int = 200) -> pd.DataFrame:
    """표준 교육명 간 문자 n-gram 빈도 코사인 유사도(어순·띄어쓰기 차이에 둔감). 통합 '후보'만 제안하며 자동 통합하지 않는다."""
    names = sorted({n for n in names if n})
    cols = ["교육명 A", "교육명 B", "유사도"]
    if len(names) < 2:
        return pd.DataFrame(columns=cols)
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    norm = [re.sub(r"[\s\(\)\[\]_\-·/]+", "", n).lower() for n in names]
    m = TfidfVectorizer(analyzer="char", ngram_range=(1, 2), use_idf=False).fit_transform(norm)
    sim = cosine_similarity(m)
    out = [(names[i], names[j], round(float(sim[i, j]), 3))
           for i in range(len(names)) for j in range(i + 1, len(names)) if sim[i, j] >= threshold]
    return pd.DataFrame(out, columns=cols).sort_values("유사도", ascending=False).head(top).reset_index(drop=True)


STOPWORDS = set("과정 교육 기본 심화 입문 실무 향상 및 위한 대한 특강 워크숍 워크샵 프로그램 차수 기수 온라인 오프라인 "
                "the of and for a to in 년 차 기".split())
SYNONYMS = {"커뮤니케이션": "소통", "communication": "소통", "리더쉽": "리더십", "leadership": "리더십",
            "english": "영어", "excel": "엑셀"}


def keywords(names: pd.Series) -> pd.Series:
    """교육명 키워드 빈도(유사어 통합·불용어 제거 후). 형태소 분석기 없이 정규표현식으로 토큰화한다."""
    counts: dict[str, int] = {}
    for n in names.dropna():
        for tok in re.findall(r"[가-힣]{2,}|[A-Za-z]{2,}", str(n)):
            tok = SYNONYMS.get(tok.lower(), tok.lower() if tok.isascii() else tok)
            if tok in STOPWORDS:
                continue
            counts[tok] = counts.get(tok, 0) + 1
    return pd.Series(counts, dtype="int64").sort_values(ascending=False)
