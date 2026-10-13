"""과거 시장 투자 게임.

규칙
- 사용자는 게임 날짜 D 의 장 마감 시점에 있다. 볼 수 있는 것은 D 이하의 차트·지표·종목 목록뿐이다.
- 주문은 다음 거래일 시가에 체결된다(백테스트와 같은 Portfolio 규칙: 비용·거래대금 한도·정수 주·현금 한도).
- 차트는 기본 '당시 실제 가격(raw)', 선택 시 'D 시점까지 알려진 기업행위만 반영한 수정주가'.
- 상장폐지는 실제로 일어난 날 처리되며 미리 알려주지 않는다.
- AI 투자자는 walk-forward 예측(각 예측일 이전 데이터로만 학습된 모델)으로 같은 조건에서 매매한다.
  해당 기간 예측이 없으면 AI 는 비활성화되고 이유를 표시한다.
- 과거 날짜의 종목 목록은 그날 실제 시세가 있던 종목으로 복원하지만, 미국은 상장폐지 종목이 대부분 빠져 있다(한계 표시).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from ..api import MarketData
from ..storage.catalog import catalog
from .backtest import MERGER_KEYWORDS, Constraints, Costs, PriceBook
from .portfolio import Portfolio
from .store import research_db

US_LIMIT = "미국 과거 종목 목록에는 상장폐지·합병된 기업 대부분이 빠져 있어, 당시 실제 시장보다 '살아남은 기업' 위주입니다."


@dataclass
class Side:
    cash: float
    shares: dict = field(default_factory=dict)
    cost_basis: dict = field(default_factory=dict)
    last_px: dict = field(default_factory=dict)
    pending_sells: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    equity: list = field(default_factory=list)


@dataclass
class GameState:
    game_id: str
    name: str
    market: str
    start_date: str
    current_date: str
    capital: float
    user: Side
    ai: Side | None
    orders: list = field(default_factory=list)      # 다음 거래일 시가 체결 대기 [{side, security_id, qty}]
    ai_run_id: str | None = None
    ai_n: int = 10
    ai_rebalance: int = 20
    ai_counter: int = 0
    ai_note: str = ""
    benchmark_start: float | None = None
    limits: list = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @staticmethod
    def from_json(s: str) -> "GameState":
        d = json.loads(s)
        d["user"] = Side(**d["user"])
        d["ai"] = Side(**d["ai"]) if d.get("ai") else None
        return GameState(**d)


def _calendar(market: str, start: date, end: date | None = None) -> list[date]:
    with catalog(read_only=True) as cat:
        q = "SELECT trade_date FROM trading_calendars WHERE market=? AND trade_date >= ? AND source IN ('both','observed')"
        args = [market, start]
        if end:
            q += " AND trade_date <= ?"
            args.append(end)
        return [r[0] for r in cat.execute(q + " ORDER BY 1", args).fetchall()]


def _ai_run(market: str, start: date) -> tuple[str | None, str]:
    with research_db(read_only=True) as db:
        r = db.execute("""SELECT run_id, min(trade_date), max(trade_date) FROM predictions
                          WHERE market=? AND kind LIKE 'oos_%' AND algorithm='lgbm' AND (pred_rank IS NOT NULL OR pred_ret IS NOT NULL)
                          GROUP BY 1 HAVING min(trade_date) <= ? ORDER BY max(trade_date) DESC LIMIT 1""", [market, start]).fetchone()
    if not r:
        return None, "이 시작일을 포함하는 walk-forward 예측이 없어 AI 투자자를 사용할 수 없습니다."
    return r[0], f"AI 는 walk-forward 예측({r[1]}~{r[2]})만 사용합니다. 각 날짜의 예측은 그 이전 데이터로 학습된 모델이 만든 값입니다."


def new_game(market: str, start: date, capital: float, name: str = "", with_ai: bool = True) -> GameState:
    days = _calendar(market, start)
    if not days:
        raise ValueError("시작일 이후 거래일이 없습니다.")
    d0 = days[0]
    ai_run, note = _ai_run(market, d0) if with_ai else (None, "AI 대결 꺼짐")
    g = GameState(game_id=f"game-{uuid.uuid4().hex[:8]}", name=name or f"{market} {d0}", market=market,
                  start_date=str(d0), current_date=str(d0), capital=capital, user=Side(cash=capital),
                  ai=Side(cash=capital) if ai_run else None, ai_run_id=ai_run, ai_note=note,
                  limits=[US_LIMIT] if market == "US" else ["한국 과거 종목 목록은 marcap 의 당시 상장 종목 전체(상장폐지 포함)로 복원합니다. 배당은 원천에 없어 반영되지 않습니다."])
    g.benchmark_start = _bench_value(market, d0)
    g.user.equity.append({"date": str(d0), "equity": capital})
    if g.ai:
        g.ai.equity.append({"date": str(d0), "equity": capital})
    save(g)
    return g


def _bench_value(market: str, d: date) -> float | None:
    md = MarketData()
    iid = "KRX:KS11" if market == "KR" else "YAHOO:^GSPC"
    df = md.indicators([iid], d - timedelta(days=10), d, as_of=datetime.combine(d, datetime.max.time()))
    md.close()
    return float(df["value"].iloc[-1]) if not df.empty else None


# ---------------------------------------------------------------- 사용자 화면용 (미래 비공개)
def visible_universe(g: GameState, text: str = "") -> pd.DataFrame:
    md = MarketData()
    try:
        u = md.universe(g.current_date, country=g.market, usable_only=True)
    finally:
        md.close()
    if text:
        u = u[u["name"].fillna("").str.contains(text, case=False) | u["symbol"].fillna("").str.contains(text, case=False)]
    return u.sort_values("market_cap", ascending=False, na_position="last")


def visible_chart(g: GameState, security_id: str, adjusted: bool = False, days: int = 750) -> pd.DataFrame:
    cur = date.fromisoformat(g.current_date)
    md = MarketData()
    try:
        df = md.prices(security_id, cur - timedelta(days=days), cur, adjust="all_corporate" if adjusted else "raw", as_of=cur)
    finally:
        md.close()
    assert df.empty or pd.to_datetime(df["trade_date"]).dt.date.max() <= cur, "미래 데이터 노출"
    return df


def place_order(g: GameState, side: str, security_id: str, qty: int) -> None:
    if side not in ("BUY", "SELL") or qty <= 0:
        raise ValueError("주문 형식 오류")
    if side == "SELL" and g.user.shares.get(security_id, 0) < qty:
        raise ValueError("보유 수량보다 많이 팔 수 없습니다 (공매도 불가).")
    g.orders.append({"side": side, "security_id": security_id, "qty": int(qty), "placed": g.current_date})
    save(g)


def _portfolio(side: Side, market: str) -> Portfolio:
    p = Portfolio(cash=side.cash, costs=Costs.for_market(market), cons=Constraints.default())
    p.shares, p.cost_basis, p.last_px = dict(side.shares), dict(side.cost_basis), dict(side.last_px)
    p.pending_sells = set(side.pending_sells)
    return p


def _store(side: Side, p: Portfolio) -> None:
    side.cash, side.shares, side.cost_basis, side.last_px = p.cash, p.shares, p.cost_basis, p.last_px
    side.pending_sells = sorted(p.pending_sells)
    for t in p.trades:
        t = dict(t)
        t["date"] = str(t["date"])
        side.trades.append(t)
    p.trades = []


def _ai_targets(g: GameState, d: date) -> dict | None:
    with research_db(read_only=True) as db:
        p = db.execute("""SELECT security_id, coalesce(pred_rank, pred_ret) AS s, model_trained_until FROM predictions
                          WHERE run_id=? AND algorithm='lgbm' AND trade_date=?""", [g.ai_run_id, d]).df()
    if p.empty:
        return None
    assert (pd.to_datetime(p["model_trained_until"]).dt.date < d).all()
    top = p.sort_values("s", ascending=False).head(g.ai_n)["security_id"].tolist()
    return {s: 1 / g.ai_n for s in top}


def advance(g: GameState, n_days: int = 1) -> GameState:
    cur = date.fromisoformat(g.current_date)
    days = _calendar(g.market, cur)
    future = [d for d in days if d > cur][:n_days]
    if not future:
        return g
    sids = set(g.user.shares) | {o["security_id"] for o in g.orders}
    if g.ai:
        sids |= set(g.ai.shares)
    ai_signal_today = None
    if g.ai and g.ai_counter % g.ai_rebalance == 0:
        ai_signal_today = _ai_targets(g, cur)
        if ai_signal_today:
            sids |= set(ai_signal_today)
    pb, pb_sids = None, None
    for d in future:
        if pb is None or set(sids) != pb_sids:
            pb_sids = set(sids)
            pb = PriceBook(g.market, sorted(sids) or ["__none__"], cur, future[-1])
        i = pb.didx.get(d)
        if i is None:
            cur = d
            continue
        up = _portfolio(g.user, g.market)
        up.corporate_actions(pb, i, d)
        up.delistings(pb, i, d, MERGER_KEYWORDS)
        for s in list(up.pending_sells):
            up.pending_sells.discard(s)
            up.sell(pb, i, d, s, up.shares.get(s, 0), "미체결 매도 재시도")
        for o in g.orders:
            if o["side"] == "SELL":
                up.sell(pb, i, d, o["security_id"], o["qty"], "사용자 주문")
        for o in g.orders:
            if o["side"] == "BUY":
                up.buy(pb, i, d, o["security_id"], o["qty"], "사용자 주문")
        g.orders = []
        g.user.equity.append({"date": str(d), "equity": up.mark(pb, i)})
        _store(g.user, up)
        if g.ai:
            ap = _portfolio(g.ai, g.market)
            ap.corporate_actions(pb, i, d)
            ap.delistings(pb, i, d, MERGER_KEYWORDS)
            if ai_signal_today is not None or ap.pending_sells:
                ap.rebalance(pb, i, d, ai_signal_today)
                ai_signal_today = None
            g.ai.equity.append({"date": str(d), "equity": ap.mark(pb, i)})
            _store(g.ai, ap)
            g.ai_counter += 1
            if g.ai_counter % g.ai_rebalance == 0:
                ai_signal_today = _ai_targets(g, d)
                if ai_signal_today:
                    sids |= set(ai_signal_today)
        cur = d
    g.current_date = str(cur)
    save(g)
    return g


def summary(g: GameState) -> dict:
    def side_sum(sd: Side):
        e = sd.equity[-1]["equity"] if sd.equity else g.capital
        realized = sum(t.get("pnl", 0) or 0 for t in sd.trades if t["side"] in ("SELL", "DELIST"))
        costs = sum(t.get("cost", 0) or 0 for t in sd.trades)
        return {"equity": e, "return": e / g.capital - 1, "cash": sd.cash, "realized_pnl": realized, "costs": costs,
                "n_trades": sum(1 for t in sd.trades if t["side"] in ("BUY", "SELL"))}
    out = {"user": side_sum(g.user)}
    if g.ai:
        out["ai"] = side_sum(g.ai)
    bv = _bench_value(g.market, date.fromisoformat(g.current_date))
    if g.benchmark_start and bv:
        out["benchmark_return"] = bv / g.benchmark_start - 1
    return out


def holdings(g: GameState, side: str = "user") -> pd.DataFrame:
    sd = g.user if side == "user" else g.ai
    rows = []
    for s, q in sd.shares.items():
        px = sd.last_px.get(s, 0)
        cb = sd.cost_basis.get(s, 0)
        rows.append({"security_id": s, "shares": q, "avg_cost": cb / q if q else 0, "price": px, "value": q * px,
                     "unrealized": q * px - cb})
    df = pd.DataFrame(rows)
    if not df.empty:
        with catalog(read_only=True) as cat:
            n = cat.execute("SELECT security_id, name FROM securities WHERE security_id IN (SELECT unnest(?::VARCHAR[]))",
                            [df["security_id"].tolist()]).df()
        df = df.merge(n, on="security_id", how="left")
    return df


def save(g: GameState) -> None:
    with research_db() as db:
        db.execute("INSERT OR REPLACE INTO games VALUES (?, now(), ?, ?)", [g.game_id, g.name, g.to_json()])


def load(game_id: str) -> GameState:
    with research_db(read_only=True) as db:
        r = db.execute("SELECT state FROM games WHERE game_id=?", [game_id]).fetchone()
    if not r:
        raise KeyError(game_id)
    return GameState.from_json(r[0])


def list_games() -> pd.DataFrame:
    with research_db(read_only=True) as db:
        return db.execute("SELECT game_id, name, updated_at FROM games ORDER BY updated_at DESC").df()
