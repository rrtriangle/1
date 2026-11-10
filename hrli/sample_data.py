"""가상 데이터 생성기. 실제 엑셀 컬럼이 확정되기 전 화면·기능 검증용이며 모든 인물·수치는 허구다."""
from __future__ import annotations

import io

import numpy as np
import pandas as pd

ORGS = {  # 본부: {그룹: [팀]}
    "생산본부": {"1공장": ["생산1팀", "설비1팀"], "2공장": ["생산2팀", "설비2팀"], "품질그룹": ["품질보증팀", "검사팀"]},
    "연구센터": {"선행연구그룹": ["소재연구팀", "공정연구팀"], "제품개발그룹": ["개발1팀", "개발2팀"]},
    "경영지원본부": {"인사총무그룹": ["인사팀", "총무팀"], "재무그룹": ["회계팀", "자금팀"]},
    "영업본부": {"국내영업그룹": ["영업1팀", "영업2팀"], "해외영업그룹": ["해외영업팀", "마케팅팀"]},
}
# (교육명, 교육기관, 원본 유형, 시간, 대상 태그)
COURSES = [
    ("공정 품질관리 실무", "한국표준협회", "사외", 16, "tech"), ("설비 보전 기본과정", "사내강사", "사내", 8, "tech"),
    ("설비보전 기본 과정", "사내강사", "사내", 8, "tech"), ("측정 및 검사 실무", "사내강사", "사내", 8, "insp"),
    ("산업안전 법정교육", "사내강사", "사내", 4, "all"), ("개인정보 보호 교육", "(주)휴넷", "사내", 2, "all"),
    ("문제해결 기본과정", "한국능률협회", "사외", 16, "office"), ("문제해결 과정 기본", "한국능률협회", "사외", 16, "office"),
    ("Communication 스킬업", "한국능률협회", "사외", 8, "office"), ("세대 공감 소통 워크숍", "사내강사", "사내", 4, "office"),
    ("신임 팀장 리더십", "한국생산성본부", "사외", 24, "lead"), ("직책자 코칭 리더십", "한국생산성본부", "사외", 16, "lead"),
    ("보고서 작성 스킬", "(주)휴넷", "사내", 6, "office"), ("데이터 분석 Python 입문", "휴넷", "사내", 20, "rnd"),
    ("재무회계 직무 기초", "휴넷", "사내", 12, "office"), ("비즈니스 영어 회화", "(주)휴넷", "사내", 40, "lang"),
    ("OPIc 대비반", "휴넷", "사내", 60, "lang"), ("중국어 첫걸음", "(주)휴넷", "사내", 40, "lang"),
    ("소재 분석 세미나", "한국공학한림원", "사외", 8, "rnd"), ("통계적 실험설계", "한국표준협회", "사외", 24, "rnd"),
    ("마음챙김 특강", "휴넷", "사내", 2, "all"), ("Step Up 업무 스킬", "사내강사", "사내", 8, "office"),
]
SUR = list("김이박최정강조윤장임한오서신권황안송류홍")
GIV = ["민준", "서연", "도윤", "지우", "하준", "서현", "예준", "수빈", "지호", "민서", "현우", "유진", "건우", "지민", "우진",
       "채원", "선우", "다은", "태윤", "은지"]


