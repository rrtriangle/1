"""Dash 앱: 로그인, 메뉴, 공통 기간 필터. 실행: python -m hrli.web.app"""
from __future__ import annotations

import os
import secrets
from datetime import date

import flask
from dash import Dash, Input, Output, State, callback, dcc, html

from .. import auth, config, db, metrics
from . import admin, pages, person_plan  # noqa: F401 - 콜백 등록
from .common import current_user, log

MENU = [  # (경로, 이름, 필요 권한)
    ("/", "전사 교육 현황", "analysis"), ("/person", "개인별 교육 이력", "person"),
    ("/org", "조직·직종별 분석", "analysis"), ("/course", "교육 과정 분석", "analysis"),
    ("/gaps", "사각지대·편중 분석", "analysis"), ("/plan", "차기 교육 설계 지원", "analysis"),
    ("/admin", "데이터 관리", "manage"),
]


def login_layout():
    return html.Div([
        html.H1("HR Learning Intelligence"), html.P("전사 교육 이력 관리 및 교육 전략 분석", className="note"),
        dcc.Input(id="login-user", placeholder="아이디", autoComplete="username"),
        dcc.Input(id="login-pw", placeholder="비밀번호", type="password", autoComplete="current-password",
                  n_submit=0),
        html.Button("로그인", id="login-btn", n_clicks=0, className="primary"),
        html.Div(id="login-msg", className="error"), dcc.Location(id="login-redirect"),
    ], className="login")


def filter_bar():
    this_year = date.today().year
    years = db.read("SELECT DISTINCT substr(start_date,1,4) y FROM training_record WHERE start_date IS NOT NULL")["y"]
    years = sorted({int(y) for y in years} | {this_year}, reverse=True)
    return html.Div([
        html.Label(["기간 단위", dcc.Dropdown(id="f-kind", clearable=False, value="year", options=[
            {"label": "연간", "value": "year"}, {"label": "반기", "value": "half"}, {"label": "분기", "value": "quarter"},
            {"label": "월별", "value": "month"}, {"label": "사용자 지정", "value": "custom"}])]),
        # 최초 진입 기본값은 현재 연도(전체 누적이 아니다)
        html.Label(["연도", dcc.Dropdown(id="f-year", clearable=False, value=this_year, options=years)]),
        html.Label(["세부 기간", dcc.Dropdown(id="f-sub", clearable=False, value=1, options=[1], disabled=True)]),
        html.Label(["사용자 지정 기간", dcc.DatePickerRange(id="f-range", start_date=date(this_year, 1, 1),
                                                     end_date=date.today(), display_format="YYYY-MM-DD")]),
        html.Label(["소속 기준", dcc.Dropdown(id="f-basis", clearable=False, value="current", options=[
            {"label": "현재 조직 기준", "value": "current"}, {"label": "교육 당시 조직 기준", "value": "at_time"}])]),
        html.Label(["비교", dcc.Dropdown(id="f-compare", clearable=False, value="yoy", options=[
            {"label": "비교 안 함", "value": "none"}, {"label": "전년 동기 대비", "value": "yoy"},
            {"label": "직전 기간 대비", "value": "prev"}, {"label": "여러 연도 비교", "value": "years"}])]),
        html.Label(["비교 연도", dcc.Dropdown(id="f-years", multi=True, options=years, value=[])]),
    ], className="filters")


def shell(user: dict):
    links = [dcc.Link(name, href=path, className="nav") for path, name, perm in MENU if auth.can(user, perm)]
    scope = f" · {user['org_scope']}" if user["role"] == "org_manager" else ""
    return html.Div([
        dcc.Location(id="url"), dcc.Store(id="tick", data=0),
        html.Nav([html.Div("HR Learning Intelligence", className="brand"), *links,
                  html.Div([html.Div(f"{user['username']} ({config.ROLES[user['role']]}{scope})"),
                            html.A("로그아웃", href="/logout")], className="who")], className="side"),
        html.Main([filter_bar(), html.Div(id="period-note", className="note"), html.Div(id="page")]),
    ], className="shell")


def serve_layout():
    user = current_user()
    return shell(user) if user else login_layout()


@callback(Output("login-redirect", "href"), Output("login-msg", "children"),
          Input("login-btn", "n_clicks"), Input("login-pw", "n_submit"),
          State("login-user", "value"), State("login-pw", "value"), prevent_initial_call=True)
def do_login(_n, _s, username, password):
    user = auth.verify(username, password)
    if not user:
        db.log_access(username, "로그인 실패")
        return None, "아이디 또는 비밀번호가 올바르지 않습니다."
    flask.session.clear()
    flask.session["user"] = user
    log(user, "로그인")
    return "/", ""


@callback(Output("f-sub", "options"), Output("f-sub", "value"), Output("f-sub", "disabled"), Input("f-kind", "value"))
def sub_options(kind):
    opts = {"half": [("상반기", 1), ("하반기", 2)], "quarter": [(f"{q}분기", q) for q in range(1, 5)],
            "month": [(f"{m}월", m) for m in range(1, 13)]}.get(kind)
    if not opts:
        return [1], 1, True
    return [{"label": a, "value": b} for a, b in opts], 1, False


@callback(Output("page", "children"), Input("url", "pathname"))
def route(pathname):
    """메뉴별 화면 틀만 그린다. 내용은 각 화면의 콜백이 공통 필터에 맞춰 채운다."""
    user = current_user()
    if not user:
        return dcc.Location(id="relogin", href="/", refresh=True)
    for path, name, perm in MENU:
        if path == (pathname or "/"):
            if not auth.can(user, perm):
                return html.Div("이 메뉴를 볼 권한이 없습니다.", className="error")
            builder = {"/": pages.home, "/org": pages.org, "/course": pages.course, "/gaps": pages.gaps,
                       "/person": person_plan.person, "/plan": person_plan.plan, "/admin": admin.skeleton}[path]
            return [html.H2(name), builder(user)]
    return html.Div("없는 화면입니다.", className="error")


def create_app() -> Dash:
    db.init_db()
    app = Dash(__name__, suppress_callback_exceptions=True, title="HR Learning Intelligence", update_title=None)
    key = os.environ.get("HRLI_SECRET_KEY")
    if not key:
        key = secrets.token_hex(32)  # 지정하지 않으면 재시작 때마다 로그인이 풀린다
    app.server.secret_key = key
    app.server.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                             SESSION_COOKIE_SECURE=os.environ.get("HRLI_HTTPS", "0") == "1",
                             MAX_CONTENT_LENGTH=50 * 1024 * 1024)
    app.layout = serve_layout

    @app.server.route("/logout")
    def logout():
        flask.session.clear()
        return flask.redirect("/")

    @app.server.before_request
    def guard():
        # 로그인 전에는 로그인 화면을 그리는 데 필요한 요청만 허용한다.
        if current_user() or not flask.request.path.startswith("/_dash-update-component"):
            return None
        body = flask.request.get_json(silent=True) or {}
        if "login-redirect" in str(body.get("output", "")):
            return None
        return flask.abort(401)

    return app


if __name__ == "__main__":
    create_app().run(host=os.environ.get("HRLI_HOST", "127.0.0.1"), port=int(os.environ.get("HRLI_PORT", "8050")),
                     debug=False)
