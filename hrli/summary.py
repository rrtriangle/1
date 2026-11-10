"""분석 결과 요약(기획안 2.6-⑤, 8.2).

수치는 전부 Python(metrics/analysis)이 계산한다. 이 모듈은 그 집계 결과를 정해진 규칙으로 문장화한다.
사내에서 외부 AI 서비스를 쓸 수 없으므로 AI 호출은 넣지 않았다. build_payload가 만드는 집계 결과에는
개인 식별정보가 없으므로, 나중에 사내 승인된 AI가 생기면 이 payload를 입력으로 쓰면 된다.
"""
from __future__ import annotations

import json

import pandas as pd

PII_KEYS = {"emp_no", "name", "사번", "성명"}


def build_payload(label: str, kpi: dict, kpi_prev: dict | None, by_group: pd.DataFrame, by_job: pd.DataFrame,
                  field_share: pd.Series, basis_note: str) -> dict:
    """비식별 집계 결과만 담는다. 소수 집단(가려진 행)은 넣지 않는다."""
    def table(df: pd.DataFrame) -> list[dict]:
        d = df[~df["소수 집단"]].drop(columns="소수 집단").reset_index()
        return json.loads(d.to_json(orient="records", force_ascii=False))

    payload = {"기간": label, "분모 기준": basis_note, "전사 지표": kpi, "비교 기간 지표": kpi_prev,
               "그룹별": table(by_group), "직종별": table(by_job),
               "분야별 비중": {k: round(float(v), 4) for k, v in field_share.items()}}
    assert not PII_KEYS & set(json.dumps(payload, ensure_ascii=False).replace('"', " ").split()), "payload에 개인 식별 항목"
    return payload


def _pct(v) -> str:
    return "-" if v is None or pd.isna(v) else f"{v * 100:.1f}%"


def rule_summary(p: dict) -> str:
    k, prev = p["전사 지표"], p.get("비교 기간 지표")
    lines = [f"### {p['기간']} 교육 현황 요약",
             f"- 참여이력 {k['참여이력 건수']:,}건, 과정 운영 {k['과정 운영 횟수']:,}회, 참여 인원 {k['참여 인원']:,}명"
             f"(참여율 {_pct(k['참여율'])}, 미참여 {k['미참여 인원']:,}명).",
             f"- 총 교육 시간 {k['총 교육 시간']:,.1f}시간, 1인당 평균 {k['1인당 평균 교육 시간']:.1f}시간. "
             f"사외 비중 {_pct(k['사외 비중'])}, 온라인 비중 {_pct(k['온라인 비중'])}."]
    if prev and prev.get("참여이력 건수"):
        d = k["참여이력 건수"] - prev["참여이력 건수"]
        lines.append(f"- 비교 기간 대비 참여이력 {d:+,}건({d / prev['참여이력 건수'] * 100:+.1f}%), "
                     f"참여 인원 {k['참여 인원'] - prev['참여 인원']:+,}명"
                     + (f", 참여율 {(k['참여율'] - prev['참여율']) * 100:+.1f}%p." if k["참여율"] is not None
                        and prev.get("참여율") is not None else "."))
    fs = p["분야별 비중"]
    if fs:
        top = max(fs, key=fs.get)
        low = [f for f in ("소통", "문제해결", "리더십", "업무능력") if fs.get(f, 0) < 0.05]
        lines.append(f"- 분야별로는 {top} 비중이 {_pct(fs[top])}로 가장 크다."
                     + (f" {', '.join(low)} 분야는 각 5% 미만으로 공백 여부 검토가 필요하다." if low else ""))
        if fs.get("미분류", 0) > 0.1:
            lines.append(f"- 미분류 교육이 {_pct(fs['미분류'])}다. 분야 분석 전에 분류 확정이 필요하다.")
    for title, rows, key in (("그룹", p["그룹별"], "그룹·공장"), ("직종", p["직종별"], "직종")):
        rows = [r for r in rows if r.get("참여율") is not None]
        if len(rows) >= 2:
            hi, lo = max(rows, key=lambda r: r["참여율"]), min(rows, key=lambda r: r["참여율"])
            lines.append(f"- {title}별 참여율: 최고 {hi[key]}({_pct(hi['참여율'])}), 최저 {lo[key]}({_pct(lo['참여율'])}). "
                         f"격차 {(hi['참여율'] - lo['참여율']) * 100:.1f}%p.")
    lines.append(f"\n_분모 기준: {p['분모 기준']}. 이 요약은 Python 집계 결과를 규칙으로 정리한 것이며, "
                 "참여 현황은 교육 기회 검토용 참고 자료다._")
    return "\n".join(lines)
