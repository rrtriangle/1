"""Plotly 차트 공통 스타일. 색은 역할로 정한다: 범주(고정 순서), 크기(단일 색 농도), 미분류(회색)."""
from __future__ import annotations

import networkx as nx
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from .. import config

INK, INK2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
GRAY = "#b5b3ab"
SEQ = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]
# 색은 순위가 아니라 항목을 따라간다: 분야·운영 구분마다 고정 색
FIELD_COLOR = {f: c for f, c in zip([f for f in config.FIELDS if f != "미분류"], SERIES)} | {"미분류": GRAY}
OP_COLOR = {"사내": SERIES[0], "사외": SERIES[1], "온라인": SERIES[0], "오프라인": SERIES[1], "혼합": SERIES[2],
            "미분류": GRAY}


def style(fig: go.Figure, title: str, height: int = 320, legend: bool = True) -> go.Figure:
    fig.update_layout(
        title=dict(text=title, font=dict(size=14, color=INK), x=0, xanchor="left"),
        height=height, margin=dict(l=8, r=8, t=44, b=8), paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(family="Pretendard, 'Noto Sans KR', 'Malgun Gothic', sans-serif", size=12, color=INK2),
        showlegend=legend, legend=dict(orientation="h", y=-0.18, x=0, title_text=""), bargap=0.35,
        hoverlabel=dict(bgcolor="white", font_color=INK), colorway=SERIES)
    fig.update_xaxes(showgrid=False, linecolor=AXIS, tickfont_color=MUTED, title_font_color=MUTED, automargin=True)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=SURFACE, tickfont_color=MUTED, title_font_color=MUTED,
                     automargin=True)
    return fig


def empty(title: str, msg: str = "표시할 데이터가 없습니다") -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=msg, showarrow=False, font=dict(color=MUTED, size=13))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return style(fig, title, 220, False)


def bar(s: pd.Series, title: str, pct: bool = False, horizontal: bool = True, color: str = SERIES[0],
        height: int | None = None) -> go.Figure:
    """단일 지표 막대. 한 계열이므로 범례 없이 값 라벨을 붙인다."""
    s = s.dropna()
    if s.empty:
        return empty(title)
    fmt = ".1%" if pct else ",.1f" if s.dtype.kind == "f" else ","
    if horizontal:
        s = s.iloc[::-1]
        fig = go.Figure(go.Bar(x=s.values, y=s.index.astype(str), orientation="h", marker_color=color,
                               texttemplate=f"%{{x:{fmt}}}", textposition="outside", cliponaxis=False,
                               hovertemplate=f"%{{y}}: %{{x:{fmt}}}<extra></extra>"))
        fig.update_xaxes(tickformat=".0%" if pct else None, showgrid=True, gridcolor=GRID)
        fig.update_yaxes(showgrid=False)
        return style(fig, title, height or max(220, 28 * len(s) + 70), False)
    fig = go.Figure(go.Bar(x=s.index.astype(str), y=s.values, marker_color=color, texttemplate=f"%{{y:{fmt}}}",
                           textposition="outside", cliponaxis=False,
                           hovertemplate=f"%{{x}}: %{{y:{fmt}}}<extra></extra>"))
    fig.update_yaxes(tickformat=".0%" if pct else None)
    fig.update_xaxes(type="category")
    return style(fig, title, height or 300, False)


def stacked(df: pd.DataFrame, title: str, colors: dict, pct: bool = False) -> go.Figure:
    """행=x축 범주, 열=계열인 누적 막대. pct=True면 행별 100% 구성비."""
    if df.empty:
        return empty(title)
    d = df.div(df.sum(axis=1), axis=0) if pct else df
    fig = go.Figure()
    for c in d.columns:
        fig.add_bar(x=d.index.astype(str), y=d[c], name=str(c), marker_color=colors.get(c, GRAY),
                    marker_line=dict(color=SURFACE, width=2),
                    hovertemplate=f"%{{x}} · {c}: %{{y:{'.1%' if pct else ','}}}<extra></extra>")
    fig.update_layout(barmode="stack")
    fig.update_yaxes(tickformat=".0%" if pct else None)
    fig.update_xaxes(type="category")
    return style(fig, title)


def lines(df: pd.DataFrame, title: str, pct: bool = False) -> go.Figure:
    if df.empty:
        return empty(title)
    fig = go.Figure()
    for i, c in enumerate(df.columns):
        fig.add_scatter(x=df.index.astype(str), y=df[c], name=str(c), mode="lines+markers",
                        line=dict(width=2, color=SERIES[i % len(SERIES)]), marker=dict(size=8))
    fig.update_layout(hovermode="x unified")
    fig.update_yaxes(tickformat=".0%" if pct else ",", rangemode="tozero")
    fig.update_xaxes(type="category")
    return style(fig, title, legend=len(df.columns) > 1)


