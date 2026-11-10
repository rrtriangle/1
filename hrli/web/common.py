"""화면 공통: 로그인 사용자, 공통 기간 필터 → 분석 컨텍스트, 표·카드 컴포넌트."""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date

import flask
import pandas as pd
from dash import Input, dash_table, dcc, html
from dash.exceptions import PreventUpdate

from .. import auth, db, metrics
from ..metrics import Period

# 모든 분석 메뉴가 공유하는 기간·기준 필터
FILTERS = [Input("f-kind", "value"), Input("f-year", "value"), Input("f-sub", "value"),
           Input("f-range", "start_date"), Input("f-range", "end_date"), Input("f-basis", "value"),
           Input("f-compare", "value"), Input("f-years", "value"), Input("tick", "data")]


def current_user() -> dict | None:
    return flask.session.get("user")


def require(perm: str) -> dict:
    """권한이 없으면 콜백을 중단한다. 모든 데이터 콜백의 첫 줄에서 호출한다."""
    user = current_user()
    if not auth.can(user, perm):
        raise PreventUpdate
    return user


@dataclass
class Ctx:
    user: dict
    period: Period
    basis: str
    fact_all: pd.DataFrame      # 전체 기간 이력(권한 범위·조직 기준 적용)
    fact: pd.DataFrame          # 선택 기간 이력
    pop: pd.DataFrame           # 참여율 분모
    pop_note: str
    compare: list[Period]
    employees: pd.DataFrame

    @property
    def can_person(self) -> bool:
        return auth.can(self.user, "person")

    @property
    def basis_label(self) -> str:
        return "교육 당시 조직 기준(교육일과 같은 달의 명부)" if self.basis == "at_time" else "현재 조직 기준"

    def population(self, p: Period) -> pd.DataFrame:
        return scope_rows(self.user, metrics.population(p, self.basis)[0])


def scope_rows(user: dict, df: pd.DataFrame) -> pd.DataFrame:
    """조직 관리자는 권한이 부여된 그룹의 데이터만 본다."""
    if user["role"] == "org_manager":
        return df[df["grp"] == user["org_scope"]]
    return df


def build_ctx(user, kind, year, sub, start, end, basis, cmp_mode, cmp_years, *_) -> Ctx:
    year = int(year or date.today().year)
    try:
        p = metrics.make_period(kind or "year", year, int(sub or 1), start, end)
    except (TypeError, ValueError):
        p = metrics.make_period("year", year)
    raw = metrics.load_fact()
    p = p.clip(metrics.last_data_date(raw))
    fact_all = scope_rows(user, metrics.with_org(raw, basis or "current"))
    pop, note = metrics.population(p, basis or "current")
    compare = []
    if cmp_mode == "yoy":
        compare = [p.shift_years(1)]
    elif cmp_mode == "prev":
        compare = [p.previous()]
    elif cmp_mode == "years":
        compare = [Period(q.start, q.end, f"{y}년 같은 기간") for y in sorted(cmp_years or [])
                   if y != p.start.year for q in [p.shift_years(p.start.year - int(y))]]
    return Ctx(user, p, basis or "current", fact_all, metrics.in_period(fact_all, p), scope_rows(user, pop), note,
               compare, scope_rows(user, metrics.load_employees()))


# ---------------------------------------------------------------- 컴포넌트

PCT_WORDS = ("율", "비중", "비율")
KO = {"emp_no": "사번", "name": "성명", "division": "본부·센터", "grp": "그룹·공장", "team": "팀", "part": "파트",
      "job_family": "직종", "position": "직위", "title": "직책", "shift": "근무조", "tenure_band": "근속연수 구간",
      "std_name": "표준 교육명", "field": "교육 분야", "course_name_raw": "원본 교육명",
      "institution_raw": "원본 교육기관", "institution_std": "표준 교육기관", "source_id": "원천 이력ID",
      "start_date": "시작일", "end_date": "종료일", "hours_raw": "원본 시간", "type_raw": "원본 유형",
      "completion": "수료 여부", "status": "재직 상태", "hire_date": "입사일"}


