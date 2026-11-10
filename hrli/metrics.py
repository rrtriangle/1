"""공통 지표 정의(기획안 7장). 모든 메뉴가 이 모듈의 계산만 사용해 지표가 화면마다 달라지지 않게 한다.

- 교육 참여이력 건수: 통계 제외되지 않고 직원 명부와 매칭된 이력 건수
- 교육 운영 횟수: (표준 교육명, 시작일, 종료일, 교육기관)이 서로 다른 조합 수
- 교육 참여 인원: 기간 내 이력이 있는 고유 사번 수
- 총 교육 시간: 시간 통계 포함 이력의 분석용 시간(보정값 우선) 합계
- 1인당 평균 교육 시간: 총 교육 시간 ÷ 고유 참여 인원
- 교육 참여율: 분석 대상 중 참여 인원 ÷ 분석 대상 인원
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import config, db

ORG_COLS = ["division", "grp", "team", "part", "job_family", "position", "title", "shift"]
LABELS = {"division": "본부·센터", "grp": "그룹·공장", "team": "팀", "part": "파트", "job_family": "직종",
          "position": "직위", "title": "직책", "shift": "근무조", "tenure_band": "근속연수 구간"}
ORG_HIERARCHY = ["division", "grp", "team", "part"]
UNKNOWN = "미확인"
NO_SNAPSHOT = "확인불가(해당 월 명부 없음)"


# ---------------------------------------------------------------- 기간

@dataclass(frozen=True)
class Period:
    start: date
    end: date
    label: str

    def shift_years(self, n: int) -> "Period":
        def sh(d: date) -> date:
            try:
                return d.replace(year=d.year - n)
            except ValueError:  # 2/29
                return d.replace(year=d.year - n, day=28)
        return Period(sh(self.start), sh(self.end), f"{self.label} 전년 동기" if n == 1 else f"{self.label} -{n}년")

    def previous(self) -> "Period":
        """같은 길이의 직전 기간."""
        months = (self.end.year - self.start.year) * 12 + self.end.month - self.start.month + 1
        if self.start.day == 1 and (self.end + timedelta(days=1)).day == 1:  # 월 단위 기간
            s = (pd.Timestamp(self.start) - pd.DateOffset(months=months)).date()
            return Period(s, self.start - timedelta(days=1), "직전 기간")
        n = (self.end - self.start).days + 1
        return Period(self.start - timedelta(days=n), self.start - timedelta(days=1), "직전 기간")

    def clip(self, last: date | None) -> "Period":
        """진행 중인 기간은 데이터가 있는 마지막 달까지로 잘라 비교 기간과 길이를 맞춘다."""
        if last is None or last >= self.end or last < self.start:
            return self
        end = (pd.Timestamp(last) + pd.offsets.MonthEnd(0)).date()
        end = min(end, self.end)
        return Period(self.start, end, f"{self.label}(~{end:%Y-%m})")


def make_period(kind: str, year: int, sub: int = 1, start: str | None = None, end: str | None = None) -> Period:
    if kind == "year":
        return Period(date(year, 1, 1), date(year, 12, 31), f"{year}년")
    if kind == "half":
        return Period(date(year, 1 if sub == 1 else 7, 1), date(year, 6, 30) if sub == 1 else date(year, 12, 31),
                      f"{year}년 {'상' if sub == 1 else '하'}반기")
    if kind == "quarter":
        s = date(year, 3 * (sub - 1) + 1, 1)
        return Period(s, (pd.Timestamp(s) + pd.offsets.QuarterEnd(0)).date(), f"{year}년 {sub}분기")
    if kind == "month":
        s = date(year, sub, 1)
        return Period(s, (pd.Timestamp(s) + pd.offsets.MonthEnd(0)).date(), f"{year}년 {sub}월")
    if kind == "custom":
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        if e < s:
            raise ValueError("종료일이 시작일보다 빠릅니다.")
        return Period(s, e, f"{s}~{e}")
    raise ValueError(kind)


# ---------------------------------------------------------------- 데이터 적재

def tenure_band(hire: pd.Series, as_of: date | None = None) -> pd.Series:
    as_of = pd.Timestamp(as_of or date.today())
    yrs = (as_of - pd.to_datetime(hire, errors="coerce")).dt.days / 365.25
    band = pd.cut(yrs, [-np.inf, 3, 7, 15, np.inf], labels=["3년 미만", "3~7년", "7~15년", "15년 이상"], right=False)
    return band.astype(object).where(yrs.notna(), UNKNOWN)


def load_employees(path=None) -> pd.DataFrame:
    e = db.read("SELECT * FROM employee", path=path)
    e[ORG_COLS] = e[ORG_COLS].astype(object).fillna(UNKNOWN)
    e["tenure_band"] = tenure_band(e["hire_date"]) if len(e) else UNKNOWN
    return e


def load_fact(path=None) -> pd.DataFrame:
    """분석용 교육 참여이력. 통계 제외 건과 명부 미매칭·분석 대상 외 직원의 이력은 빠진다."""
    f = db.read(
        "SELECT r.record_id, r.emp_no, r.course_name_raw, r.start_date, r.end_date, r.hours_raw, r.hours_adj,"
        " r.hours_included, r.institution_raw, r.completion,"
        " COALESCE(c.std_name, r.course_name_raw) AS std_name, COALESCE(c.field,'미분류') AS field,"
        " COALESCE(c.operation,'미분류') AS operation, COALESCE(c.method,'미분류') AS method,"
        " COALESCE(i.institution_std,'미확인') AS institution"
        " FROM training_record r JOIN employee e ON e.emp_no=r.emp_no AND e.in_scope=1"
        " LEFT JOIN course_master c ON c.course_name_raw=r.course_name_raw"
        " LEFT JOIN institution_master i ON i.institution_raw=r.institution_raw"
        " WHERE r.excluded=0", path=path)
    f["date"] = pd.to_datetime(f[config.DATE_BASIS], errors="coerce")
    f = f[f["date"].notna()].copy()
    f["year"] = f["date"].dt.year
    # 분석용 시간: 보정값 우선. 시간 통계 제외(개별 설정 또는 time_rule)면 NaN → 합계에서 빠지되 건수에는 남는다.
    hours = f["hours_adj"].where(f["hours_adj"].notna(), f["hours_raw"]).astype(float)
    off = f["hours_included"] == 0
    for r in db.read("SELECT scope, value FROM time_rule", path=path).itertuples():
        col = {"field": "field", "institution": "institution", "course": "std_name"}.get(r.scope)
        if col:
            off |= f[col] == r.value
    f["hours"] = hours.mask(off)
    f["hours_counted"] = ~off & hours.notna()
    return f.merge(load_employees(path)[["emp_no", "name", *ORG_COLS, "tenure_band"]], on="emp_no", how="left")


def in_period(fact: pd.DataFrame, p: Period) -> pd.DataFrame:
    return fact[(fact["date"] >= pd.Timestamp(p.start)) & (fact["date"] <= pd.Timestamp(p.end))]


def last_data_date(fact: pd.DataFrame) -> date | None:
    return None if fact.empty else fact["date"].max().date()


def with_org(fact: pd.DataFrame, basis: str, path=None) -> pd.DataFrame:
    """basis='current': 현재 조직 기준. 'at_time': 교육일과 같은 달의 명부 스냅샷 기준(없으면 확인불가)."""
    if basis == "current" or fact.empty:
        return fact
    snap = db.read(f"SELECT snapshot_month, emp_no, {', '.join(ORG_COLS)} FROM employee_snapshot", path=path)
    f = fact.drop(columns=ORG_COLS)
    f["snapshot_month"] = f["date"].dt.strftime("%Y-%m")
    f = f.merge(snap, on=["snapshot_month", "emp_no"], how="left", indicator=True)
    f[ORG_COLS] = f[ORG_COLS].astype(object)
    hit = f["_merge"] == "both"
    f.loc[hit, ORG_COLS] = f.loc[hit, ORG_COLS].fillna(UNKNOWN)
    f.loc[~hit, ORG_COLS] = NO_SNAPSHOT
    return f.drop(columns=["_merge", "snapshot_month"])


def population(p: Period, basis: str = "current", path=None) -> tuple[pd.DataFrame, str]:
    """참여율 분모(분석 대상 인원)와 그 산정 기준 설명.

    기간 내 월별 명부 스냅샷에 재직 상태로 한 번이라도 나타난 분석 대상 직원. 스냅샷이 없는 기간이면
    현재 명부의 재직자를 대신 쓰고 그 사실을 기준 설명에 밝힌다.
    """
    emp = load_employees(path)
    snap = db.read("SELECT * FROM employee_snapshot WHERE snapshot_month BETWEEN ? AND ?",
                   (f"{p.start:%Y-%m}", f"{p.end:%Y-%m}"), path)
    snap = snap[snap["status"].isna() | (snap["status"] == "재직")]
    scope = emp[emp["in_scope"] == 1]
    if snap.empty:
        pop = scope[scope["status"].isna() | (scope["status"] == "재직")]
        return pop, "기간 내 월별 명부가 없어 현재 명부의 재직자 기준(참고용)"
    if basis == "at_time":
        last = snap.sort_values("snapshot_month").groupby("emp_no").tail(1)
        last = last[last["emp_no"].isin(scope["emp_no"])].copy()
        last[ORG_COLS] = last[ORG_COLS].astype(object).fillna(UNKNOWN)
        last["tenure_band"] = tenure_band(last["hire_date"], p.end)
        return last, "기간 내 월별 명부 재직자, 소속은 기간 내 마지막 명부 기준"
    pop = scope[scope["emp_no"].isin(snap["emp_no"])]
    return pop, "기간 내 월별 명부 재직자, 소속은 현재 조직 기준"


# ---------------------------------------------------------------- 지표

def run_count(fact: pd.DataFrame) -> int:
    return len(fact.drop_duplicates(["std_name", "start_date", "end_date", "institution"]))


def diversity(counts: pd.Series) -> float:
    """정규화 섀넌 지수(0=한 분야 집중, 1=분야별 균등). 미분류는 분야로 세지 않는다."""
    c = counts[(counts > 0) & (counts.index != "미분류")]
    if len(c) < 2:
        return 0.0
    q = c / c.sum()
    return float(-(q * np.log(q)).sum() / np.log(len(config.FIELDS) - 1))


def share(fact: pd.DataFrame, col: str) -> pd.Series:
    return fact[col].value_counts(normalize=True) if len(fact) else pd.Series(dtype=float)


def kpis(fact: pd.DataFrame, pop: pd.DataFrame) -> dict:
    """기간으로 이미 걸러진 fact와 분모 pop으로 전사 핵심 지표를 계산한다."""
    participants = fact["emp_no"].nunique()
    in_pop = fact.loc[fact["emp_no"].isin(pop["emp_no"]), "emp_no"].nunique()
    hours = float(fact["hours"].sum())
    return {
        "참여이력 건수": len(fact),
        "과정 운영 횟수": run_count(fact),
        "참여 인원": participants,
        "총 교육 시간": round(hours, 1),
        "1인당 평균 교육 시간": round(hours / participants, 1) if participants else 0.0,
        "분석 대상 인원": len(pop),
        "미참여 인원": len(pop) - in_pop,
        "참여율": round(in_pop / len(pop), 4) if len(pop) else None,
        "사외 비중": round(float((fact["operation"] == "사외").mean()), 4) if len(fact) else None,
        "온라인 비중": round(float((fact["method"] == "온라인").mean()), 4) if len(fact) else None,
        "시간 통계 제외 건수": int((~fact["hours_counted"]).sum()),
    }


def group_summary(fact: pd.DataFrame, pop: pd.DataFrame, by: str, mask_small: bool = True) -> pd.DataFrame:
    """조직·직종 등 집단별 지표. 인원이 적은 집단은 재식별 위험 때문에 비율·평균을 가린다."""
    cols = ["대상 인원", "참여 인원", "참여율", "참여이력", "1인당 이력", "총 시간", "1인당 시간", "사외 비중",
            "분야 수", "분야 다양성"]
    head = pop.groupby(by)["emp_no"].nunique().rename("대상 인원")
    f = fact
    g = f.groupby(by)
    out = pd.concat([
        head,
        f[f["emp_no"].isin(pop["emp_no"])].groupby(by)["emp_no"].nunique().rename("참여 인원(대상 내)"),
        g["emp_no"].nunique().rename("참여 인원"),
        g.size().rename("참여이력"),
        g["hours"].sum().rename("총 시간"),
        g["operation"].agg(lambda s: (s == "사외").mean()).rename("사외 비중"),
        g["field"].agg(lambda s: s[s != "미분류"].nunique()).rename("분야 수"),
        g["field"].agg(lambda s: diversity(s.value_counts())).rename("분야 다양성"),
    ], axis=1)
    out[["대상 인원", "참여 인원", "참여 인원(대상 내)", "참여이력", "총 시간", "분야 수"]] = \
        out[["대상 인원", "참여 인원", "참여 인원(대상 내)", "참여이력", "총 시간", "분야 수"]].fillna(0)
    denom = out["대상 인원"].replace(0, np.nan)
    out["참여율"] = (out["참여 인원(대상 내)"] / denom).clip(upper=1)
    part = out["참여 인원"].replace(0, np.nan)
    out["1인당 이력"] = out["참여이력"] / part
    out["1인당 시간"] = out["총 시간"] / part
    out = out[cols].round({"참여율": 4, "1인당 이력": 2, "총 시간": 1, "1인당 시간": 1, "사외 비중": 4, "분야 다양성": 3})
    out["소수 집단"] = out["대상 인원"] < config.MIN_GROUP_SIZE
    if mask_small:
        out.loc[out["소수 집단"], ["참여율", "1인당 이력", "1인당 시간", "사외 비중", "분야 다양성"]] = np.nan
    out.index.name = LABELS.get(by, by)
    return out.sort_values("대상 인원", ascending=False)


def field_matrix(fact: pd.DataFrame, pop: pd.DataFrame, by: str) -> pd.DataFrame:
    """집단 × 교육 분야 참여율(해당 분야 이력이 있는 인원 ÷ 집단 대상 인원). 소수 집단은 제외한다."""
    head = pop.groupby(by)["emp_no"].nunique()
    head = head[head >= config.MIN_GROUP_SIZE]
    f = fact[fact["emp_no"].isin(pop["emp_no"]) & fact[by].isin(head.index)]
    m = f.groupby([by, "field"])["emp_no"].nunique().unstack(fill_value=0)
    m = m.reindex(index=head.index, columns=[c for c in config.FIELDS if c in m.columns], fill_value=0)
    return m.div(head, axis=0).clip(upper=1).round(4)


def yearly(fact: pd.DataFrame) -> pd.DataFrame:
    g = fact.groupby("year")
    return pd.DataFrame({"참여이력": g.size(), "참여 인원": g["emp_no"].nunique(), "총 시간": g["hours"].sum().round(1),
                         "사외 비중": g["operation"].agg(lambda s: (s == "사외").mean()).round(4)})


def compare(fact_all: pd.DataFrame, periods: list[Period], basis: str = "current", path=None) -> pd.DataFrame:
    """여러 기간의 핵심 지표를 같은 정의로 나란히 계산한다."""
    rows = {}
    for p in periods:
        pop, _ = population(p, basis, path)
        k = kpis(in_period(fact_all, p), pop)
        rows[f"{p.label}\n({p.start}~{p.end})"] = k
    return pd.DataFrame(rows)