def share_bar(s: pd.Series, title: str, colors: dict) -> go.Figure:
    """구성비를 한 줄 누적 막대로(파이 대신). 조각마다 이름과 비율을 직접 표시한다."""
    s = s[s > 0]
    if s.empty:
        return empty(title)
    fig = go.Figure()
    for k, v in s.items():
        fig.add_bar(x=[v], y=[""], orientation="h", name=str(k), marker_color=colors.get(k, GRAY),
                    marker_line=dict(color=SURFACE, width=2), text=f"{k} {v:.0%}" if v >= 0.08 else "",
                    textposition="inside", insidetextanchor="middle",
                    textfont_color=INK if colors.get(k) in ("#eda100", GRAY) else "white",
                    hovertemplate=f"{k}: %{{x:.1%}}<extra></extra>")
    fig.update_layout(barmode="stack")
    fig.update_xaxes(visible=False, range=[0, 1])
    fig.update_yaxes(visible=False)
    return style(fig, title, 150)


def heatmap(m: pd.DataFrame, title: str) -> go.Figure:
    if m.empty:
        return empty(title)
    fig = px.imshow(m, color_continuous_scale=SEQ, zmin=0, zmax=max(0.01, float(m.values.max())), aspect="auto",
                    text_auto=".0%")
    fig.update_traces(xgap=2, ygap=2, hovertemplate="%{y} · %{x}: %{z:.1%}<extra></extra>")
    fig.update_coloraxes(colorbar=dict(tickformat=".0%", thickness=10, title=""))
    fig.update_xaxes(side="top", title="")
    fig.update_yaxes(title="")
    return style(fig, title, max(260, 30 * len(m) + 110), False)


def clusters(df: pd.DataFrame, title: str) -> go.Figure:
    if df.empty:
        return empty(title, "군집을 만들 만큼 집단 수가 충분하지 않습니다")
    fig = go.Figure()
    for i, (name, g) in enumerate(df.groupby("군집")):
        fig.add_scatter(x=g["x"], y=g["y"], mode="markers+text", name=name, text=g.index, textposition="top center",
                        marker=dict(size=12, color=SERIES[i], line=dict(color=SURFACE, width=2)),
                        hovertemplate="%{text}<extra>" + name + "</extra>")
    fig.update_xaxes(showticklabels=False, title="주성분 1")
    fig.update_yaxes(showticklabels=False, title="주성분 2")
    return style(fig, title, 420)


def network(fact: pd.DataFrame, by: str, title: str, top: int = 12) -> go.Figure:
    """조직-교육 과정 이분 그래프. 선 굵기는 참여이력 건수."""
    if fact.empty:
        return empty(title)
    top_courses = fact["std_name"].value_counts().head(top).index
    e = fact[fact["std_name"].isin(top_courses)].groupby([by, "std_name"]).size().reset_index(name="n")
    e = e[e["n"] >= max(2, e["n"].quantile(0.4))]
    if e.empty:
        return empty(title)
    g = nx.Graph()
    for r in e.itertuples(index=False):
        g.add_edge(("org", r[0]), ("course", r[1]), weight=r[2])
    pos = nx.spring_layout(g, seed=3, k=1.2, weight="weight")
    fig, mx = go.Figure(), e["n"].max()
    for a, b, d in g.edges(data=True):
        fig.add_scatter(x=[pos[a][0], pos[b][0]], y=[pos[a][1], pos[b][1]], mode="lines", hoverinfo="skip",
                        line=dict(width=0.5 + 4 * d["weight"] / mx, color=GRID), showlegend=False)
    for kind, name, color in (("org", "조직", SERIES[0]), ("course", "교육 과정", SERIES[1])):
        nodes = [n for n in g.nodes if n[0] == kind]
        fig.add_scatter(x=[pos[n][0] for n in nodes], y=[pos[n][1] for n in nodes], mode="markers+text", name=name,
                        text=[n[1] for n in nodes], textposition="top center", textfont=dict(size=10, color=INK2),
                        marker=dict(size=[8 + 2 * g.degree(n) for n in nodes], color=color,
                                    line=dict(color=SURFACE, width=2)),
                        hovertemplate="%{text}<extra></extra>")
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return style(fig, title, 520)
