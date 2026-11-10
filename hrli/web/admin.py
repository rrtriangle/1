"""데이터 관리 및 관리자 기능: 업로드·검토·승인·복구, 데이터 품질, 분류 기준, 교육 시간, 직원, 이력, 사용자."""
from __future__ import annotations

import base64
import json
from datetime import date

import pandas as pd
from dash import Input, Output, State, callback, ctx, dcc, html
from dash.exceptions import PreventUpdate

from .. import auth, classify, config, db, ingest
from .common import KO, require, section, table

CAT_KO = {"new": "신규", "changed": "변경", "duplicate": "중복(반영 안 함)", "suspect": "중복 의심(검토 필요)",
          "unmatched": "사번 미매칭", "error": "오류", "same": "변동 없음", "missing": "명부 미포함"}
STATUS_KO = {"staged": "검토 대기", "failed": "검증 실패", "approved": "반영 완료", "rejected": "반려",
             "rolled_back": "복구됨"}
KIND_KO = {"roster": "직원 명부", "training": "교육 이력"}
TYPE_KO = {"full": "전체 데이터", "incremental": "증분 데이터", "reprocess": "재처리 데이터"}
DEC_KO = {"accept": "반영", "update": "기존 이력 수정", "skip": "반영 안 함", "pending": "검토 필요"}


def skeleton(user):
    tabs = [("upload", "업로드·검토·승인"), ("quality", "데이터 품질"), ("classify", "교육 분류 기준"),
            ("time", "교육 시간·통계 제외"), ("employee", "직원 명부"), ("log", "업로드·수정 이력")]
    if auth.can(user, "users"):
        tabs.append(("users", "사용자"))
    return html.Div([dcc.Tabs(id="adm-tab", value="upload", children=[dcc.Tab(label=n, value=v) for v, n in tabs]),
                     html.Div(id="adm-msg", className="msg"), dcc.Loading(html.Div(id="adm-body"))])


def _batch_table() -> pd.DataFrame:
    b = ingest.batches()
    if b.empty:
        return b

    def counts(s):
        d = json.loads(s) if s else {}
        return ", ".join(f"{CAT_KO[k]} {v}" for k, v in d.items() if k in CAT_KO)

    return pd.DataFrame({"배치": b["batch_id"], "종류": b["kind"].map(KIND_KO), "유형": b["file_type"].map(TYPE_KO),
                         "파일명": b["filename"], "기준월": b["snapshot_month"], "업로드": b["uploaded_by"],
                         "업로드 일시": b["uploaded_at"], "상태": b["status"].map(STATUS_KO), "건수": b["summary"].map(counts),
                         "처리자": b["decided_by"], "비고": b["notes"]})


