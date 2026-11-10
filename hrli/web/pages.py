"""분석 화면: 전사 교육 현황, 조직·직종별 분석, 교육 과정 분석, 사각지대·편중 분석."""
from __future__ import annotations

import pandas as pd
from dash import Input, Output, callback, dcc, html

from .. import analysis, classify, config, metrics, summary
from ..metrics import LABELS
from . import charts
from .common import FILTERS, build_ctx, card, grid, kpi_text, options, require, section, table

DIMS = [{"label": v, "value": k} for k, v in LABELS.items()]


@callback(Output("period-note", "children"), *FILTERS)
def period_note(*f):
    c = build_ctx(require("analysis"), *f)
    cmp = " · 비교: " + ", ".join(f"{p.start}~{p.end}" for p in c.compare) if c.compare else ""
    return (f"조회 기간 {c.period.start} ~ {c.period.end} ({c.period.label}) · {c.basis_label} · "
            f"참여율 분모: {c.pop_note}{cmp}")


# ================================================================ 전사 교육 현황

def home(user):
    return dcc.Loading(html.Div(id="home-body"))


def _payload(c):
    k = metrics.kpis(c.fact, c.pop)
    prev = metrics.kpis(metrics.in_period(c.fact_all, c.compare[0]), c.population(c.compare[0])) if c.compare else None
    return summary.build_payload(c.period.label, k, prev, metrics.group_summary(c.fact, c.pop, "grp"),
                                    metrics.group_summary(c.fact, c.pop, "job_family"),
                                    metrics.share(c.fact, "field"), c.pop_note)


@callback(Output("home-body", "children"), *FILTERS)
def home_body(*f):
    c = build_ctx(require("analysis"), *f)
    k = metrics.kpis(c.fact, c.pop)
    prev = metrics.kpis(metrics.in_period(c.fact_all, c.compare[0]), c.population(c.compare[0])) if c.compare else {}

    def d(key):
        return None if prev.get(key) is None or k[key] is None else k[key] - prev[key]

    cards = html.Div([
        card("교육 참여이력", k["참여이력 건수"], d("참여이력 건수")), card("과정 운영 횟수", k["과정 운영 횟수"], d("과정 운영 횟수")),
        card("참여 인원(중복 제거)", k["참여 인원"], d("참여 인원")),
        card("총 교육 시간", k["총 교육 시간"], d("총 교육 시간"), sub=f"시간 통계 제외 {k['시간 통계 제외 건수']:,}건"),
        card("1인당 평균 교육 시간", k["1인당 평균 교육 시간"], d("1인당 평균 교육 시간")),
        card("교육 참여율", k["참여율"], d("참여율"), pct=True, sub=f"대상 {k['분석 대상 인원']:,}명"),
        card("교육 미참여 인원", k["미참여 인원"], d("미참여 인원")), card("사외 교육 비중", k["사외 비중"], d("사외 비중"), pct=True),
    ], className="cards")

    y = metrics.yearly(c.fact_all)
    by_year_field = c.fact_all.groupby(["year", "field"]).size().unstack(fill_value=0)
    by_year_field = by_year_field[[x for x in config.FIELDS if x in by_year_field.columns]]
    by_year_op = c.fact_all.groupby(["year", "operation"]).size().unstack(fill_value=0)
    trend = grid(charts.bar(y["참여이력"], "연도별 교육 참여이력(건)", horizontal=False),
                 charts.bar(y["참여 인원"], "연도별 참여 인원(명)", horizontal=False, color=charts.SERIES[2]),
                 charts.bar(y["총 시간"], "연도별 총 교육 시간(시간)", horizontal=False, color=charts.SERIES[6]),
                 charts.stacked(by_year_op, "연도별 사내·사외 구성비", charts.OP_COLOR, pct=True),
                 charts.stacked(by_year_field, "연도별 교육 분야 구성비", charts.FIELD_COLOR, pct=True))

    targets = grid(*[charts.bar(metrics.group_summary(c.fact, c.pop, by)["참여율"], f"{LABELS[by]}별 교육 참여율", pct=True)
                     for by in ("job_family", "grp", "title", "shift")])
    comp = grid(charts.share_bar(metrics.share(c.fact, "operation"), "사내·사외 비중(참여이력 기준)", charts.OP_COLOR),
                charts.share_bar(metrics.share(c.fact, "method"), "온라인·오프라인 비중", charts.OP_COLOR),
                charts.share_bar(metrics.share(c.fact, "field").reindex(config.FIELDS).dropna(), "교육 분야별 비중",
                                 charts.FIELD_COLOR),
                cols=1)
    cs = analysis.course_summary(c.fact)
    top = grid(charts.bar(cs["참여 인원"].nlargest(10) if len(cs) else pd.Series(dtype=float), "참여 인원이 많은 과정 TOP 10"),
               charts.bar(cs["운영 횟수"].nlargest(10) if len(cs) else pd.Series(dtype=float), "운영 횟수가 많은 과정 TOP 10",
                          color=charts.SERIES[1]))
    out = [cards]
    if c.compare:
        cmp = pd.DataFrame({f"{p.label} ({p.start}~{p.end})": metrics.kpis(metrics.in_period(c.fact_all, p), c.population(p))
                            for p in [c.period, *c.compare]})
        cmp = cmp.apply(lambda row: row.map(lambda v: kpi_text(row.name, v)), axis=1)
        cmp.index.name = "지표"
        out.append(section("기간 비교", table(cmp, page_size=20, sort_action="none", filter_action="none"),
                           note="모든 기간에 같은 지표 정의와 같은 기간 길이를 적용한다."))
    out += [section("연도별 교육 운영 추이", trend, note="추이는 조회 기간과 관계없이 전체 연도를 보여 준다. "
                                                "진행 중인 연도는 연중 누계다."),
            section("교육 대상별 현황", targets, note=f"{c.basis_label}. 인원 {config.MIN_GROUP_SIZE}명 미만 집단은 표시하지 않는다."),
            section("교육 구성", comp), section("주요 교육 과정", top),
            section("분석 결과 요약", dcc.Markdown(summary.rule_summary(_payload(c))),
                    note="Python이 계산한 집계 결과를 정해진 규칙으로 문장화한 것이다. 외부 AI 서비스는 쓰지 않는다.")]
    return out