def kpi_text(key: str, v) -> str:
    if v is None or pd.isna(v):
        return "-"
    if any(w in key for w in PCT_WORDS):
        return f"{v * 100:.1f}%"
    return f"{v:,.1f}" if "시간" in key and "건수" not in key else f"{int(v):,}"


def table(df: pd.DataFrame, id: str | None = None, page_size: int = 12, **kw):
    """정렬·필터가 되는 표. 이름에 율·비중·비율이 들어간 수치 열은 백분율로 표시한다."""
    if df is None or df.empty:
        return html.Div("해당하는 데이터가 없습니다.", className="empty")
    d = df.reset_index() if df.index.name else df
    d = d.rename(columns=KO) if "columns" not in kw else d.copy()
    cols = []
    for c in d.columns:
        col = {"name": str(c), "id": str(c)}
        if pd.api.types.is_bool_dtype(d[c]):
            d[c] = d[c].map({True: "예", False: ""})
        elif pd.api.types.is_numeric_dtype(d[c]):
            col["type"] = "numeric"
            if any(w in str(c) for w in PCT_WORDS):
                col["format"] = {"specifier": ".1%"}
            elif pd.api.types.is_float_dtype(d[c]):
                small = d[c].abs().max() <= 1
                col["format"] = {"specifier": ",.2f" if small else ",.1f"}
        else:
            d[c] = d[c].astype(object).where(d[c].notna(), "").astype(str)
        cols.append(col)
    d.columns = [str(c) for c in d.columns]
    args = dict(
        data=d.to_dict("records"), columns=cols, page_size=page_size, sort_action="native", filter_action="native",
        style_as_list_view=True, style_table={"overflowX": "auto"},
        style_header={"fontWeight": "600", "backgroundColor": "#f3f2ee", "border": "none", "color": "#52514e"},
        style_cell={"fontFamily": "inherit", "fontSize": "13px", "padding": "6px 10px", "textAlign": "left",
                    "maxWidth": "320px", "overflow": "hidden", "textOverflow": "ellipsis", "border": "none",
                    "borderBottom": "1px solid #eceae4"},
        style_cell_conditional=[{"if": {"column_type": "numeric"}, "textAlign": "right"}])
    args.update(kw)
    return dash_table.DataTable(id=id, **args) if id else dash_table.DataTable(**args)


def card(label: str, value, delta: float | None = None, pct: bool = False, sub: str | None = None):
    if value is None:
        text = "-"
    elif pct:
        text = f"{value * 100:.1f}%"
    else:
        text = f"{value:,.1f}" if isinstance(value, float) and value % 1 else f"{int(value):,}"
    extra = []
    if delta is not None:
        extra.append(html.Div(f"{'▲' if delta > 0 else '▼' if delta < 0 else '–'} "
                              + (f"{abs(delta) * 100:.1f}%p" if pct else f"{abs(delta):,.1f}".rstrip("0").rstrip("."))
                              + " (비교 기간 대비)", className="delta"))
    if sub:
        extra.append(html.Div(sub, className="delta"))
    return html.Div([html.Div(label, className="card-label"), html.Div(text, className="card-value"), *extra],
                    className="card")


def section(title: str, *children, note: str | None = None):
    return html.Section([html.H3(title), html.P(note, className="note") if note else None, *children])


def grid(*figs, cols: int = 2):
    return html.Div([dcc.Graph(figure=f, config={"displayModeBar": False}) for f in figs], className=f"grid g{cols}")


def to_excel(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as w:
        for name, df in sheets.items():
            df.to_excel(w, sheet_name=name[:31], index=bool(df.index.name))
    return buf.getvalue()


def options(values) -> list[dict]:
    return [{"label": str(v), "value": v} for v in sorted({v for v in values if v is not None and v == v}, key=str)]


def log(user: dict, action: str, detail: str = "") -> None:
    db.log_access(user["username"], action, detail)