@callback(Output("adm-body", "children"), Input("adm-tab", "value"), Input("tick", "data"))
def body(tab, _tick):
    user = require("manage")
    if tab == "upload":
        b = ingest.batches()
        opts = [{"label": f"#{r.batch_id} {KIND_KO[r.kind]} · {r.filename} · {STATUS_KO[r.status]}", "value": r.batch_id}
                for r in b.itertuples()]
        staged = b.loc[b["status"] == "staged", "batch_id"]
        return [
            section("① 월별 데이터 업로드",
                    html.Div([
                        html.Label(["데이터 종류", dcc.Dropdown(id="up-kind", clearable=False, value="training", options=[
                            {"label": v, "value": k} for k, v in KIND_KO.items()])]),
                        html.Label(["파일 유형", dcc.Dropdown(id="up-type", clearable=False, value="full", options=[
                            {"label": v, "value": k} for k, v in TYPE_KO.items()])]),
                        html.Label(["명부 기준월(YYYY-MM)", dcc.Input(id="up-month", value=f"{date.today():%Y-%m}")]),
                        dcc.Upload(html.Button("Excel/CSV 파일 선택", className="primary"), id="up-file", multiple=False),
                    ], className="controls"),
                    note="업로드하면 원본을 보관하고 기존 데이터와 비교만 한다. 아래에서 검토·승인하기 전에는 통계에 반영되지 않는다. "
                         "파일에 없는 기존 직원·이력은 삭제하지 않는다."),
            section("② 배치 검토·승인·복구",
                    dcc.Dropdown(id="bt-pick", options=opts, value=int(staged.iloc[0]) if len(staged) else None,
                                 placeholder="배치를 선택하세요"),
                    html.Div(id="bt-detail")),
            section("업로드 이력", table(_batch_table(), page_size=10)),
        ]
    if tab == "quality":
        q = lambda sql: int(db.read(sql).iat[0, 0])  # noqa: E731
        rows = [
            ("전체 교육 이력 건수", q("SELECT COUNT(*) FROM training_record")),
            ("통계 반영 건수(매칭·미제외)", q("SELECT COUNT(*) FROM training_record r JOIN employee e ON e.emp_no=r.emp_no"
                                    " AND e.in_scope=1 WHERE r.excluded=0")),
            ("사번 미매칭 건수", q("SELECT COUNT(*) FROM training_record r WHERE NOT EXISTS"
                            " (SELECT 1 FROM employee e WHERE e.emp_no=r.emp_no)")),
            ("통계 제외 건수", q("SELECT COUNT(*) FROM training_record WHERE excluded=1")),
            ("분류 미완료 과정 수(분야 미분류)", q("SELECT COUNT(*) FROM course_master WHERE field='미분류'")),
            ("담당자 미확정 과정 수", q("SELECT COUNT(*) FROM course_master WHERE confirmed=0")),
            ("교육 시간 이상 건수(누락·0·과다)", q(f"SELECT COUNT(*) FROM training_record WHERE hours_raw IS NULL OR hours_raw<=0"
                                        f" OR hours_raw>{config.MAX_REASONABLE_HOURS}")),
            ("검토 대기 배치 수", q("SELECT COUNT(*) FROM upload_batch WHERE status='staged'")),
            ("검토 대기 중복 의심 건수", q("SELECT COUNT(*) FROM staging_row s JOIN upload_batch b USING(batch_id)"
                                 " WHERE b.status='staged' AND s.decision='pending'")),
            ("최근 업로드의 오류 건수", q("SELECT COUNT(*) FROM staging_row WHERE category='error' AND batch_id="
                                "(SELECT MAX(batch_id) FROM upload_batch)")),
            ("명부 미포함 확인 대상 직원 수", q("SELECT COUNT(*) FROM employee WHERE roster_state='명부미포함'")),
        ]
        unmatched = db.read("SELECT r.emp_no AS 사번, COUNT(*) AS 건수, MIN(r.start_date) AS 최초, MAX(r.start_date) AS 최근"
                            " FROM training_record r WHERE NOT EXISTS (SELECT 1 FROM employee e WHERE e.emp_no=r.emp_no)"
                            " GROUP BY r.emp_no")
        odd = db.read("SELECT record_id AS 이력ID, emp_no AS 사번, course_name_raw AS 교육명, start_date AS 시작일,"
                      " hours_raw AS 원본시간 FROM training_record WHERE hours_raw IS NULL OR hours_raw<=0 OR hours_raw>?"
                      " ORDER BY start_date DESC", (config.MAX_REASONABLE_HOURS,))
        return [section("데이터 품질 현황", table(pd.DataFrame(rows, columns=["항목", "건수"]), page_size=20,
                                           sort_action="none", filter_action="none")),
                section("사번 미매칭 이력", table(unmatched), note="명부에 해당 사번이 올라오면 자동으로 통계에 포함된다."),
                section("교육 시간 이상 이력", table(odd, page_size=8),
                        note="'교육 시간·통계 제외' 탭에서 분석용 시간을 보정하거나 시간 통계에서 제외할 수 있다.")]
    if tab == "classify":
        cm = db.read("SELECT course_name_raw, std_name, field, operation, method, target, confirmed, rule_note"
                     " FROM course_master ORDER BY confirmed, field='미분류' DESC, course_name_raw")
        dd = lambda vals: {"options": [{"label": v, "value": v} for v in vals]}  # noqa: E731
        cols = [{"name": "원본 교육명(수정 불가)", "id": "course_name_raw", "editable": False},
                {"name": "표준 교육명", "id": "std_name"},
                {"name": "교육 분야", "id": "field", "presentation": "dropdown"},
                {"name": "운영 구분", "id": "operation", "presentation": "dropdown"},
                {"name": "교육 방식", "id": "method", "presentation": "dropdown"},
                {"name": "교육 대상", "id": "target"},
                {"name": "확정(1/0)", "id": "confirmed", "type": "numeric"},
                {"name": "자동 분류 근거", "id": "rule_note", "editable": False}]
        inst = db.read("SELECT institution_raw, institution_std FROM institution_master ORDER BY institution_raw")
        sim = classify.similar_courses(cm["std_name"].tolist())
        return [
            section("교육 과정 기준정보", table(
                cm, id="cm-table", page_size=15, columns=cols, editable=True,
                dropdown={"field": dd(config.FIELDS), "operation": dd(config.OPERATIONS), "method": dd(config.METHODS)},
                css=[{"selector": ".Select-menu-outer", "rule": "display: block !important"}]),
                html.Div([html.Label(["변경 사유", dcc.Input(id="cm-reason", placeholder="예: 담당자 검토")]),
                          html.Button("변경 내용 저장", id="cm-save", n_clicks=0, className="primary")], className="controls"),
                note="자동 분류는 제안이다. 표에서 직접 고친 뒤 저장하면 변경 이력이 남는다. 서로 다른 원본 교육명에 같은 표준 교육명을 "
                     "주면 하나의 과정으로 집계된다(원본 교육명은 그대로 보존)."),
            section("표준화 후보(교육명 유사도)", table(sim, page_size=8),
                    note="비슷하다는 이유만으로 자동 통합하지 않는다. 같은 과정이라고 판단하면 위 표에서 표준 교육명을 맞춘다."),
            section("교육기관 표준명", table(inst, id="inst-table", page_size=8, editable=True, columns=[
                {"name": "원본 교육기관(수정 불가)", "id": "institution_raw", "editable": False},
                {"name": "표준 교육기관", "id": "institution_std"}]),
                html.Button("교육기관 변경 저장", id="inst-save", n_clicks=0)),
        ]
    if tab == "time":
        rules = db.read("SELECT rule_id AS 규칙ID, scope AS 범위, value AS 값, reason AS 사유, created_by AS 등록자,"
                        " created_at AS 등록일시 FROM time_rule")
        return [
            section("시간 통계 제외 규칙", table(rules, page_size=6),
                    html.Div([
                        html.Label(["범위", dcc.Dropdown(id="tr-scope", clearable=False, value="field", options=[
                            {"label": "교육 분야", "value": "field"}, {"label": "표준 교육기관", "value": "institution"},
                            {"label": "표준 교육명", "value": "course"}])]),
                        html.Label(["값", dcc.Input(id="tr-value", placeholder="예: 어학")]),
                        html.Label(["사유", dcc.Input(id="tr-reason")]),
                        html.Button("규칙 추가", id="tr-add", n_clicks=0, className="primary"),
                        html.Label(["삭제할 규칙ID", dcc.Input(id="tr-del-id", type="number")]),
                        html.Button("규칙 삭제", id="tr-del", n_clicks=0),
                    ], className="controls"),
                    note="규칙에 해당하는 교육은 참여 건수·인원에는 포함되고 총 교육 시간에서만 빠진다. 원본 시간은 그대로 보존된다."),
            section("개별 이력 수정",
                    html.Div([html.Label(["사번으로 이력 찾기", dcc.Input(id="rec-emp", debounce=True)])], className="controls"),
                    html.Div(id="rec-list"),
                    html.Div([
                        html.Label(["이력ID", dcc.Input(id="rec-id", type="number")]),
                        html.Label(["수정 항목", dcc.Dropdown(id="rec-col", clearable=False, value="hours_adj", options=[
                            {"label": "분석용 교육 시간(보정)", "value": "hours_adj"},
                            {"label": "시간 통계 포함(1/0)", "value": "hours_included"},
                            {"label": "통계 제외(1/0)", "value": "excluded"},
                            {"label": "교육 시작일", "value": "start_date"}, {"label": "교육 종료일", "value": "end_date"},
                            {"label": "교육기관(원본)", "value": "institution_raw"},
                            {"label": "수료 여부", "value": "completion"}])]),
                        html.Label(["새 값", dcc.Input(id="rec-val")]),
                        html.Label(["사유(필수)", dcc.Input(id="rec-reason")]),
                        html.Button("수정 저장", id="rec-save", n_clicks=0, className="primary"),
                    ], className="controls"),
                    note="잘못된 이력은 삭제하지 않고 '통계 제외'로 처리한다. 수정 전 값과 사유는 변경 이력에 남는다."),
        ]
    if tab == "employee":
        e = db.read("SELECT emp_no AS 사번, name AS 성명, status AS 재직상태, grp AS 그룹, team AS 팀, job_family AS 직종,"
                    " in_scope AS 분석대상, roster_state AS 명부상태, last_seen_month AS 마지막확인월 FROM employee"
                    " ORDER BY roster_state='명부미포함' DESC, emp_no")
        return [section("직원 명부 관리", table(e, page_size=12),
                        html.Div([
                            html.Label(["사번", dcc.Input(id="emp-no")]),
                            html.Label(["수정 항목", dcc.Dropdown(id="emp-col", clearable=False, value="in_scope", options=[
                                {"label": "분석 대상 여부(1/0)", "value": "in_scope"},
                                {"label": "재직 상태", "value": "status"},
                                {"label": "명부 상태(명부확인/명부미포함)", "value": "roster_state"}])]),
                            html.Label(["새 값", dcc.Input(id="emp-val")]),
                            html.Label(["사유(필수)", dcc.Input(id="emp-reason")]),
                            html.Button("수정 저장", id="emp-save", n_clicks=0, className="primary"),
                        ], className="controls"),
                        note="'명부미포함'은 최근 전체 명부에 없던 직원이다. 휴직·추출 조건 등 다른 사유일 수 있어 자동으로 퇴직 처리하지 "
                             "않는다. 확인 후 재직 상태나 분석 대상 여부를 직접 정한다.")]
    if tab == "log":
        ch = db.read("SELECT ts AS 일시, username AS 사용자, op AS 작업, tbl AS 대상, row_key AS 키, col AS 항목,"
                     " old_value AS 이전값, new_value AS 새값, reason AS 사유, batch_id AS 배치 FROM change_log"
                     " ORDER BY log_id DESC LIMIT 500")
        ac = db.read("SELECT ts AS 일시, username AS 사용자, action AS 작업, detail AS 내용 FROM access_log"
                     " ORDER BY log_id DESC LIMIT 500")
        return [section("업로드 이력", table(_batch_table(), page_size=10)),
                section("데이터 수정 이력(최근 500건)", table(ch, page_size=12)),
                section("접근·다운로드 이력(최근 500건)", table(ac, page_size=12))]
    if tab == "users" and auth.can(user, "users"):
        u = db.read("SELECT username AS 아이디, role AS 역할, org_scope AS 조회범위, active AS 사용 FROM users")
        u["역할"] = u["역할"].map(config.ROLES)
        return [section("사용자 관리", table(u),
                        html.Div([
                            html.Label(["아이디", dcc.Input(id="usr-name", autoComplete="off")]),
                            html.Label(["비밀번호(8자 이상)", dcc.Input(id="usr-pw", type="password", autoComplete="new-password")]),
                            html.Label(["역할", dcc.Dropdown(id="usr-role", clearable=False, value="analyst", options=[
                                {"label": v, "value": k} for k, v in config.ROLES.items()])]),
                            html.Label(["조회 범위(조직 관리자: 그룹·공장명)", dcc.Input(id="usr-scope")]),
                            html.Button("계정 생성·갱신", id="usr-save", n_clicks=0, className="primary"),
                        ], className="controls"),
                        note="같은 아이디로 저장하면 비밀번호와 역할이 바뀐다.")]
    raise PreventUpdate