# ================================================================ 조직·직종별 분석

def org(user):
    return html.Div([
        html.Div([
            html.Label(["분석 기준", dcc.Dropdown(id="org-by", options=DIMS, value="grp", clearable=False)]),
            html.Label(["상위 조직: 본부·센터", dcc.Dropdown(id="org-division", placeholder="전체")]),
            html.Label(["상위 조직: 그룹·공장", dcc.Dropdown(id="org-grp", placeholder="전체")]),
        ], className="controls"),
        dcc.Loading(html.Div(id="org-body"))])


@callback(Output("org-division", "options"), Output("org-grp", "options"), Input("org-division", "value"), *FILTERS)
def org_options(division, *f):
    c = build_ctx(require("analysis"), *f)
    grp = c.pop if not division else c.pop[c.pop["division"] == division]
    return options(c.pop["division"]), options(grp["grp"])


@callback(Output("org-body", "children"), Input("org-by", "value"), Input("org-division", "value"),
          Input("org-grp", "value"), *FILTERS)
def org_body(by, division, grp, *f):
    c = build_ctx(require("analysis"), *f)
    fact, pop = c.fact, c.pop
    for col, v in (("division", division), ("grp", grp)):  # 상위 조직에서 하위 조직으로 좁혀 가며 본다
        if v:
            fact, pop = fact[fact[col] == v], pop[pop[col] == v]
    s = metrics.group_summary(fact, pop, by)
    shown = s[~s["소수 집단"]]
    return [
        section(f"{LABELS[by]}별 교육 참여 현황", table(s, page_size=15),
                grid(charts.bar(shown["참여율"], "교육 참여율", pct=True),
                     charts.bar(shown["1인당 시간"], "참여자 1인당 평균 교육 시간", color=charts.SERIES[6]),
                     charts.bar(shown["1인당 이력"], "참여자 1인당 평균 참여이력", color=charts.SERIES[2]),
                     charts.bar(shown["사외 비중"], "사외 교육 비중", pct=True, color=charts.SERIES[1])),
                note=f"{c.basis_label} · 분모: {c.pop_note}. 참여 건수만 보면 인원이 많은 조직이 높게 나오므로 인원 대비 지표를 함께 본다. "
                     f"{config.MIN_GROUP_SIZE}명 미만 집단은 비율·평균을 가린다."),
        section(f"{LABELS[by]} × 교육 분야 참여율", grid(charts.heatmap(metrics.field_matrix(fact, pop, by),
                                                              "집단 인원 중 해당 분야 교육 이력이 있는 인원 비율"), cols=1)),
        section("교육 분야 다양성", grid(charts.bar(shown["분야 다양성"], "분야 다양성 지수(0=한 분야 집중, 1=고른 분포)",
                                             color=charts.SERIES[3]), cols=1),
                note="이 분석은 교육 기회와 운영 특성을 파악하기 위한 것이며 조직 성과나 구성원 역량 평가에 쓰지 않는다."),
    ]


