"""사각지대·편중 분석, 교육 과정 분석, 차기 교육 대상자 탐색(기획안 2.4~2.6).

여기서 나오는 결과는 교육 기회와 운영 현황을 검토하기 위한 참고 자료다.
미참여·군집 결과를 개인 역량이나 조직 우열 평가에 쓰지 않는다.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from . import config
from .metrics import ORG_COLS, Period, in_period

PERSON_COLS = ["emp_no", "name", "division", "grp", "team", "job_family", "position", "title", "shift", "tenure_band"]


def non_participants(pop: pd.DataFrame, fact_period: pd.DataFrame, field: str | None = None) -> pd.DataFrame:
    """분석 대상 중 기간 내 (특정 분야) 교육 이력이 없는 직원."""
    f = fact_period if not field else fact_period[fact_period["field"] == field]
    return pop.loc[~pop["emp_no"].isin(f["emp_no"]), PERSON_COLS].reset_index(drop=True)


def last_training(fact_all: pd.DataFrame, pop: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """직원별 최근 교육일과 경과 연수. 이력이 전혀 없으면 최근 교육일이 비어 있다."""
    last = fact_all[fact_all["date"] <= pd.Timestamp(as_of)].groupby("emp_no")["date"].max().rename("최근 교육일")
    out = pop[PERSON_COLS].merge(last, on="emp_no", how="left")
    out["경과 연수"] = ((pd.Timestamp(as_of) - out["최근 교육일"]).dt.days / 365.25).round(1)
    out["최근 교육일"] = out["최근 교육일"].dt.date
    return out


def long_absent(fact_all: pd.DataFrame, pop: pd.DataFrame, as_of: date, years: float) -> pd.DataFrame:
    lt = last_training(fact_all, pop, as_of)
    return lt[lt["경과 연수"].isna() | (lt["경과 연수"] >= years)].sort_values(
        "경과 연수", ascending=False, na_position="first").reset_index(drop=True)


def concentration(fact: pd.DataFrame, pop: pd.DataFrame, by: str, where: pd.Series | None = None) -> pd.DataFrame:
    """편중 지수 = 집단의 참여이력 비중 ÷ 집단의 인원 비중. 1보다 크면 인원 대비 교육이 몰려 있다."""
    f = fact if where is None else fact[where]
    f = f[f["emp_no"].isin(pop["emp_no"])]
    head = pop.groupby(by)["emp_no"].nunique()
    head = head[head >= config.MIN_GROUP_SIZE]
    rec = f.groupby(by).size().reindex(head.index, fill_value=0)
    out = pd.DataFrame({"대상 인원": head, "참여이력": rec})
    out["인원 비중"] = out["대상 인원"] / out["대상 인원"].sum()
    out["이력 비중"] = out["참여이력"] / out["참여이력"].sum() if out["참여이력"].sum() else 0.0
    out["편중 지수"] = (out["이력 비중"] / out["인원 비중"]).round(2)
    return out.round({"인원 비중": 4, "이력 비중": 4}).sort_values("편중 지수", ascending=False)


def repeats(fact: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(같은 과정을 2회 이상 수강한 직원 목록, 과정별 반복 수강 인원 요약)."""
    g = fact.groupby(["emp_no", "name", "std_name"]).agg(수강횟수=("record_id", "size"), 최초=("date", "min"),
                                                         최근=("date", "max")).reset_index()
    rep = g[g["수강횟수"] > 1].sort_values("수강횟수", ascending=False)
    rep[["최초", "최근"]] = rep[["최초", "최근"]].apply(lambda s: s.dt.date)
    by_course = rep.groupby("std_name").agg(반복수강인원=("emp_no", "nunique"), 최대횟수=("수강횟수", "max")) \
        .sort_values("반복수강인원", ascending=False).reset_index()
    return rep.reset_index(drop=True), by_course


def cluster_orgs(fact: pd.DataFrame, pop: pd.DataFrame, by: str, k: int = 3) -> pd.DataFrame:
    """분야별 참여 비중과 1인당 시간으로 유사한 조직을 묶는다(탐색용, 우열 판단 아님)."""
    head = pop.groupby(by)["emp_no"].nunique()
    head = head[head >= config.MIN_GROUP_SIZE]
    f = fact[fact[by].isin(head.index)]
    mix = f.groupby([by, "field"]).size().unstack(fill_value=0)
    if len(mix) < max(k, 3):
        return pd.DataFrame()
    feat = mix.div(mix.sum(axis=1), axis=0)
    feat["1인당 시간"] = (f.groupby(by)["hours"].sum() / head).reindex(feat.index).fillna(0)
    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import StandardScaler

    x = StandardScaler().fit_transform(feat)
    out = feat.round(3)
    out["군집"] = [f"유형 {c + 1}" for c in KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(x)]
    xy = PCA(n_components=2, random_state=0).fit_transform(x)
    out["x"], out["y"] = xy[:, 0], xy[:, 1]
    return out


