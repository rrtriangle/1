"""개인별 교육 이력 조회, 차기 교육 설계 지원(대상자 탐색·시나리오 비교)."""
from __future__ import annotations

from datetime import date

import pandas as pd
from dash import Input, Output, State, callback, dash_table, dcc, html
from dash.exceptions import PreventUpdate

from .. import analysis, config, metrics
from ..metrics import LABELS
from . import charts
from .common import FILTERS, build_ctx, card, grid, log, options, require, scope_rows, section, table, to_excel

F_STATES = [State(i.component_id, i.component_property) for i in FILTERS]
HIST_COLS = {"std_name": "표준 교육명", "course_name_raw": "원본 교육명", "start_date": "시작일", "end_date": "종료일",
             "operation": "운영 구분", "method": "교육 방식", "field": "교육 분야", "institution": "교육기관",
             "hours": "교육 시간(분석용)", "hours_raw": "원본 시간", "completion": "수료 여부"}
PERSON_KO = {"emp_no": "사번", "name": "성명", "division": "본부·센터", "grp": "그룹·공장", "team": "팀", "job_family": "직종",
             "position": "직위", "title": "직책", "shift": "근무조", "tenure_band": "근속 구간"}


# ================================================================ 개인별 교육 이력

def _no_table(msg: str):
    """결과가 없을 때도 선택 콜백이 참조하는 표(id=p-table)는 화면에 둔다."""
    return [html.Div(msg, className="empty"),
            html.Div(dash_table.DataTable(id="p-table", data=[], columns=[]), style={"display": "none"})]


def person(user):
    emp = scope_rows(user, metrics.load_employees())
    return html.Div([
        html.Div([
            html.Label(["사번 또는 성명", dcc.Input(id="p-q", debounce=True, placeholder="예: 10023, 김민준")]),
            html.Label(["그룹·공장", dcc.Dropdown(id="p-grp", options=options(emp["grp"]))]),
            html.Label(["직종", dcc.Dropdown(id="p-job", options=options(emp["job_family"]))]),
            html.Label(["직위", dcc.Dropdown(id="p-pos", options=options(emp["position"]))]),
            html.Label(["직책", dcc.Dropdown(id="p-title", options=options(emp["title"]))]),
        ], className="controls"),
        html.Div(_no_table("사번·성명 또는 조건을 입력하면 대상자가 표시됩니다."), id="p-results"),
        html.Div([
            dcc.Checklist(id="p-all", options=[{"label": " 전체 기간 이력 보기(공통 기간 필터 무시)", "value": "all"}], value=[]),
            html.Label(["교육 분야", dcc.Dropdown(id="p-field", options=config.FIELDS, multi=True)]),
            html.Label(["운영 구분", dcc.Dropdown(id="p-op", options=config.OPERATIONS, multi=True)]),
            html.Button("개인 교육 이력 Excel 다운로드", id="p-dl-btn", n_clicks=0), dcc.Download(id="p-dl"),
        ], className="controls"),
        dcc.Loading(html.Div(id="p-detail")),
    ])


@callback(Output("p-results", "children"), Input("p-q", "value"), Input("p-grp", "value"), Input("p-job", "value"),
          Input("p-pos", "value"), Input("p-title", "value"))
def person_search(q, grp, job, pos, title):
    user = require("person")
    if not any([q, grp, job, pos, title]):
        return _no_table("사번·성명 또는 조건을 입력하면 대상자가 표시됩니다.")
    e = scope_rows(user, metrics.load_employees())
    if q:
        q = q.strip()
        e = e[e["emp_no"].str.contains(q, regex=False) | e["name"].fillna("").str.contains(q, regex=False)]
    for col, v in (("grp", grp), ("job_family", job), ("position", pos), ("title", title)):
        if v:
            e = e[e[col] == v]
    view = e[list(PERSON_KO)[:8] + ["status"]].rename(columns=PERSON_KO | {"status": "재직 상태"})
    if view.empty:
        return _no_table("조건에 맞는 직원이 없습니다.")
    return [html.P(f"검색 결과 {len(view):,}명. 행을 선택하면 교육 이력이 표시됩니다.", className="note"),
            table(view, id="p-table", page_size=8, row_selectable="single", selected_rows=[0] if len(view) == 1 else [])]