# ================================================================ 교육 과정 분석

def course(user):
    return html.Div([dcc.Loading(html.Div(id="course-body")),
                     section("과정별 상세", dcc.Dropdown(id="course-pick", placeholder="표준 교육명을 선택하세요"),
                             dcc.Loading(html.Div(id="course-detail")))])


@callback(Output("course-body", "children"), Output("course-pick", "options"), *FILTERS)
def course_body(*f):
    c = build_ctx(require("analysis"), *f)
    cs = analysis.course_summary(c.fact)
    ch = analysis.course_changes(c.fact_all, c.period)
    sim = classify.similar_courses(list(c.fact_all["std_name"].unique()))
    kw = classify.keywords(c.fact.drop_duplicates(["std_name", "start_date"])["std_name"]).head(20)
    body = [
        section("과정별 운영 현황", table(cs, page_size=12)),
        section("교육 과정 변화", grid(charts.bar(ch["변화"].value_counts() if len(ch) else pd.Series(dtype=float),
                                             "변화 유형별 과정 수", horizontal=False), cols=1),
                table(ch, page_size=12), note="선택 기간과 전년 동기(같은 길이)의 운영 횟수를 비교한다."),
        section("유사 교육 과정 통합 검토", table(sim, page_size=8),
                note="교육명 유사도로 찾은 표준화 후보다. 자동으로 통합하지 않으며, 통합은 데이터 관리 > 분류 기준에서 담당자가 확정한다."),
        section("교육명 주요 키워드", grid(charts.bar(kw, "운영된 과정의 교육명 키워드 빈도(불용어 제외, 유사어 통합)"), cols=1)),
    ]
    return body, sorted(c.fact_all["std_name"].unique())


@callback(Output("course-detail", "children"), Input("course-pick", "value"), *FILTERS)
def course_detail(name, *f):
    c = build_ctx(require("analysis"), *f)
    if not name:
        return None
    d = c.fact_all[c.fact_all["std_name"] == name]
    if d.empty:
        return html.Div("이력이 없습니다.", className="empty")
    runs = d.drop_duplicates(["start_date", "end_date", "institution"]).groupby("year").size()
    raw = d.groupby("course_name_raw").size().rename("참여이력").rename_axis("원본 교육명")
    return [grid(charts.bar(runs, "연도별 운영 횟수", horizontal=False),
                 charts.bar(d.groupby("year")["emp_no"].nunique(), "연도별 참여 인원", horizontal=False,
                            color=charts.SERIES[2]),
                 charts.bar(d.groupby("grp")["emp_no"].nunique().sort_values(ascending=False), "그룹·공장별 참여 인원(전체 기간)"),
                 charts.bar(d.groupby("job_family")["emp_no"].nunique().sort_values(ascending=False),
                            "직종별 참여 인원(전체 기간)", color=charts.SERIES[1])),
            html.H4("이 표준 교육명에 연결된 원본 교육명"), table(raw.to_frame(), page_size=5)]


# ================================================================ 사각지대·편중 분석

def gaps(user):
    return html.Div([
        html.Div([
            html.Label(["비교 기준", dcc.Dropdown(id="gap-by", options=DIMS, value="job_family", clearable=False)]),
            html.Label(["장기 미참여 기준(년)", dcc.Dropdown(id="gap-years", options=[1, 2, 3, 5], value=2, clearable=False)]),
            html.Label(["분야", dcc.Dropdown(id="gap-field", options=[x for x in config.FIELDS if x != "미분류"],
                                           value="소통", clearable=False)]),
        ], className="controls"),
        dcc.Loading(html.Div(id="gap-body"))])


@callback(Output("gap-body", "children"), Input("gap-by", "value"), Input("gap-years", "value"),
          Input("gap-field", "value"), *FILTERS)