# ---------------------------------------------------------------- 교육 과정

def course_summary(fact: pd.DataFrame) -> pd.DataFrame:
    if fact.empty:
        return pd.DataFrame()
    runs = fact.drop_duplicates(["std_name", "start_date", "end_date", "institution"]).groupby("std_name").size()
    g = fact.groupby("std_name")
    first = lambda s: s.mode().iat[0] if len(s.mode()) else None  # noqa: E731
    out = pd.DataFrame({
        "교육 분야": g["field"].agg(first), "운영 구분": g["operation"].agg(first), "교육 방식": g["method"].agg(first),
        "교육기관": g["institution"].agg(first),
        "최초 운영": g["date"].min().dt.date, "최근 운영": g["date"].max().dt.date,
        "운영 횟수": runs, "참여 인원": g["emp_no"].nunique(), "참여이력": g.size(),
        "누적 시간": g["hours"].sum().round(1), "참여 그룹 수": g["grp"].nunique(), "참여 직종 수": g["job_family"].nunique(),
    })
    out.index.name = "표준 교육명"
    return out.sort_values("참여 인원", ascending=False)


def course_changes(fact_all: pd.DataFrame, p: Period) -> pd.DataFrame:
    """선택 기간과 전년 동기(같은 길이)의 운영 횟수를 비교해 과정 변화를 분류한다."""
    runs = fact_all.drop_duplicates(["std_name", "start_date", "end_date", "institution"])
    runs = runs[runs["date"] <= pd.Timestamp(p.end)]
    if runs.empty:
        return pd.DataFrame()
    first, last = runs.groupby("std_name")["date"].min(), runs.groupby("std_name")["date"].max()
    cur = in_period(runs, p).groupby("std_name").size().reindex(first.index, fill_value=0)
    prev = in_period(runs, p.shift_years(1)).groupby("std_name").size().reindex(first.index, fill_value=0)
    recent = last >= pd.Timestamp(p.start) - pd.DateOffset(years=3)
    status = np.select(
        [first >= pd.Timestamp(p.start), (cur == 0) & recent, cur == 0, cur > prev, cur < prev],
        ["신규 개설", "최근 중단", "과거 운영", "운영 증가", "운영 감소"], default="지속 운영")
    out = pd.DataFrame({"변화": status, "전년 동기 운영": prev, "선택 기간 운영": cur,
                        "최초 운영": first.dt.date, "최근 운영": last.dt.date})
    out.index.name = "표준 교육명"
    return out[recent].sort_values(["변화", "선택 기간 운영"], ascending=[True, False])


# ---------------------------------------------------------------- 대상자 탐색·시나리오

def find_targets(pop: pd.DataFrame, fact_all: pd.DataFrame, as_of: date, *, filters: dict | None = None,
                 not_field: str | None = None, years: float | None = None, not_course: str | None = None,
                 exclude: list[str] | None = None) -> pd.DataFrame:
    """조건에 맞는 교육 대상자 후보. 선정 기준은 담당자가 정하고, 결과는 확정이 아닌 검토 자료다.

    filters: {컬럼: [허용값]} / not_field+years: 최근 N년간 해당 분야 미이수(years 없으면 전체 기간)
    not_course: 해당 표준 교육명 미이수 / exclude: 제외할 사번
    """
    t = pop.copy()
    for col, vals in (filters or {}).items():
        if vals:
            t = t[t[col].isin(vals)]
    f = fact_all[fact_all["date"] <= pd.Timestamp(as_of)]
    if years:
        f = f[f["date"] > pd.Timestamp(as_of) - pd.DateOffset(months=int(round(years * 12)))]
    if not_field:
        t = t[~t["emp_no"].isin(f.loc[f["field"] == not_field, "emp_no"])]
    if not_course:
        t = t[~t["emp_no"].isin(fact_all.loc[fact_all["std_name"] == not_course, "emp_no"])]
    if exclude:
        t = t[~t["emp_no"].isin([e.strip() for e in exclude if e.strip()])]
    return last_training(fact_all, t, as_of).reset_index(drop=True)


def scenario_row(name: str, targets: pd.DataFrame, fact_all: pd.DataFrame, field: str | None, as_of: date) -> dict:
    """시나리오 비교용 한 줄: 대상 인원과 해당 분야 기존 경험 여부."""
    f = fact_all[fact_all["emp_no"].isin(targets["emp_no"])]
    exp = f[f["field"] == field]["emp_no"].nunique() if field else f["emp_no"].nunique()
    recent = in_period(f, Period(date(as_of.year - 1, as_of.month, 1), as_of, ""))["emp_no"].nunique()
    n = len(targets)
    return {"시나리오": name, "대상 인원": n, f"{field or '교육'} 경험자": exp,
            "경험 비율": round(exp / n, 3) if n else None, "최근 1년 교육 참여자": recent,
            "직종 구성": ", ".join(f"{k} {v}" for k, v in targets["job_family"].value_counts().head(4).items())}