# ---------------------------------------------------------------- 업로드·배치

@callback(Output("adm-msg", "children"), Output("tick", "data"), Input("up-file", "contents"),
          State("up-file", "filename"), State("up-kind", "value"), State("up-type", "value"), State("up-month", "value"),
          State("tick", "data"), prevent_initial_call=True)
def upload(contents, filename, kind, ftype, month, tick):
    user = require("manage")
    if not contents:
        raise PreventUpdate
    data = base64.b64decode(contents.split(",", 1)[1])
    bid = ingest.stage(data, filename, kind, ftype, user["username"], month if kind == "roster" else None)
    b = ingest.batches().set_index("batch_id").loc[bid]
    if b["status"] == "failed":
        return f"배치 #{bid} 검증 실패(원본은 보관됨): {b['notes']}", (tick or 0) + 1
    return f"배치 #{bid} 업로드 완료. 아래에서 비교 결과를 검토한 뒤 승인하세요.", (tick or 0) + 1


@callback(Output("bt-detail", "children"), Input("bt-pick", "value"))
def batch_detail(bid):
    require("manage")
    if not bid:
        return None
    b = ingest.batches().set_index("batch_id").loc[bid]
    s = ingest.staging(bid)
    summary = json.loads(b["summary"]) if b["summary"] else {}
    head = html.P(f"{KIND_KO[b['kind']]} · {TYPE_KO[b['file_type']]} · {b['filename']} · 상태: {STATUS_KO[b['status']]}"
                  + (f" · 기준월 {b['snapshot_month']}" if pd.notna(b["snapshot_month"]) else "")
                  + (f" · {b['notes']}" if pd.notna(b["notes"]) else ""))
    if s.empty:
        return [head]
    counts = s["category"].map(CAT_KO).value_counts().rename_axis("구분").rename("건수").to_frame()
    payload = pd.json_normalize(s["payload"].map(json.loads).tolist())
    rows = pd.concat([pd.DataFrame({"행": s["row_no"], "구분": s["category"].map(CAT_KO), "처리": s["decision"].map(DEC_KO),
                                    "기존 키": s["target_id"], "확인 사항": s["messages"]}), payload], axis=1)
    order = {CAT_KO[k]: i for i, k in enumerate(["suspect", "error", "unmatched", "missing", "changed", "duplicate", "new"])}
    review = rows[rows["구분"] != CAT_KO["same"]].sort_values("구분", key=lambda x: x.map(order), kind="stable")
    out = [head, table(counts, sort_action="none", filter_action="none"),
           html.P(f"원본 {summary.get('total', len(s)):,}행 · 인식한 표준 항목: {', '.join(KO.get(x, x) for x in summary.get('present_cols', []))}",
                  className="note"),
           html.H4("검토 대상(변동 없는 행 제외)"),
           table(review, id="bt-rows", page_size=12, row_selectable="multi", selected_rows=[])]
    if b["status"] == "staged":
        out.append(html.Div([
            html.Span("선택한 행을: "), html.Button("신규로 반영", id="bt-accept", n_clicks=0),
            html.Button("기존 이력 수정으로 반영", id="bt-update", n_clicks=0),
            html.Button("반영 안 함", id="bt-skip", n_clicks=0),
            html.Span(" | 배치: "), html.Button("승인하고 최종 반영", id="bt-approve", n_clicks=0, className="primary"),
            html.Button("반려(반영하지 않음)", id="bt-reject", n_clicks=0),
        ], className="controls"))
        out.append(html.P("'중복 의심' 건은 수정인지 별개 이력인지 정해야 승인할 수 있다. 오류 행은 반영되지 않는다.", className="note"))
    elif b["status"] == "approved":
        out.append(html.Div([html.Button("이 배치 반영 내용 복구(롤백)", id="bt-rollback", n_clicks=0)], className="controls"))
        out.append(html.P("복구하면 이 배치로 추가·변경된 내용이 이전 상태로 돌아간다. 원본 파일과 이력 기록은 남는다.", className="note"))
    return out