def _excel(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def generate(n: int = 360, start: str = "2024-01", end: str = "2026-10", seed: int = 7):
    """반환: (월별 명부 {YYYY-MM: DataFrame}, 전체 교육 이력 DataFrame). 컬럼명은 원본 엑셀을 흉내 낸 한글이다."""
    rng = np.random.default_rng(seed)
    months = [m.strftime("%Y-%m") for m in pd.period_range(start, end, freq="M")]
    teams = [(d, g, t) for d, gs in ORGS.items() for g, ts in gs.items() for t in ts]
    emps = []
    for i in range(n):
        d, g, t = teams[rng.integers(len(teams))]
        fam = ("검사" if t == "검사팀" else "기술" if d == "생산본부" else "연구" if d == "연구센터"
               else "전문" if rng.random() < 0.12 else "사무")
        yrs = float(rng.gamma(2.0, 4.5))
        title = "팀원"
        if yrs > 8 and rng.random() < 0.18:
            title = rng.choice(["파트장", "팀장", "그룹장"], p=[0.55, 0.35, 0.10])
        emps.append({
            "사번": str(10001 + i), "성명": rng.choice(SUR) + rng.choice(GIV), "재직상태": "재직",
            "입사일": (pd.Timestamp("2026-10-01") - pd.Timedelta(days=int(yrs * 365))).date().isoformat(),
            "본부": d, "그룹": g, "팀": t, "직종": fam,
            "직위": "사원" if yrs < 4 else "선임" if yrs < 9 else "책임" if yrs < 16 else "수석", "직책": title,
            "근무조": rng.choice(["A조", "B조", "C조"]) if fam in ("기술", "검사") and rng.random() < 0.7 else "주간",
            "_join": months[0] if yrs * 12 > len(months) else months[max(0, len(months) - 1 - int(yrs * 12))],
            "_leave": None, "_vanish": None,
        })
    for e in rng.choice(emps, 14, replace=False):   # 퇴직: 한 달 '퇴직'으로 표시된 뒤 명부에서 빠진다
        e["_leave"] = months[rng.integers(6, len(months) - 1)]
    for e in rng.choice(emps, 3, replace=False):    # 사유 없이 명부에서 빠짐(휴직 등) → 명부미포함 확인 대상
        e["_vanish"] = months[-2]
    moves = {e["사번"]: (months[rng.integers(8, len(months) - 2)], teams[rng.integers(len(teams))])
             for e in rng.choice(emps, 25, replace=False)}

    rosters = {}
    for m in months:
        rows = []
        for e in emps:
            if m < e["_join"] or (e["_leave"] and m > e["_leave"]) or (e["_vanish"] and m >= e["_vanish"]):
                continue
            r = {k: v for k, v in e.items() if not k.startswith("_")}
            if e["_leave"] == m:
                r["재직상태"] = "퇴직"
            if e["사번"] in moves and m >= moves[e["사번"]][0]:
                r["본부"], r["그룹"], r["팀"] = moves[e["사번"]][1]
            rows.append(r)
        rosters[m] = pd.DataFrame(rows)

    recs, rid = [], 0
    for m in months:
        for r in rosters[m].to_dict("records"):
            if r["재직상태"] != "재직":
                continue
            p = 0.16                                   # 월별 참여 확률. 아래는 데모용으로 넣은 편중
            p *= 0.35 if r["근무조"] != "주간" else 1.0   # 교대 근무자는 참여가 어렵다
            p *= 1.5 if r["직종"] in ("사무", "연구") else 1.0
            if rng.random() > p:
                continue
            w = []
            for name, inst, typ, hrs, tag in COURSES:
                ok = (tag == "all" or (tag == "tech" and r["직종"] == "기술") or (tag == "insp" and r["직종"] == "검사")
                      or (tag == "office" and r["직종"] in ("사무", "전문", "연구")) or (tag == "rnd" and r["직종"] == "연구")
                      or (tag == "lead" and r["직책"] != "팀원")
                      or (tag == "lang" and (r["그룹"] == "해외영업그룹" or rng.random() < 0.08)))
                w.append(1.0 if ok else 0.0)
            w = np.array(w)
            if "리더십" in "".join(c[0] for c, x in zip(COURSES, w) if x) and m < "2025-01":
                w[[i for i, c in enumerate(COURSES) if c[0] == "직책자 코칭 리더십"]] = 0  # 2025년 신설 과정
            if m >= "2026-01":
                w[[i for i, c in enumerate(COURSES) if c[0] == "세대 공감 소통 워크숍"]] = 0  # 2026년 중단 과정
            name, inst, typ, hrs, tag = COURSES[rng.choice(len(COURSES), p=w / w.sum())]
            day = int(rng.integers(1, 5)) * 5 if tag != "all" else 10  # 같은 날 열리는 차수로 묶이도록
            s = pd.Timestamp(f"{m}-{day:02d}")
            rid += 1
            recs.append({"교육이력ID": f"T{rid:06d}", "사번": r["사번"], "교육명": name, "시작일": s.date().isoformat(),
                         "종료일": (s + pd.Timedelta(days=max(0, hrs // 8 - 1))).date().isoformat(),
                         "교육시간": float(rng.choice([hrs, 100, 0])) if tag == "lang" and rng.random() < 0.3 else float(hrs),
                         "교육기관": inst, "교육유형": typ, "수료여부": "수료" if rng.random() < 0.95 else "미수료"})
    return rosters, pd.DataFrame(recs)


def demo_files(seed: int = 7) -> dict:
    """데모 적재용 파일 묶음. 마지막 달 교육 이력은 검토 화면 시연을 위해 문제 행을 섞은 증분 파일로 따로 만든다."""
    rosters, tr = generate(seed=seed)
    last = max(rosters)
    full, inc = tr[tr["시작일"] < f"{last}-01"], tr[tr["시작일"] >= f"{last}-01"].copy()
    inc = inc.drop(columns="교육이력ID")  # 원천 ID가 없는 파일 → 항목 조합으로 중복 판별
    old = full.drop(columns="교육이력ID").tail(3)
    changed = old.head(1).assign(교육시간=99.0)                       # 기존 이력과 일부 항목만 다름 → 중복 의심
    bad = inc.head(2).assign(사번=["99999", None])                    # 명부에 없는 사번, 사번 누락
    wrong = inc.tail(1).assign(종료일="2020-01-01")                   # 종료일 오류
    inc = pd.concat([inc, old, changed, bad, wrong], ignore_index=True)
    return {"rosters": {m: _excel(d) for m, d in rosters.items()}, "training_full": _excel(full),
            "training_incremental": _excel(inc), "last_month": last}