def gap_body(by, years, field, *f):
    c = build_ctx(require("analysis"), *f)
    np_ = analysis.non_participants(c.pop, c.fact)
    absent = analysis.long_absent(c.fact_all, c.pop, c.period.end, float(years))
    no_field = analysis.non_participants(c.pop, c.fact, field)
    head = c.pop.groupby(by)["emp_no"].nunique()
    ok = head[head >= config.MIN_GROUP_SIZE].index

    def rate(people: pd.DataFrame) -> pd.Series:
        return (people.groupby(by)["emp_no"].nunique().reindex(ok, fill_value=0) / head[ok]).sort_values(ascending=False)

    def people(df, title):
        # 개인 목록은 개인 이력 조회 권한이 있는 사용자에게만 보인다
        return [html.H4(f"{title} ({len(df):,}명)"), table(df, page_size=8)] if c.can_person else \
            [html.P(f"{title}: {len(df):,}명 (개인 목록은 권한이 있는 사용자만 볼 수 있다)", className="note")]

    conc = analysis.concentration(c.fact, c.pop, by)
    ext = analysis.concentration(c.fact, c.pop, by, c.fact["operation"] == "사외")
    fld = analysis.concentration(c.fact, c.pop, by, c.fact["field"] == field)
    rep, rep_course = analysis.repeats(c.fact_all)
    gs = metrics.group_summary(c.fact, c.pop, by)
    cl = analysis.cluster_orgs(c.fact, c.pop, "team")
    blocks = [
        section("① 교육 미참여자",
                grid(charts.bar(rate(np_), f"{LABELS[by]}별 기간 내 미참여 비율", pct=True, color=charts.SERIES[7]),
                     charts.bar(rate(absent), f"{LABELS[by]}별 최근 {years}년 이상 미참여 비율", pct=True,
                                color=charts.SERIES[7])),
                *people(np_, "선택 기간 내 교육 미참여자"), *people(absent, f"최근 {years}년 이상 교육 이력이 없는 직원"),
                note="미참여는 교육 필요성을 확정하는 기준이 아니라 교육 기회와 운영 현황을 더 살펴보기 위한 참고 지표다."),
        section("② 조직별 교육 편중",
                grid(charts.bar(conc["편중 지수"], "전체 교육 편중 지수(1 = 인원 비중과 같음)"),
                     charts.bar(ext["편중 지수"], "사외 교육 편중 지수", color=charts.SERIES[1])),
                table(conc, page_size=10),
                note="편중 지수 = 집단의 참여이력 비중 ÷ 집단의 인원 비중. 1보다 크면 인원에 비해 교육이 몰려 있고, 작으면 적다."),
        section(f"③ 교육 분야별 공백: {field}",
                grid(charts.bar(fld["편중 지수"], f"{field} 교육 편중 지수", color=charts.FIELD_COLOR[field]),
                     charts.bar(rate(no_field), f"{LABELS[by]}별 기간 내 {field} 교육 미이수 비율", pct=True,
                                color=charts.SERIES[7])),
                grid(charts.heatmap(metrics.field_matrix(c.fact, c.pop, by), f"{LABELS[by]} × 분야 참여율"), cols=1)),
        section("④ 반복 교육", table(rep_course, page_size=8),
                *([html.H4("같은 과정을 2회 이상 수강한 직원"),
                   table(rep, page_size=8)]
                  if c.can_person else []),
                note="전체 기간 기준. 법정 교육처럼 반복이 정상인 과정도 포함되므로 과정 성격을 함께 확인한다."),
        section("⑤ 교육 다양성", grid(charts.bar(gs.loc[~gs["소수 집단"], "분야 다양성"].sort_values(ascending=False),
                                           f"{LABELS[by]}별 분야 다양성 지수", color=charts.SERIES[3]), cols=1)),
        section("⑥ 데이터 기반 패턴 탐색",
                grid(charts.clusters(cl, "교육 운영 특성이 비슷한 팀(분야별 참여 비중·1인당 시간 기준)"),
                     charts.network(c.fact, "grp", "그룹·공장 - 교육 과정 관계(선 굵기 = 참여이력)")),
                table(cl.drop(columns=["x", "y"]) if len(cl) else cl, page_size=8),
                note="군집은 탐색용 참고 자료다. 유형 번호에는 순서나 우열의 의미가 없으며 조직 평가에 쓰지 않는다."),
    ]
    return blocks
