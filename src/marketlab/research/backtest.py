"""백테스트 엔진 — 실제 거래 가능성을 반영한 일 단위 시뮬레이션.

체결 규칙
- 신호: 리밸런싱일 t 장 마감까지의 정보(전략이 생성한 목표 비중).
- 체결: t+1 거래일 시가(raw, 당시 실제 가격) ± 슬리피지. 시가가 없으면(거래정지) 매수는 취소, 매도는 다음 날 재시도.
- 수량: 정수 주. 현금 부족 시 매수 불가(무차입). 공매도·레버리지 없음.
- 주문 한도: 체결일 거래대금 × max_participation 이하 (부분 체결).
- 비용: 수수료(양방향) + 슬리피지 + 매도세(시점별 표) (+ 원화 기준 시 환전비용).
  슬리피지는 slippage_tiers 를 설정하면 체결일까지의 20일 평균 거래대금 구간별로 다르게 적용(소형주일수록 크게).
- 기업행위: KR 기준가 조정일에 보유 수량 ÷ 조정계수(분할·병합은 정확, 유·무상증자는 가치 보존 근사),
  US 분할일 수량 × 분할비율, 배당락일 현금 배당 입금(분할 반영 전 주당 금액으로 환산).
  KR 은 원천에 배당 정보가 없어 배당 수익이 빠진다(성과 과소평가 요인).
- 상장폐지: 상장폐지·합병이 확정된(status delisted/merged) 종목만, 마지막 거래일 이후 시세가 없으면 last_close(마지막 종가로 청산) 또는
  conservative(합병·완전자회사화 등 보상 근거가 있는 사유가 아니면 0원) 시나리오로 처리하고 거래내역에 표시.
  거래정지(시세 공백)는 기간과 무관하게 청산하지 않고 마지막 가격으로 보유·평가한다.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from ..config import get_settings
from ..storage.catalog import catalog, memory_connection
from .store import jdump, research_db

MERGER_KEYWORDS = ("합병", "완전자회사", "이전상장", "지주회사", "주식교환", "포괄적")


@dataclass
class Costs:
    commission_bps: float = 1.5
    slippage_bps: float = 10.0
    sell_tax_schedule: list = field(default_factory=list)
    fx_cost_bps: float = 0.0
    multiplier: float = 1.0
    # [{min_amount, bps}, ...] — 20일 평균 거래대금(현지 통화)이 min_amount 이상인 구간 중 가장 높은 구간의 bps 적용.
    # 비어 있으면 모든 종목에 slippage_bps. 거래대금을 모르면(신규 상장 등) 가장 낮은 구간(가장 비싼 쪽).
    slippage_tiers: list = field(default_factory=list)

    def __post_init__(self):
        tiers = sorted(({"min_amount": float(t["min_amount"]), "bps": float(t["bps"])} for t in self.slippage_tiers or []),
                       key=lambda t: t["min_amount"])
        if tiers and tiers[0]["min_amount"] > 0:
            raise ValueError("slippage_tiers 의 첫 구간은 min_amount: 0 이어야 합니다 (모든 종목이 어느 구간엔가 속하도록).")
        self.slippage_tiers = tiers

    @staticmethod
    def for_market(market: str, multiplier: float = 1.0) -> "Costs":
        c = dict(get_settings().get(f"research.costs.{market}", {}) or {})
        return Costs(commission_bps=float(c.get("commission_bps", 0)), slippage_bps=float(c.get("slippage_bps", 0)),
                     sell_tax_schedule=c.get("sell_tax_schedule", []) or [], fx_cost_bps=0.0, multiplier=multiplier,
                     slippage_tiers=c.get("slippage_tiers", []) or [])

    def slip_for(self, avg_amount: float | None) -> float:
        """종목의 20일 평균 거래대금에 맞는 슬리피지 비율."""
        if not self.slippage_tiers:
            return self.slip
        bps = self.slippage_tiers[0]["bps"]
        if avg_amount is not None and not math.isnan(avg_amount):
            for t in self.slippage_tiers:
                if avg_amount >= t["min_amount"]:
                    bps = t["bps"]
        return bps * self.multiplier / 1e4

    def sell_tax(self, d: date) -> float:
        bps = 0.0
        for row in sorted(self.sell_tax_schedule, key=lambda r: r["from"]):
            if date.fromisoformat(str(row["from"])) <= d:
                bps = float(row["bps"])
        return bps * self.multiplier / 1e4

    @property
    def comm(self) -> float:
        return self.commission_bps * self.multiplier / 1e4

    @property
    def slip(self) -> float:
        return self.slippage_bps * self.multiplier / 1e4


@dataclass
class Constraints:
    max_weight: float = 0.10
    max_participation: float = 0.05
    cash_buffer: float = 0.0
    delisting_scenario: str = "last_close"

    @staticmethod
    def default() -> "Constraints":
        p = get_settings().get("research.portfolio", {}) or {}
        return Constraints(max_weight=float(p.get("max_weight", 0.1)), max_participation=float(p.get("max_participation", 0.05)),
                           cash_buffer=float(p.get("cash_buffer", 0.0)), delisting_scenario=p.get("delisting_scenario", "last_close"))


class PriceBook:
    """필요한 종목·기간의 일봉을 날짜×종목 float32 배열로 보관 (메모리 절약)."""

    def __init__(self, market: str, sids: list[str], start: date, end: date):
        self.market = market
        g = (get_settings().paths.standardized / "daily_prices" / f"country={market}" / "year=*" / "*.parquet").as_posix()
        con = memory_connection()
        try:
            con.execute("CREATE TEMP TABLE ids AS SELECT unnest(?::VARCHAR[]) AS security_id", [list(sids)])
            adj = "coalesce(base_adj_factor, 1.0)" if market == "KR" else "1.0"
            split = "coalesce(nullif(stock_splits, 0), 1.0)" if market == "US" else "1.0"
            div = "coalesce(dividends, 0) * coalesce(split_factor, 1)" if market == "US" else "0.0"
            amt = "amount" if market == "KR" else "close * volume"
            df = con.execute(f"""
                SELECT p.security_id, p.trade_date, p.open, p.close, {amt} AS amount, {adj} AS adj, {split} AS split, {div} AS div,
                       p.is_traded
                FROM read_parquet('{g}', hive_partitioning=true) p JOIN ids USING (security_id)
                WHERE p.trade_date BETWEEN DATE '{start}' AND DATE '{end}'
            """).df()
            # 슬리피지 구간용 20일 평균 거래대금의 초기값: 시작일 전 약 2개월 거래대금
            pre = con.execute(f"""
                SELECT p.security_id, p.trade_date, {amt} AS amount
                FROM read_parquet('{g}', hive_partitioning=true) p JOIN ids USING (security_id)
                WHERE p.trade_date >= DATE '{start}' - INTERVAL 45 DAY AND p.trade_date < DATE '{start}'
            """).df()
            with catalog(read_only=True) as cat:
                cal = [r[0] for r in cat.execute("SELECT trade_date FROM trading_calendars WHERE market=? AND trade_date BETWEEN ? AND ? "
                                                  "AND source IN ('both','observed') ORDER BY 1", [market, start, end]).fetchall()]
                meta = cat.execute("SELECT security_id, name, status, delisting_reason, last_trade_date FROM securities "
                                   "WHERE security_id IN (SELECT unnest(?::VARCHAR[]))", [list(sids)]).df()
        finally:
            con.close()
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        self.dates = sorted(set(cal) | set(df["trade_date"].unique()))
        self.didx = {d: i for i, d in enumerate(self.dates)}
        self.sids = list(sids)
        self.sidx = {s: i for i, s in enumerate(self.sids)}
        T, N = len(self.dates), len(self.sids)

        def arr(col, fill=np.nan, dtype=np.float32):
            a = np.full((T, N), fill, dtype=dtype)
            r = df["trade_date"].map(self.didx).to_numpy(dtype=np.int64) if len(df) else np.array([], dtype=np.int64)
            c = df["security_id"].map(self.sidx).to_numpy(dtype=np.int64) if len(df) else np.array([], dtype=np.int64)
            a[r, c] = df[col].to_numpy(dtype=dtype)
            return a
        self.open, self.close, self.amount = arr("open"), arr("close"), arr("amount")
        # 슬리피지 구간 판단용: 체결일 직전까지의 20거래일 평균 거래대금 (체결일 당일 값은 장 시작 시 모르므로 제외)
        pre_a = np.empty((0, N), dtype=np.float32)
        if len(pre):
            pv = pre.pivot_table(index="trade_date", columns="security_id", values="amount", aggfunc="last").sort_index()
            pre_a = pv.reindex(columns=self.sids).to_numpy(dtype=np.float32)
        self.amount_avg20 = self._lagged_avg(np.vstack([pre_a, self.amount]), 20)[len(pre_a):]
        # 조정계수·분할비율은 float64 (0.02 같은 값의 float32 오차로 수량이 950.00000x 가 되는 것 방지)
        self.adj, self.split, self.div = arr("adj", 1.0, np.float64), arr("split", 1.0, np.float64), arr("div", 0.0, np.float64)
        self.last_idx = {s: self.didx[d] for s, d in df.groupby("security_id")["trade_date"].max().items()}
        self.meta = meta.set_index("security_id").to_dict("index")

    @staticmethod
    def _lagged_avg(a: np.ndarray, n: int) -> np.ndarray:
        """각 날짜 i 에 대해 i-n ~ i-1 의 평균(값이 있는 날만). 조회 구간 첫날은 이전 값이 없어 NaN."""
        if a.size == 0:
            return a.copy()
        return pd.DataFrame(a).rolling(n, min_periods=1).mean().shift(1).to_numpy(dtype=np.float32)

    TERMINATED_STATUS = ("delisted", "merged")

    def is_terminated(self, sid: str, i: int) -> bool:
        """i 시점 이후 더 이상 시세가 없고, 상장폐지·합병이 확정된 종목인가.
        시세 공백(거래정지)만으로는 청산하지 않는다 — 30일 넘게 정지됐다가 재개되는 종목도 있고,
        사유불명(unknown) 종목은 상장폐지가 확정되지 않았으므로 마지막 가격으로 계속 보유·평가한다."""
        st = (self.meta.get(sid) or {}).get("status")
        if st not in self.TERMINATED_STATUS:
            return False
        li = self.last_idx.get(sid)
        if li is None:   # 조회 구간에 시세가 전혀 없음 = 이미 거래 종료된 보유 종목
            return True
        return i > li


def run(market: str, targets: dict[date, dict[str, float]], start: date, end: date, capital: float = 100_000_000,
        costs: Costs | None = None, cons: Constraints | None = None, name: str = "strategy",
        prices: PriceBook | None = None, tranches: int = 1) -> dict:
    """targets: 신호일(t) → {security_id: 목표비중}. t 다음 거래일 시가에 체결.
    tranches>1: 분할매수·분할매도 — 이전 목표에서 새 목표로 tranches 거래일에 걸쳐 나눠 이동."""
    costs = costs or Costs.for_market(market)
    cons = cons or Constraints.default()
    sids = sorted({s for w in targets.values() for s in w})
    pb = prices or PriceBook(market, sids, start, end)
    dates = [d for d in pb.dates if start <= d <= end]
    sig_by_exec: dict[int, dict[str, float]] = {}
    for t, w in targets.items():
        nxt = next((i for i in range(pb.didx.get(t, -1) + 1, len(pb.dates)) if pb.dates[i] > t), None) \
            if t in pb.didx else next((pb.didx[d] for d in pb.dates if d > t), None)
        if nxt is not None:
            sig_by_exec[nxt] = {k: min(v, cons.max_weight) for k, v in w.items() if v > 0}
    if tranches > 1 and sig_by_exec:
        staged: dict[int, dict[str, float]] = {}
        prev: dict[str, float] = {}
        execs = sorted(sig_by_exec)
        for n_, i0 in enumerate(execs):
            new_t = sig_by_exec[i0]
            nxt_sig = execs[n_ + 1] if n_ + 1 < len(execs) else None
            for j in range(1, tranches + 1):
                ij = i0 + j - 1
                if nxt_sig is not None and ij >= nxt_sig:
                    break
                keys = set(prev) | set(new_t)
                staged[ij] = {k: prev.get(k, 0) + (new_t.get(k, 0) - prev.get(k, 0)) * j / tranches for k in keys}
                staged[ij] = {k: v for k, v in staged[ij].items() if v > 1e-9}
            prev = new_t
        sig_by_exec = staged
    from .portfolio import Portfolio
    pf = Portfolio(cash=float(capital), costs=costs, cons=cons)
    equity = []
    for d in dates:
        i = pb.didx[d]
        pf.corporate_actions(pb, i, d)              # 1) 기업행위 (장 시작 전)
        pf.delistings(pb, i, d, MERGER_KEYWORDS)     # 2) 상장폐지·거래종료
        tgt = sig_by_exec.get(i)                     # 3) t+1 시가 체결
        if tgt is not None or pf.pending_sells:
            pf.rebalance(pb, i, d, tgt)
        equity.append({"date": d, "equity": pf.mark(pb, i), "cash": pf.cash, "n_positions": len(pf.shares)})  # 4) 종가 평가
    trades = pf.trades
    eq = pd.DataFrame(equity)
    tr = pd.DataFrame(trades)
    return {"name": name, "market": market, "equity": eq, "trades": tr, "metrics": metrics(eq, tr, capital),
            "costs": costs.__dict__, "constraints": cons.__dict__}


# ---------------------------------------------------------------------------- metrics
def metrics(eq: pd.DataFrame, tr: pd.DataFrame, capital: float) -> dict:
    if eq.empty:
        return {}
    e = eq.set_index("date")["equity"]
    r = e.pct_change().dropna()
    years = max((e.index[-1] - e.index[0]).days / 365.25, 1e-9)
    total = e.iloc[-1] / capital - 1
    cagr = (e.iloc[-1] / capital) ** (1 / years) - 1 if e.iloc[-1] > 0 else -1.0
    vol = r.std() * np.sqrt(252)
    down = r[r < 0].std() * np.sqrt(252)
    dd = e / e.cummax() - 1
    mdd = float(dd.min())
    sells = tr[tr["side"].isin(["SELL", "DELIST"])] if not tr.empty else tr
    wins = sells[sells["pnl"] > 0]["pnl"] if not sells.empty and "pnl" in sells else pd.Series(dtype=float)
    losses = sells[sells["pnl"] <= 0]["pnl"] if not sells.empty and "pnl" in sells else pd.Series(dtype=float)
    traded = tr[tr["side"].isin(["BUY", "SELL"])]["value"].sum() if not tr.empty else 0.0
    return {
        "start": str(e.index[0]), "end": str(e.index[-1]), "years": round(years, 2),
        "total_return": float(total), "cagr": float(cagr), "volatility": float(vol),
        "sharpe": float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else None,
        "sortino": float(r.mean() * 252 / down) if down and down > 0 else None,
        "mdd": mdd, "calmar": float(cagr / abs(mdd)) if mdd < 0 else None,
        "win_rate": float(len(wins) / (len(wins) + len(losses))) if len(wins) + len(losses) else None,
        "avg_win": float(wins.mean()) if len(wins) else None, "avg_loss": float(losses.mean()) if len(losses) else None,
        "n_trades": int((tr["side"].isin(["BUY", "SELL"])).sum()) if not tr.empty else 0,
        "turnover_per_year": float(traded / e.mean() / years) if e.mean() > 0 else None,
        "total_cost": float(tr["cost"].sum()) if not tr.empty and "cost" in tr else 0.0,
        "n_delist_events": int((tr["side"] == "DELIST").sum()) if not tr.empty else 0,
        "avg_positions": float(eq["n_positions"].mean()),
    }


def yearly_returns(eq: pd.DataFrame) -> pd.DataFrame:
    e = eq.set_index(pd.to_datetime(eq["date"]))["equity"]
    y = e.resample("YE").last()
    first = e.iloc[0]
    prev = y.shift(1).fillna(first)
    return pd.DataFrame({"year": y.index.year, "return": (y / prev - 1).to_numpy()})


def block_bootstrap(eq: pd.DataFrame, n: int = 1000, block: int = 20, seed: int = 42) -> dict:
    """거래일 블록 재표본추출로 CAGR·샤프 신뢰구간 (시계열 의존성 반영)."""
    r = eq["equity"].pct_change().dropna().to_numpy()
    if len(r) < block * 5:
        return {"note": "표본 부족 — 신뢰구간 계산 불가"}
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(len(r) / block))
    cagrs, sharpes = [], []
    for _ in range(n):
        starts = rng.integers(0, len(r) - block, nb)
        s = np.concatenate([r[a:a + block] for a in starts])[:len(r)]
        g = np.prod(1 + s)
        cagrs.append(g ** (252 / len(s)) - 1)
        sharpes.append(s.mean() / s.std() * np.sqrt(252) if s.std() > 0 else 0)
    q = lambda a: [float(np.quantile(a, 0.05)), float(np.quantile(a, 0.5)), float(np.quantile(a, 0.95))]
    return {"cagr_p05_p50_p95": q(cagrs), "sharpe_p05_p50_p95": q(sharpes), "prob_sharpe_gt_0": float(np.mean(np.array(sharpes) > 0)),
            "block_days": block, "n": n}


def benchmark_curves(market: str, start: date, end: date, capital: float) -> pd.DataFrame:
    """지수(가격지수, 배당 미포함) + 투자 가능 종목 동일가중(일 단위 리밸런싱, 비용 제외) 비교 곡선."""
    from .features import feature_glob
    base = get_settings().paths.standardized / "market_indicators"
    ids = ["KRX:KS11", "KRX:KQ11"] if market == "KR" else ["YAHOO:^GSPC", "YAHOO:^IXIC"]
    con = memory_connection()
    try:
        idx = con.execute(f"""SELECT indicator_id, obs_date, value FROM read_parquet('{(base / '*.parquet').as_posix()}', union_by_name=true)
                              WHERE indicator_id IN (SELECT unnest(?::VARCHAR[])) AND obs_date BETWEEN ? AND ?""", [ids, start, end]).df()
        ew = con.execute(f"""SELECT trade_date AS obs_date, avg(ret_1) AS r FROM read_parquet('{feature_glob(market)}')
                             WHERE trade_date BETWEEN ? AND ? GROUP BY 1 ORDER BY 1""", [start, end]).df()
    finally:
        con.close()
    out = {}
    for iid, g in idx.groupby("indicator_id"):
        g = g.sort_values("obs_date")
        out[iid.split(":")[1]] = pd.Series((g["value"] / g["value"].iloc[0] * capital).to_numpy(), index=pd.to_datetime(g["obs_date"]).dt.date)
    if market == "US":
        g = (get_settings().paths.standardized / "daily_prices" / "country=US" / "year=*" / "*.parquet").as_posix()
        con = memory_connection()
        try:
            spy = con.execute(f"""SELECT trade_date, adj_close_tr FROM read_parquet('{g}', hive_partitioning=true)
                                  WHERE symbol = 'SPY' AND trade_date BETWEEN ? AND ? ORDER BY 1""", [start, end]).df()
        except Exception:
            spy = pd.DataFrame()
        finally:
            con.close()
        if not spy.empty:
            out["SPY(배당재투자)"] = pd.Series((spy["adj_close_tr"] / spy["adj_close_tr"].iloc[0] * capital).to_numpy(),
                                             index=pd.to_datetime(spy["trade_date"]).dt.date)
    if not ew.empty:
        out["동일가중(투자가능종목)"] = pd.Series((np.cumprod(1 + ew["r"].fillna(0).to_numpy()) * capital), index=pd.to_datetime(ew["obs_date"]).dt.date)
    return pd.DataFrame(out)


def save(result: dict, strategy: str, params: dict, selection_period: str, note: str = "") -> str:
    s = get_settings()
    bt_id = f"bt-{result['market']}-{strategy}-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4]}"
    d = s.paths.data / "research_backtests"
    d.mkdir(parents=True, exist_ok=True)
    ep, tp = d / f"{bt_id}_equity.parquet", d / f"{bt_id}_trades.parquet"
    result["equity"].to_parquet(ep, index=False)
    if not result["trades"].empty:
        result["trades"].assign(date=lambda x: pd.to_datetime(x["date"])).to_parquet(tp, index=False)
    m = result["metrics"]
    with research_db() as db:
        db.execute("INSERT INTO backtests VALUES (?, now(), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [bt_id, result["market"], strategy, jdump(params), m.get("start"), m.get("end"), jdump(result["costs"]),
                    jdump(result["constraints"]), jdump(m), jdump(result.get("benchmark", {})), selection_period,
                    str(ep), str(tp), note])
    return bt_id


def to_krw(eq: pd.DataFrame, trades: pd.DataFrame, fx_cost_bps: float | None = None) -> dict:
    """미국 전략의 원화 기준 성과: 달러 자산 × 원/달러(KRW=X, 해당일 사용 가능 값) − 거래금액 × 환전비용."""
    fx_bps = fx_cost_bps if fx_cost_bps is not None else float(get_settings().get("research.costs.US.fx_cost_bps", 25))
    base = get_settings().paths.standardized / "market_indicators" / "yahoo_KRW_X.parquet"
    if not base.exists() or eq.empty:
        return {"note": "환율 데이터 없음"}
    fx = pd.read_parquet(base)[["obs_date", "value"]]
    fx["obs_date"] = pd.to_datetime(fx["obs_date"]).dt.date
    e = eq[["date", "equity"]].merge(fx, left_on="date", right_on="obs_date", how="left").sort_values("date")
    e["value"] = e["value"].ffill().bfill()
    traded = trades[trades["side"].isin(["BUY", "SELL"])].groupby("date")["value"].sum() if not trades.empty else pd.Series(dtype=float)
    fxcost = e["date"].map(traded).fillna(0).cumsum() * fx_bps / 1e4
    e["equity_krw"] = (e["equity"] - fxcost) * e["value"]
    m = metrics(pd.DataFrame({"date": e["date"], "equity": e["equity_krw"], "n_positions": 0}), pd.DataFrame(columns=["side"]),
                float(e["equity_krw"].iloc[0]))
    return {"metrics_krw": m, "fx_cost_bps": fx_bps, "fx_start": float(e["value"].iloc[0]), "fx_end": float(e["value"].iloc[-1])}
