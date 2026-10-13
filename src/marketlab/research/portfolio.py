"""포트폴리오 회계 — 백테스트 엔진과 과거 시장 게임이 같은 규칙(체결·비용·기업행위·상장폐지)을 공유한다."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date


def _snap(q: float) -> float:
    """분할·병합 후 수량이 정수에 극히 가까우면 정수로 (부동소수점 오차 제거). 단주는 그대로 둔다."""
    r = round(q)
    return float(r) if abs(q - r) < 1e-6 * max(1.0, abs(q)) else q


@dataclass
class Portfolio:
    cash: float
    costs: object            # backtest.Costs
    cons: object             # backtest.Constraints
    shares: dict = field(default_factory=dict)
    cost_basis: dict = field(default_factory=dict)
    last_px: dict = field(default_factory=dict)
    pending_sells: set = field(default_factory=set)
    trades: list = field(default_factory=list)

    # ---------------------------------------------------------------- daily steps
    def corporate_actions(self, pb, i: int, d: date) -> None:
        for s in list(self.shares):
            if s not in pb.sidx:
                continue
            j = pb.sidx[s]
            f = float(pb.adj[i, j])
            if f > 0 and not math.isnan(f) and abs(f - 1) > 1e-6:
                self.shares[s] = _snap(self.shares[s] / f)
                if s in self.last_px:
                    self.last_px[s] *= f
            r = float(pb.split[i, j])
            if r > 0 and not math.isnan(r) and abs(r - 1) > 1e-6:
                self.shares[s] = _snap(self.shares[s] * r)
                if s in self.last_px:
                    self.last_px[s] /= r
            dv = float(pb.div[i, j])
            if dv > 0 and not math.isnan(dv):
                self.cash += self.shares[s] * dv
                self.trades.append({"date": d, "security_id": s, "side": "DIVIDEND", "shares": self.shares[s], "price": dv,
                                    "value": self.shares[s] * dv, "cost": 0.0, "pnl": 0.0, "note": "배당 입금"})

    def delistings(self, pb, i: int, d: date, merger_keywords) -> None:
        for s in list(self.shares):
            if s in pb.sidx and pb.is_terminated(s, i):
                reason = str((pb.meta.get(s) or {}).get("delisting_reason") or "")
                px = self.last_px.get(s, 0.0)
                merger = any(k in reason for k in merger_keywords)
                if self.cons.delisting_scenario == "conservative" and not merger:
                    px, note = 0.0, f"상장폐지 보수적 처리(0원): {reason or '사유 미확인'}"
                else:
                    note = f"상장폐지/거래종료 — 마지막 종가로 청산 가정: {reason or '사유 미확인'}"
                value = self.shares[s] * px
                self.cash += value
                self.trades.append({"date": d, "security_id": s, "side": "DELIST", "shares": self.shares[s], "price": px,
                                    "value": value, "cost": 0.0, "pnl": value - self.cost_basis.get(s, 0), "note": note})
                self.shares.pop(s)
                self.cost_basis.pop(s, None)
                self.pending_sells.discard(s)

    def equity_at_open(self, pb, i: int) -> float:
        v = self.cash
        for s, q in self.shares.items():
            o = float(pb.open[i, pb.sidx[s]]) if s in pb.sidx else 0.0
            v += q * (o if o > 0 and not math.isnan(o) else self.last_px.get(s, 0.0))
        return v

    def _price(self, pb, i, s):
        if s not in pb.sidx:
            return 0.0
        px = float(pb.open[i, pb.sidx[s]])
        return px if px > 0 and not math.isnan(px) else 0.0

    def sell(self, pb, i: int, d: date, s: str, qty: float, note: str = "") -> float:
        px = self._price(pb, i, s)
        have = self.shares.get(s, 0)
        if px <= 0 or have <= 0:
            if have > 0:
                self.pending_sells.add(s)
            return 0
        j = pb.sidx[s]
        cap = float(pb.amount[i, j]) * self.cons.max_participation if pb.amount[i, j] > 0 else 0.0
        q = min(qty, have)
        if cap > 0:
            q = min(q, math.floor(cap / px))
        if q <= 0:
            self.pending_sells.add(s)
            return 0
        exec_px = px * (1 - self.costs.slip)
        value = q * exec_px
        fee = value * (self.costs.comm + self.costs.sell_tax(d))
        avg = self.cost_basis.get(s, 0) / have
        self.cash += value - fee
        self.shares[s] = have - q
        self.cost_basis[s] = avg * self.shares[s]
        if self.shares[s] <= 1e-9:
            self.shares.pop(s)
            self.cost_basis.pop(s, None)
            self.pending_sells.discard(s)
        elif qty > q:
            self.pending_sells.add(s)
        self.trades.append({"date": d, "security_id": s, "side": "SELL", "shares": q, "price": exec_px, "value": value,
                            "cost": fee + q * px * self.costs.slip, "pnl": value - fee - avg * q, "note": note})
        return q

    def buy(self, pb, i: int, d: date, s: str, qty: float, note: str = "") -> float:
        px = self._price(pb, i, s)
        if px <= 0 or qty <= 0:
            return 0
        j = pb.sidx[s]
        cap = float(pb.amount[i, j]) * self.cons.max_participation if pb.amount[i, j] > 0 else 0.0
        exec_px = px * (1 + self.costs.slip)
        q = min(math.floor(qty), math.floor(cap / px) if cap > 0 else 0,
                math.floor(self.cash / (exec_px * (1 + self.costs.comm))))
        if q <= 0:
            return 0
        value = q * exec_px
        fee = value * self.costs.comm
        self.cash -= value + fee
        self.shares[s] = self.shares.get(s, 0) + q
        self.cost_basis[s] = self.cost_basis.get(s, 0) + value + fee
        self.trades.append({"date": d, "security_id": s, "side": "BUY", "shares": q, "price": exec_px, "value": value,
                            "cost": fee + q * px * self.costs.slip, "pnl": 0.0, "note": note})
        return q

    def rebalance(self, pb, i: int, d: date, target: dict | None) -> None:
        """목표 비중으로 조정 (target=None 이면 미체결 매도만 재시도). 매도 먼저, 그다음 매수."""
        sells = set(self.pending_sells)
        self.pending_sells = set()
        if target is not None:
            sells |= {s for s in self.shares if s not in target}
        for s in sorted(sells):
            self.sell(pb, i, d, s, self.shares.get(s, 0))
        if target is None:
            return
        eq = self.equity_at_open(pb, i) * (1 - self.cons.cash_buffer)
        plan = []
        for s, w in target.items():
            px = self._price(pb, i, s)
            if px <= 0:
                continue
            want = math.floor(eq * min(w, self.cons.max_weight) / (px * (1 + self.costs.slip)))
            plan.append((s, want - self.shares.get(s, 0)))
        for s, dq in sorted(plan, key=lambda x: x[1]):
            if dq < 0:
                self.sell(pb, i, d, s, -dq, "비중 축소")
        for s, dq in plan:
            if dq > 0:
                self.buy(pb, i, d, s, dq)

    def mark(self, pb, i: int) -> float:
        v = 0.0
        for s, q in self.shares.items():
            if s in pb.sidx:
                c = float(pb.close[i, pb.sidx[s]])
                if c > 0 and not math.isnan(c):
                    self.last_px[s] = c
            v += q * self.last_px.get(s, 0.0)
        return self.cash + v
