"""백테스트 회계·체결 규칙과 과거 시장 게임 검증 (실제 KR 데이터)."""
from datetime import date

import pytest

from marketlab.research import backtest as bt

pytestmark = pytest.mark.data
SAMSUNG = "KR-005930-19950502"


def test_next_open_execution_costs_and_split():
    """2018-04-20 신호 → 04-23 시가 체결, 2018-05-04 50:1 분할 후 수량 50배·평가액 연속."""
    tg = {date(2018, 4, 20): {SAMSUNG: 0.5}}
    costs = bt.Costs.for_market("KR")
    cons = bt.Constraints(max_weight=1.0, max_participation=0.05)
    res = bt.run("KR", tg, date(2018, 4, 20), date(2018, 5, 10), capital=100_000_000, costs=costs, cons=cons)
    tr = res["trades"]
    buy = tr[tr["side"] == "BUY"].iloc[0]
    assert buy["date"] == date(2018, 4, 23)
    from marketlab.api import MarketData
    md = MarketData()
    o = md.prices(SAMSUNG, "2018-04-23", "2018-04-23").iloc[0]["open"]
    assert buy["price"] == pytest.approx(o * (1 + costs.slip))
    assert buy["shares"] == int(100_000_000 * 0.5 // (o * (1 + costs.slip)))
    eq = res["equity"].set_index("date")
    # 분할 전후 평가액이 분할 때문에 50분의 1로 떨어지지 않아야 함
    before, after = eq.loc[date(2018, 4, 27), "equity"], eq.loc[date(2018, 5, 4), "equity"]
    assert 0.9 < after / before < 1.1
    # 현금 + 보유 평가 = 자산 (마지막 날)
    last = res["equity"].iloc[-1]
    close = md.prices(SAMSUNG, last["date"], last["date"]).iloc[0]["close"]
    assert last["equity"] == pytest.approx(last["cash"] + buy["shares"] * 50 * close, rel=1e-9)
    # 현금 회계: 초기자금 − 매수금액 − 수수료
    assert last["cash"] == pytest.approx(100_000_000 - buy["value"] * (1 + costs.comm), rel=1e-9)


def test_sell_tax_schedule():
    c = bt.Costs.for_market("KR")
    assert c.sell_tax(date(2010, 1, 1)) == pytest.approx(0.003)
    assert c.sell_tax(date(2024, 6, 1)) == pytest.approx(0.0018)
    assert bt.Costs.for_market("KR", 0.0).sell_tax(date(2024, 6, 1)) == 0


def test_delisted_holding_is_closed():
    """상장폐지 종목(우리은행 000030, 2019-02-13 폐지)을 보유하면 마지막 거래일 이후 청산 처리."""
    sid = "KR-000030-20141119"
    res = bt.run("KR", {date(2019, 1, 2): {sid: 0.3}}, date(2019, 1, 2), date(2019, 3, 29), capital=100_000_000)
    tr = res["trades"]
    assert (tr["side"] == "DELIST").sum() == 1
    cons = bt.Constraints.default()
    cons.delisting_scenario = "conservative"
    res2 = bt.run("KR", {date(2019, 1, 2): {sid: 0.3}}, date(2019, 1, 2), date(2019, 3, 29), capital=100_000_000, cons=cons)
    d2 = res2["trades"][res2["trades"]["side"] == "DELIST"].iloc[0]
    # 지주회사 완전자회사화 → 합병류 사유이므로 보수적 시나리오에서도 마지막 종가 인정
    assert d2["price"] > 0


def test_default_single_stock_cap():
    res = bt.run("KR", {date(2018, 4, 20): {SAMSUNG: 0.5}}, date(2018, 4, 20), date(2018, 4, 30), capital=100_000_000)
    b = res["trades"][res["trades"]["side"] == "BUY"].iloc[0]
    assert b["value"] <= 100_000_000 * 0.10   # 기본 단일 종목 비중 상한 10%


def test_no_cash_overdraft_and_participation_cap():
    tg = {date(2015, 3, 2): {SAMSUNG: 1.0}}
    cons = bt.Constraints(max_weight=1.0, max_participation=0.00001)
    res = bt.run("KR", tg, date(2015, 3, 2), date(2015, 3, 20), capital=1e12, cons=cons)
    assert (res["equity"]["cash"] >= -1e-6).all()
    b = res["trades"][res["trades"]["side"] == "BUY"]
    from marketlab.api import MarketData
    amt = MarketData().prices(SAMSUNG, "2015-03-03", "2015-03-03").iloc[0]["amount"]
    assert b.iloc[0]["value"] <= amt * 0.00001 * 1.01


def test_game_hides_future_and_executes_next_open():
    from marketlab.research import game as G
    g = G.new_game("KR", date(2018, 4, 26), 10_000_000, with_ai=False)
    ch = G.visible_chart(g, SAMSUNG)
    import pandas as pd
    assert pd.to_datetime(ch["trade_date"]).dt.date.max().isoformat() <= g.current_date
    u = G.visible_universe(g, "삼성전자")
    assert SAMSUNG in set(u["security_id"])
    G.place_order(g, "BUY", SAMSUNG, 2)
    with pytest.raises(ValueError):
        G.place_order(g, "SELL", SAMSUNG, 5)   # 보유보다 많이 팔기 불가
    G.advance(g, 1)
    t = [x for x in g.user.trades if x["side"] == "BUY"]
    assert t and t[0]["date"] == "2018-04-27" and t[0]["shares"] == 2
    G.advance(g, 10)                            # 2018-05-04 50:1 분할 통과
    assert g.user.shares[SAMSUNG] == pytest.approx(100)
    g2 = G.load(g.game_id)                      # 저장·복원
    assert g2.current_date == g.current_date and g2.user.shares == g.user.shares


def test_memory_monitor_trips():
    from marketlab.research.resources import MemoryMonitor, MemoryPressure
    import time
    m = MemoryMonitor(hard_gb=0.0001).start()
    time.sleep(1.2)
    with pytest.raises(MemoryPressure):
        m.check()
    m.stop()