def _batch_action(fn, tick):
    try:
        return fn(), (tick or 0) + 1
    except (ValueError, KeyError) as e:
        return f"처리하지 못했습니다: {e}", tick


@callback(Output("adm-msg", "children", allow_duplicate=True), Output("tick", "data", allow_duplicate=True),
          Input("bt-accept", "n_clicks"), Input("bt-update", "n_clicks"), Input("bt-skip", "n_clicks"),
          Input("bt-approve", "n_clicks"), Input("bt-reject", "n_clicks"),
          State("bt-pick", "value"), State("bt-rows", "selected_rows"), State("bt-rows", "derived_virtual_data"),
          State("tick", "data"), prevent_initial_call=True)
def batch_staged_action(a, u, s, ap, rj, bid, sel, rows, tick):
    user = require("manage")
    trig = ctx.triggered_id
    if not bid or not any([a, u, s, ap, rj]):
        raise PreventUpdate
    name = user["username"]
    if trig == "bt-approve":
        return _batch_action(lambda: f"배치 #{bid} 반영 완료: {ingest.approve(bid, name)}", tick)
    if trig == "bt-reject":
        return _batch_action(lambda: ingest.reject(bid, name, "담당자 반려") or f"배치 #{bid}를 반려했습니다.", tick)
    if not sel:
        return "먼저 표에서 행을 선택하세요.", tick
    nos = [int(rows[i]["행"]) for i in sel if i < len(rows)]
    dec = {"bt-accept": "accept", "bt-update": "update", "bt-skip": "skip"}[trig]
    return _batch_action(lambda: ingest.set_decision(bid, nos, dec) or f"{len(nos)}건을 '{DEC_KO[dec]}'(으)로 정했습니다.", tick)


