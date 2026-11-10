"""Dash 앱 구성: 로그인(Dataiku 계정 / 선택적 이메일 인증), 메뉴, 공통 기간 필터.

- 로컬 실행: python -m hrli.web.app
- Dataiku Dash 웹앱: Dataiku가 만들어 준 app에 attach(app, ...)를 호출한다(dataiku/webapp_backend.py 참고).

주소(URL)에 의존하지 않도록 메뉴 이동과 로그인 전환을 모두 콜백으로 처리한다.
Dataiku는 웹앱을 자체 경로 아래에서 띄우기 때문이다.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import flask
from dash import Dash, Input, Output, State, callback, dcc, html, no_update
from dash.exceptions import PreventUpdate

from .. import auth, config, db
from . import admin, pages, person_plan  # noqa: F401 - 콜백 등록
from .common import current_user, log
from .style import CSS

MENU = [  # (키, 이름, 필요 권한, 화면 함수)
    ("home", "전사 교육 현황", "analysis", pages.home), ("person", "개인별 교육 이력", "person", person_plan.person),
    ("org", "조직·직종별 분석", "analysis", pages.org), ("course", "교육 과정 분석", "analysis", pages.course),
    ("gaps", "사각지대·편중 분석", "analysis", pages.gaps), ("plan", "차기 교육 설계 지원", "analysis", person_plan.plan),
    ("admin", "데이터 관리", "manage", admin.skeleton),
]
PUBLIC_OUTPUTS = {"root", "login-msg", "auth-tick"}  # 로그인 전에도 호출할 수 있는 콜백의 출력
SKIP_AUTO = ("/_dash-component-suites/", "/_favicon", "/assets/", "/_reload-hash")
_identify: auth.Identify | None = None


def login_layout(dss_login: str | None):
    otp = auth.otp_enabled()
    parts = [html.H1("HR Learning Intelligence"), html.P("전사 교육 이력 관리 및 교육 전략 분석", className="note")]
    if _identify is not None:
        if dss_login:
            parts.append(html.P(f"Dataiku 사용자 '{dss_login}'은(는) 이 시스템에 등록되어 있지 않습니다. "
                                "시스템 관리자에게 등록을 요청하세요.", className="error"))
        parts.append(html.Button("Dataiku 계정으로 로그인", id="login-dku", n_clicks=0, className="primary"))
    else:
        parts.append(html.Div(html.Button(id="login-dku", n_clicks=0), style={"display": "none"}))
    if otp:
        parts += [html.Hr(), html.P("Dataiku 계정이 없으면 등록된 이메일로 인증 코드를 받아 로그인합니다.", className="note"),
                  dcc.Input(id="login-email", placeholder="등록된 이메일 주소", type="email", autoComplete="username"),
                  html.Button("인증 코드 받기", id="login-send", n_clicks=0),
                  dcc.Input(id="login-code", placeholder="메일로 받은 6자리 코드", inputMode="numeric",
                            autoComplete="one-time-code", maxLength=6, n_submit=0),
                  html.Button("로그인", id="login-btn", n_clicks=0, className="primary")]
    elif _identify is None:
        parts.append(html.P("로그인할 수 있는 방법이 없습니다. Dataiku 웹앱으로 열거나, 관리자가 이메일 인증 로그인을 켜야 합니다.",
                            className="error"))
    parts.append(html.Div(id="login-msg", className="note"))
    return html.Div(parts, className="login")


def _start_session(user: dict, how: str) -> None:
    flask.session.clear()
    flask.session.permanent = True
    flask.session["user"] = user
    log(user, f"로그인({how})")


def _try_dataiku_login() -> tuple[dict | None, str | None]:
    """Dataiku에 로그인한 브라우저면 등록된 사용자인지 확인한다. (사용자, Dataiku 로그인 ID)"""
    if _identify is None:
        return None, None
    login = _identify(dict(flask.request.headers))
    return auth.get_user_by_login(login), login


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
    items = [{"label": name, "value": key} for key, name, perm, _ in MENU if auth.can(user, perm)]
    scope = f" · {user['org_scope']}" if user["role"] == "org_manager" else ""
    return html.Div([
        dcc.Store(id="tick", data=0),
        html.Nav([html.Div("HR Learning Intelligence", className="brand"),
                  dcc.RadioItems(id="nav", options=items, value=items[0]["value"], className="navmenu"),
                  html.Div([html.Div(user["username"]), html.Div(f"{config.ROLES[user['role']]}{scope}"),
                            html.Button("로그아웃", id="logout-btn", n_clicks=0)], className="who")], className="side"),
        html.Main([filter_bar(), html.Div(id="period-note", className="note"), html.Div(id="page")]),
    ], className="shell")


def layout():
    return html.Div([dcc.Store(id="auth-tick", data=0), html.Div(id="root")])


@callback(Output("root", "children"), Input("auth-tick", "data"))
def render_root(_tick):
    user = current_user()
    return shell(user) if user else login_layout(flask.g.get("dss_login"))


@callback(Output("auth-tick", "data", allow_duplicate=True), Output("login-msg", "children", allow_duplicate=True),
          Input("login-dku", "n_clicks"), State("auth-tick", "data"), prevent_initial_call=True)
def dataiku_login(n, tick):
    if not n:
        raise PreventUpdate
    user, login = _try_dataiku_login()
    if user is None:
        db.log_access(login, "로그인 실패(Dataiku)")
        return no_update, ("Dataiku 로그인 상태를 확인하지 못했습니다. Dataiku에 먼저 로그인하세요." if not login else
                           f"Dataiku 사용자 '{login}'은(는) 등록되어 있지 않습니다.")
    _start_session(user, "Dataiku")
    return (tick or 0) + 1, ""


@callback(Output("login-msg", "children"), Input("login-send", "n_clicks"), State("login-email", "value"),
          prevent_initial_call=True)
def send_code(n, email):
    if not n:
        raise PreventUpdate
    return auth.request_code(email, auth.MAILER)


@callback(Output("auth-tick", "data"), Output("login-msg", "children", allow_duplicate=True),
          Input("login-btn", "n_clicks"), Input("login-code", "n_submit"),
          State("login-email", "value"), State("login-code", "value"), State("auth-tick", "data"),
          prevent_initial_call=True)
def do_login(n, s, email, code, tick):
    if not (n or s):
        raise PreventUpdate
    user = auth.verify_code(email, code)
    if not user:
        db.log_access(auth.norm(email), "로그인 실패")
        return no_update, "인증 코드가 올바르지 않거나 만료되었습니다. 코드를 다시 받아 주세요."
    _start_session(user, "이메일 인증")
    return (tick or 0) + 1, ""


@callback(Output("auth-tick", "data", allow_duplicate=True), Input("logout-btn", "n_clicks"),
          State("auth-tick", "data"), prevent_initial_call=True)
def do_logout(n, tick):
    if not n:
        raise PreventUpdate
    user = current_user()
    if user:
        log(user, "로그아웃")
    flask.session.clear()
    flask.session["no_auto"] = True  # 로그아웃 직후 Dataiku 계정으로 자동 재로그인하지 않는다
    return (tick or 0) + 1


@callback(Output("f-sub", "options"), Output("f-sub", "value"), Output("f-sub", "disabled"), Input("f-kind", "value"))
def sub_options(kind):
    opts = {"half": [("상반기", 1), ("하반기", 2)], "quarter": [(f"{q}분기", q) for q in range(1, 5)],
            "month": [(f"{m}월", m) for m in range(1, 13)]}.get(kind)
    if not opts:
        return [1], 1, True
    return [{"label": a, "value": b} for a, b in opts], 1, False


@callback(Output("page", "children"), Input("nav", "value"))
def route(key):
    """메뉴별 화면 틀만 그린다. 내용은 각 화면의 콜백이 공통 필터에 맞춰 채운다."""
    user = current_user()
    if not user:
        raise PreventUpdate
    for k, name, perm, builder in MENU:
        if k == key:
            if not auth.can(user, perm):
                return html.Div("이 메뉴를 볼 권한이 없습니다.", className="error")
            return [html.H2(name), builder(user)]
    return html.Div("없는 화면입니다.", className="error")


def _output_ids(body: dict) -> set[str]:
    found: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            if "id" in o and "property" in o:
                found.add(o["id"] if isinstance(o["id"], str) else "?")
            else:
                for v in o.values():
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(body.get("outputs"))
    return found


def attach(app: Dash, data_dir: str | None = None, mailer: auth.Mailer | None = None, https: bool | None = None,
           identify: auth.Identify | None | str = "auto") -> Dash:
    """주어진 Dash 앱에 화면·로그인·접근 통제를 붙인다. 로컬 실행과 Dataiku 웹앱이 함께 쓴다.

    identify: Dataiku 로그인 사용자 확인 함수. "auto"면 Dataiku 안에서만 켜진다. None이면 Dataiku 로그인을 쓰지 않는다.
    """
    global _identify
    if data_dir:
        config.configure(data_dir=data_dir)
    db.init_db()
    auth.MAILER = mailer or auth.mailer_from_env()
    _identify = auth.default_identify() if identify == "auto" else identify
    app.title = "HR Learning Intelligence"
    app.config.suppress_callback_exceptions = True
    app.index_string = app.index_string.replace("{%css%}", "{%css%}\n<style>" + CSS + "</style>")
    server = app.server
    server.secret_key = auth.secret()
    if https is None:
        https = os.environ.get("HRLI_HTTPS", "0") == "1"
    server.config.update(SESSION_COOKIE_NAME="hrli_session", SESSION_COOKIE_HTTPONLY=True,
                         SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(https),
                         PERMANENT_SESSION_LIFETIME=timedelta(hours=config.SESSION_HOURS),
                         MAX_CONTENT_LENGTH=50 * 1024 * 1024)
    app.layout = layout

    @server.before_request
    def guard():
        path = flask.request.path
        if any(x in path for x in SKIP_AUTO):
            return None
        user = current_user()
        if user:
            # 로그인 중에 사용자가 중지되거나 역할이 바뀌면 다음 요청부터 반영한다
            fresh = auth.get_user(user["username"])
            if fresh is None:
                flask.session.clear()
                user = None
            elif fresh != user:
                flask.session["user"] = fresh
        if user is None and not flask.session.get("no_auto"):
            found, login = _try_dataiku_login()
            flask.g.dss_login = login
            if found:
                _start_session(found, "Dataiku")
                user = found
        # 로그인 전에는 로그인 화면을 그리고 인증하는 콜백만 허용한다(각 데이터 콜백의 권한 검사에 더한 이중 방어).
        if user or not path.endswith("/_dash-update-component"):
            return None
        ids = _output_ids(flask.request.get_json(silent=True) or {})
        if ids and ids <= PUBLIC_OUTPUTS:
            return None
        return flask.abort(401)

    return app


def create_app() -> Dash:
    return attach(Dash(__name__, update_title=None))


if __name__ == "__main__":
    create_app().run(host=os.environ.get("HRLI_HOST", "127.0.0.1"), port=int(os.environ.get("HRLI_PORT", "8050")),
                     debug=False)