def _person_data(user, emp_no, show_all, fields, ops, f):
    c = build_ctx(user, *f)
    e = c.employees[c.employees["emp_no"] == emp_no]
    if e.empty:
        raise PreventUpdate
    hist_all = c.fact_all[c.fact_all["emp_no"] == emp_no]
    hist = hist_all if show_all else metrics.in_period(hist_all, c.period)
    if fields:
        hist = hist[hist["field"].isin(fields)]
    if ops:
        hist = hist[hist["operation"].isin(ops)]
    return c, e.iloc[0], hist_all, hist.sort_values("date", ascending=False)


@callback(Output("p-detail", "children"), Input("p-table", "selected_rows"), Input("p-table", "derived_virtual_data"),
          Input("p-all", "value"), Input("p-field", "value"), Input("p-op", "value"), *FILTERS)
def person_detail(sel, rows, show_all, fields, ops, *f):
    user = require("person")
    if not sel or not rows or sel[0] >= len(rows):
        return None
    emp_no = rows[sel[0]]["사번"]
    c, e, hist_all, hist = _person_data(user, emp_no, bool(show_all), fields, ops, f)
    log(user, "개인 이력 조회", emp_no)
    hire = pd.to_datetime(e["hire_date"], errors="coerce")
    tenure = "-" if pd.isna(hire) else f"{(pd.Timestamp(date.today()) - hire).days / 365.25:.1f}년"
    last = hist_all["date"].max() if len(hist_all) else None
    gap = None if last is None else (pd.Timestamp(date.today()) - last).days / 365.25
    profile = html.Div([
        html.Div([html.B(f"{e['name']} ({e['emp_no']})"),
                  html.Span(f" · {e['status'] or '재직 상태 미확인'} · 명부 상태: {e['roster_state']}")]),
        html.Div(f"{e['division']} > {e['grp']} > {e['team']} · {e['job_family']} · {e['position']} / {e['title']} · "
                 f"근무조 {e['shift']} · 입사 {e['hire_date'] or '-'} (근속 {tenure})"),
    ], className="profile")
    cards = html.Div([
        card("누적 교육 참여이력", len(hist_all)), card("누적 교육 시간", round(float(hist_all["hours"].sum()), 1)),
        card("조회 조건 내 이력", len(hist)), card("조회 조건 내 시간", round(float(hist["hours"].sum()), 1)),
        card("최근 교육 참여", None, sub="이력 없음") if last is None else
        card("최근 교육 이후 경과(년)", round(gap, 1), sub=f"최근 교육일 {last.date()}"
             + (" · 2년 이상 미참여" if gap >= 2 else "")),
    ], className="cards")
    by_year = hist_all.groupby("year").agg(참여이력=("record_id", "size"), 교육시간=("hours", "sum"))
    return [profile, cards,
            grid(charts.bar(by_year["참여이력"], "연도별 참여이력(전체 기간)", horizontal=False),
                 charts.bar(by_year["교육시간"], "연도별 교육 시간(전체 기간)", horizontal=False, color=charts.SERIES[6]),
                 charts.share_bar(metrics.share(hist_all, "field"), "교육 분야별 이력 구성(전체 기간)", charts.FIELD_COLOR),
                 charts.share_bar(metrics.share(hist_all, "operation"), "사내·사외 구성(전체 기간)", charts.OP_COLOR)),
            html.H4(f"교육 이력 목록 ({'전체 기간' if show_all else c.period.label})"),
            table(hist[list(HIST_COLS)].rename(columns=HIST_COLS), page_size=15),
            html.P("교육 참여 이력은 교육 경험을 나타내는 자료다. 이 이력만으로 실제 역량 수준이나 교육 효과를 단정하지 않는다.",
                   className="note")]


@callback(Output("p-dl", "data"), Input("p-dl-btn", "n_clicks"), State("p-table", "selected_rows"),
          State("p-table", "derived_virtual_data"), State("p-all", "value"), State("p-field", "value"),
          State("p-op", "value"), *F_STATES, prevent_initial_call=True)