@callback(Output("adm-msg", "children", allow_duplicate=True), Output("tick", "data", allow_duplicate=True),
          Input("bt-rollback", "n_clicks"), State("bt-pick", "value"), State("tick", "data"), prevent_initial_call=True)
def batch_rollback(n, bid, tick):
    user = require("manage")
    if not n or not bid:
        raise PreventUpdate

    def run():
        r = ingest.rollback(bid, user["username"])
        msg = f"배치 #{bid} 복구 완료: 되돌린 수정 {r['reverted']}건, 제거 {r['removed']}건."
        return msg + (f" 이후 수정과 겹쳐 유지한 항목 {len(r['conflicts'])}건." if r["conflicts"] else "")
    return _batch_action(run, tick)


# ---------------------------------------------------------------- 기준정보·시간·직원·사용자

def _save_table(tbl: str, key: str, rows: list[dict], cols: list[str], user: str, reason: str | None) -> int:
    cur = db.read(f"SELECT * FROM {tbl}").set_index(key)
    n = 0
    for r in rows or []:
        if r[key] not in cur.index:
            continue
        for c in cols:
            new = r.get(c)
            new = None if new in ("", None) else (int(new) if c == "confirmed" else str(new).strip())
            old = cur.at[r[key], c]
            old = None if pd.isna(old) else old
            if new != old and not (c != "target" and new is None):  # 분류 값은 비울 수 없다
                db.update_field(tbl, r[key], c, new, user, reason or "담당자 수정")
                n += 1
    return n