def person_download(_n, sel, rows, show_all, fields, ops, *f):
    user = require("download")
    if not sel or not rows:
        raise PreventUpdate
    emp_no = rows[sel[0]]["사번"]
    _, e, _, hist = _person_data(user, emp_no, bool(show_all), fields, ops, f)
    log(user, "Excel 다운로드", f"개인 교육 이력 {emp_no} ({len(hist)}건)")
    profile = e[list(PERSON_KO)].rename(PERSON_KO).to_frame("값").rename_axis("항목")
    return dcc.send_bytes(to_excel({"프로필": profile, "교육 이력": hist[list(HIST_COLS)].rename(columns=HIST_COLS)}),
                          f"training_history_{emp_no}.xlsx")


# ================================================================ 차기 교육 설계 지원

def plan(user):
    emp = scope_rows(user, metrics.load_employees())
    courses = sorted(metrics.load_fact()["std_name"].unique())
    dd = lambda i, col: html.Label([LABELS[col], dcc.Dropdown(id=i, options=options(emp[col]), multi=True)])  # noqa: E731
    return html.Div([
        section("① 교육 대상자 탐색",
                html.Div([dd("plan-job", "job_family"), dd("plan-title", "title"), dd("plan-pos", "position"),
                          dd("plan-grp", "grp"), dd("plan-tenure", "tenure_band")], className="controls"),
                html.Div([
                    html.Label(["이 분야 교육을 받지 않은 직원", dcc.Dropdown(
                        id="plan-notfield", options=[x for x in config.FIELDS if x != "미분류"])]),
                    html.Label(["최근 N년 이내 기준(비우면 전체 기간)", dcc.Dropdown(id="plan-years", options=[1, 2, 3, 5])]),
                    html.Label(["이 과정을 받지 않은 직원", dcc.Dropdown(id="plan-notcourse", options=courses)]),
                    html.Label(["제외할 사번(쉼표로 구분)", dcc.Input(id="plan-exclude", debounce=True)]),
                ], className="controls"),
                dcc.Loading(html.Div(id="plan-preview")),
                note="선정 기준과 제외 조건은 담당자가 정한다. 결과는 후보 목록이며 대상자를 자동으로 확정하지 않는다."),
        section("④ 교육 시나리오 비교",
                html.Div([html.Label(["시나리오 이름", dcc.Input(id="plan-name", placeholder="예: 전 직종 대상 소통 교육")]),
                          html.Button("현재 조건을 시나리오로 추가", id="plan-add", n_clicks=0, className="primary"),
                          html.Button("모두 지우기", id="plan-clear", n_clicks=0),
                          html.Button("대상자 후보 Excel 다운로드", id="plan-dl-btn", n_clicks=0), dcc.Download(id="plan-dl")],
                         className="controls"),
                dcc.Store(id="plan-scenarios", data=[]), dcc.Loading(html.Div(id="plan-compare")),
                note="위에서 정한 조건을 이름 붙여 저장하고, 시나리오별 대상 인원과 기존 교육 경험을 나란히 비교한다."),
        section("② 교육 프로그램 재검토 · ③ 차기 프로그램 구성 참고", dcc.Loading(html.Div(id="plan-review"))),
    ])


PLAN_IN = ["plan-job", "plan-title", "plan-pos", "plan-grp", "plan-tenure", "plan-notfield", "plan-years",
           "plan-notcourse", "plan-exclude"]


def _targets(c, job, title, pos, grp, tenure, notfield, years, notcourse, exclude):
    return analysis.find_targets(
        c.pop, c.fact_all, c.period.end,
        filters={"job_family": job, "title": title, "position": pos, "grp": grp, "tenure_band": tenure},
        not_field=notfield, years=years, not_course=notcourse, exclude=(exclude or "").split(","))


@callback(Output("plan-preview", "children"), *[Input(i, "value") for i in PLAN_IN], *FILTERS)
def plan_preview(*a):
    crit, f = a[:len(PLAN_IN)], a[len(PLAN_IN):]
    c = build_ctx(require("analysis"), *f)
    t = _targets(c, *crit)
    body = [html.Div([card("대상자 후보", len(t)), card("분석 대상 인원 대비", len(t) / len(c.pop) if len(c.pop) else None, pct=True),
                      card("교육 이력이 전혀 없는 인원", int(t["최근 교육일"].isna().sum()))], className="cards"),
            grid(charts.bar(t["job_family"].value_counts(), "후보의 직종 구성"),
                 charts.bar(t["grp"].value_counts(), "후보의 그룹·공장 구성", color=charts.SERIES[2]))]
    if c.can_person:
        body.append(table(t.rename(columns=PERSON_KO), page_size=10))
    return body


@callback(Output("plan-scenarios", "data"), Input("plan-add", "n_clicks"), Input("plan-clear", "n_clicks"),
          State("plan-scenarios", "data"), State("plan-name", "value"), *[State(i, "value") for i in PLAN_IN],
          prevent_initial_call=True)
def plan_store(_add, _clear, data, name, *crit):
    from dash import ctx as dash_ctx
    require("analysis")
    if dash_ctx.triggered_id == "plan-clear":
        return []
    data = list(data or [])
    data.append({"name": name or f"시나리오 {len(data) + 1}", "crit": list(crit)})
    return data[-8:]  # 비교 대상이 너무 많으면 읽기 어렵다


@callback(Output("plan-compare", "children"), Input("plan-scenarios", "data"), *FILTERS)
def plan_compare(data, *f):
    c = build_ctx(require("analysis"), *f)
    if not data:
        return html.Div("추가된 시나리오가 없습니다.", className="empty")
    rows = [analysis.scenario_row(s["name"], _targets(c, *s["crit"]), c.fact_all, s["crit"][5], c.period.end) for s in data]
    df = pd.DataFrame(rows)
    exp_cols = [x for x in df.columns if x.endswith("경험자")]
    df["기존 경험자"] = df[exp_cols].bfill(axis=1).iloc[:, 0]
    df = df.drop(columns=exp_cols)[["시나리오", "대상 인원", "기존 경험자", "경험 비율", "최근 1년 교육 참여자", "직종 구성"]]
    return [grid(charts.bar(df.set_index("시나리오")["대상 인원"], "시나리오별 예상 대상 인원"), cols=1),
            table(df, sort_action="none", filter_action="none"),
            html.P("'기존 경험자'는 조건에 지정한 분야(없으면 모든 교육)의 이력이 있는 인원이다.", className="note")]


@callback(Output("plan-dl", "data"), Input("plan-dl-btn", "n_clicks"), *[State(i, "value") for i in PLAN_IN], *F_STATES,
          prevent_initial_call=True)
def plan_download(_n, *a):
    user = require("download")
    crit, f = a[:len(PLAN_IN)], a[len(PLAN_IN):]
    t = _targets(build_ctx(user, *f), *crit)
    log(user, "Excel 다운로드", f"교육 대상자 후보 {len(t)}명")
    return dcc.send_bytes(to_excel({"대상자 후보": t.rename(columns=PERSON_KO)}), "training_target_candidates.xlsx")


@callback(Output("plan-review", "children"), *FILTERS)
def plan_review(*f):
    c = build_ctx(require("analysis"), *f)
    ch = analysis.course_changes(c.fact_all, c.period)
    stopped = ch[ch["변화"] == "최근 중단"] if len(ch) else ch
    cs = analysis.course_summary(c.fact)
    narrow = cs[(cs["참여 그룹 수"] <= 2) & (cs["참여 인원"] >= config.MIN_GROUP_SIZE)] if len(cs) else cs
    m_title = metrics.field_matrix(c.fact, c.pop, "title")
    m_job = metrics.field_matrix(c.fact, c.pop, "job_family")
    m_ten = metrics.field_matrix(c.fact, c.pop, "tenure_band")
    return [html.H4("운영이 중단된 과정(재개 검토)"), table(stopped, page_size=6),
            html.H4("일부 조직에서만 운영된 과정(참여 그룹 2개 이하)"), table(narrow, page_size=6),
            grid(charts.heatmap(m_job, "직종 × 분야 참여율"), charts.heatmap(m_title, "직책 × 분야 참여율"),
                 charts.heatmap(m_ten, "근속연수 구간 × 분야 참여율")),
            html.P("Step Up & Next Leader 체계와 연결할 때는 직종·근속 구간·직책별로 문제해결·소통·업무능력·리더십 분야의 "
                   "빈칸을 보고 보완 과정을 검토한다. 근속 구간 경계는 실제 RL 연차 기준이 확정되면 맞춰 조정한다. "
                   "프로그램과 대상자는 이 자료를 참고해 담당자가 최종 결정한다.", className="note")]