@callback(Output("adm-msg", "children", allow_duplicate=True), Output("tick", "data", allow_duplicate=True),
          Input("cm-save", "n_clicks"), Input("inst-save", "n_clicks"), State("cm-table", "data"),
          State("inst-table", "data"), State("cm-reason", "value"), State("tick", "data"), prevent_initial_call=True)
def save_masters(n1, n2, cm, inst, reason, tick):
    user = require("manage")
    if not (n1 or n2):
        raise PreventUpdate
    if ctx.triggered_id == "cm-save":
        bad = [r["course_name_raw"] for r in cm if r.get("field") not in config.FIELDS
               or r.get("operation") not in config.OPERATIONS or r.get("method") not in config.METHODS
               or str(r.get("confirmed")) not in ("0", "1")]
        if bad:
            return f"분류 값이 올바르지 않은 행이 있습니다: {bad[:3]}", tick
        n = _save_table("course_master", "course_name_raw", cm, ["std_name", "field", "operation", "method", "target",
                                                                 "confirmed"], user["username"], reason)
    else:
        n = _save_table("institution_master", "institution_raw", inst, ["institution_std"], user["username"], reason)
    return f"{n}개 항목을 저장했습니다.", (tick or 0) + 1


@callback(Output("adm-msg", "children", allow_duplicate=True), Output("tick", "data", allow_duplicate=True),
          Input("tr-add", "n_clicks"), Input("tr-del", "n_clicks"), State("tr-scope", "value"), State("tr-value", "value"),
          State("tr-reason", "value"), State("tr-del-id", "value"), State("tick", "data"), prevent_initial_call=True)
def time_rules(a, d, scope, value, reason, del_id, tick):
    user = require("manage")
    if not (a or d):
        raise PreventUpdate
    with db.tx() as con:
        if ctx.triggered_id == "tr-add":
            if not value or not reason:
                return "값과 사유를 입력하세요.", tick
            cur = con.execute("INSERT INTO time_rule (scope, value, reason, created_by, created_at) VALUES (?,?,?,?,?)",
                              (scope, value.strip(), reason, user["username"], db.now()))
            db.log_change(con, user["username"], "insert", "time_rule", cur.lastrowid, scope, None, value, reason)
            return "시간 통계 제외 규칙을 추가했습니다.", (tick or 0) + 1
        row = con.execute("SELECT * FROM time_rule WHERE rule_id=?", (del_id,)).fetchone()
        if not row:
            return "해당 규칙ID가 없습니다.", tick
        con.execute("DELETE FROM time_rule WHERE rule_id=?", (del_id,))
        db.log_change(con, user["username"], "delete", "time_rule", del_id, row["scope"], row["value"], None, "규칙 삭제")
    return "규칙을 삭제했습니다.", (tick or 0) + 1


@callback(Output("rec-list", "children"), Input("rec-emp", "value"), Input("tick", "data"))
def record_list(emp_no, _t):
    require("manage")
    if not emp_no:
        return None
    return table(db.read(
        "SELECT record_id AS 이력ID, course_name_raw AS 교육명, start_date AS 시작일, end_date AS 종료일, hours_raw AS 원본시간,"
        " hours_adj AS 보정시간, hours_included AS 시간포함, excluded AS 통계제외, institution_raw AS 교육기관, completion AS 수료"
        " FROM training_record WHERE emp_no=? ORDER BY start_date DESC", (emp_no.strip(),)), page_size=8)


def _edit(tbl, key, col, val, reason, user, tick, numeric=(), flags=(), dates=()):
    if not key or not reason:
        return "대상과 사유를 입력하세요.", tick
    try:
        v = None if val in (None, "") else val.strip()
        if col in flags:
            if v not in ("0", "1"):
                return "1 또는 0을 입력하세요.", tick
            v = int(v)
        elif col in numeric and v is not None:
            v = float(v)
            if v < 0:
                return "교육 시간은 0 이상이어야 합니다.", tick
        elif col in dates and v is not None:
            v = date.fromisoformat(v).isoformat()
        elif col == "roster_state" and v not in ("명부확인", "명부미포함"):
            return "명부확인 또는 명부미포함을 입력하세요.", tick
        db.update_field(tbl, key, col, v, user["username"], reason)
    except (ValueError, KeyError) as e:
        return f"저장하지 못했습니다: {e}", tick
    return "저장했습니다. 변경 이력에 기록되었습니다.", (tick or 0) + 1


@callback(Output("adm-msg", "children", allow_duplicate=True), Output("tick", "data", allow_duplicate=True),
          Input("rec-save", "n_clicks"), State("rec-id", "value"), State("rec-col", "value"), State("rec-val", "value"),
          State("rec-reason", "value"), State("tick", "data"), prevent_initial_call=True)
def record_save(n, rid, col, val, reason, tick):
    user = require("manage")
    if not n:
        raise PreventUpdate
    return _edit("training_record", rid, col, val, reason, user, tick, numeric=("hours_adj",),
                 flags=("hours_included", "excluded"), dates=("start_date", "end_date"))


@callback(Output("adm-msg", "children", allow_duplicate=True), Output("tick", "data", allow_duplicate=True),
          Input("emp-save", "n_clicks"), State("emp-no", "value"), State("emp-col", "value"), State("emp-val", "value"),
          State("emp-reason", "value"), State("tick", "data"), prevent_initial_call=True)
def employee_save(n, emp_no, col, val, reason, tick):
    user = require("manage")
    if not n:
        raise PreventUpdate
    return _edit("employee", (emp_no or "").strip(), col, val, reason, user, tick, flags=("in_scope",))


@callback(Output("adm-msg", "children", allow_duplicate=True), Output("tick", "data", allow_duplicate=True),
          Input("usr-save", "n_clicks"), State("usr-name", "value"), State("usr-pw", "value"), State("usr-role", "value"),
          State("usr-scope", "value"), State("tick", "data"), prevent_initial_call=True)
def user_save(n, name, pw, role, scope, tick):
    admin_user = require("users")
    if not n:
        raise PreventUpdate
    try:
        auth.create_user((name or "").strip(), pw or "", role, (scope or "").strip() or None)
    except ValueError as e:
        return f"저장하지 못했습니다: {e}", tick
    db.log_access(admin_user["username"], "사용자 계정 생성·갱신", f"{name} ({role})")
    return f"계정 '{name}'을 저장했습니다.", (tick or 0) + 1
