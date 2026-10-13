"""미국 수집·표준화 로직 검증.

이 환경에서 Yahoo 접속이 불가할 수 있으므로 fetch_history 만 대체(test double)하고
나머지(재개 상태, 배치 기록, 기업행위, 분할 역산, 멱등성)는 실제 코드 경로를 그대로 실행한다.
대체 데이터는 테스트 내부에서만 쓰이며 제품 데이터에 섞이지 않는다(isolated 임시 디렉터리).
"""
from datetime import date

import pandas as pd
import pytest

from marketlab.collectors import base, yfinance_us
from marketlab.storage.catalog import catalog


def _register(sid, sym):
    with catalog() as con:
        con.execute("INSERT INTO securities (security_id, country, market, security_type, name, status) "
                    "VALUES (?, 'US', 'NASDAQ', 'common', ?, 'listed')", [sid, sym])
        con.execute("INSERT INTO symbol_history VALUES (?, 'yahoo', ?, 'NASDAQ', ?, DATE '2020-01-01', NULL, 'test', 'reported')",
                    [sid, sym, sym])


def _frame(split_on=None):
    idx = pd.DatetimeIndex(["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06"], tz="America/New_York")
    df = pd.DataFrame({"Open": [10.0, 10.2, 10.1, 10.3], "High": [10.5, 10.6, 10.4, 10.8],
                       "Low": [9.8, 10.0, 9.9, 10.1], "Close": [10.2, 10.1, 10.3, 10.6],
                       "Adj Close": [10.0, 9.9, 10.1, 10.6], "Volume": [4000, 3000, 5000, 4500],
                       "Dividends": [0, 0, 0, 0.0], "Stock Splits": [0, 0, 0, 0.0]}, index=idx)
    if split_on:
        df.loc[pd.Timestamp(split_on, tz="America/New_York"), "Stock Splits"] = 4.0
    return df


def test_collect_resume_split_reconstruction_and_idempotency(isolated, monkeypatch):
    _register("US-AAA-20240101", "AAA")
    _register("US-BBB-20240101", "BBB")
    calls = []

    def fake_fetch(symbol, start):
        calls.append((symbol, start))
        if symbol == "BBB":
            return pd.DataFrame(), "YFTzMissingError: possibly delisted; no timezone found"
        return _frame(split_on="2024-06-05"), None

    monkeypatch.setattr(yfinance_us, "fetch_history", fake_fetch)
    r = yfinance_us.collect(mode="full")
    assert r["ok"] == 1 and r["failed"] == 1
    st = base.get_item_states("yfinance")
    assert st["US-AAA-20240101"]["status"] == "ok" and st["US-BBB-20240101"]["status"] == "empty"

    # 재개: 전체 수집을 다시 실행하면 이미 받은 종목은 건너뛴다
    calls.clear()
    yfinance_us.collect(mode="full")
    assert [c[0] for c in calls if c[0] != "SPY"] == ["BBB"]

    # 표준화: 분할(4:1, 2024-06-05) 이전 가격은 ×4 로 당시 거래가 역산
    from marketlab.transforms import us_prices
    us_prices.run(years=[2024])
    from marketlab.api import MarketData
    md = MarketData()
    px = md.prices("US-AAA-20240101", "2024-06-01", "2024-06-30")
    assert list(px["close"]) == pytest.approx([10.2 * 4, 10.1 * 4, 10.3, 10.6])
    assert px.iloc[0]["volume"] == 1000
    # 분할 반영 비교가격: as_of 이후면 Yahoo 원래 값과 같다
    adj = md.prices("US-AAA-20240101", "2024-06-01", "2024-06-30", adjust="split")
    assert list(adj["close"]) == pytest.approx([10.2, 10.1, 10.3, 10.6])
    # 분할 전 시점(as_of)에서는 분할을 몰랐으므로 조정하지 않는다 (미래정보 누출 방지)
    pit = md.prices("US-AAA-20240101", "2024-06-01", "2024-06-04", adjust="split", as_of="2024-06-04")
    assert list(pit["close"]) == pytest.approx([40.8, 40.4])

    # 멱등성: 같은 데이터를 다시 받아도 표준화 결과 중복 없음
    yfinance_us.collect(mode="incremental", symbols=["AAA"])
    us_prices.run(years=[2024])
    md2 = MarketData()
    assert len(md2.prices("US-AAA-20240101", "2024-06-01", "2024-06-30")) == 4


def test_stop_flag_halts_and_saves_state(isolated, monkeypatch):
    for i in range(3):
        _register(f"US-S{i}-20240101", f"S{i}")

    def fake_fetch(symbol, start):
        if symbol == "S0":
            base.request_stop()
        return _frame(), None

    monkeypatch.setattr(yfinance_us, "fetch_history", fake_fetch)
    r = yfinance_us.collect(mode="full")
    assert r["status"] == "stopped" and r["ok"] == 1
    assert base.get_item_states("yfinance")["US-S0-20240101"]["status"] == "ok"
    base.clear_stop()


def test_rate_limit_circuit_breaker(isolated, monkeypatch):
    for i in range(3):
        _register(f"US-R{i}-20240101", f"R{i}")
    monkeypatch.setattr(yfinance_us, "fetch_history", lambda s, st: (pd.DataFrame(), "YFRateLimitError: Too Many Requests"))
    monkeypatch.setattr("time.sleep", lambda x: None)
    from marketlab.config import get_settings
    get_settings().raw["http"]["circuit_breaker_threshold"] = 2
    r = yfinance_us.collect(mode="full")
    assert r["status"] == "blocked"


def test_blocked_yahoo_does_not_mark_delisted(isolated, monkeypatch):
    """Yahoo 접속 불가 시 yfinance 는 'possibly delisted' 를 돌려준다 → 상장폐지로 기록하지 않고 중단해야 한다."""
    for i in range(3):
        _register(f"US-B{i}-20240101", f"B{i}")
    monkeypatch.setattr(yfinance_us, "fetch_history",
                        lambda s, st: (pd.DataFrame(), f"YFTzMissingError: ${s}: possibly delisted; no timezone found"))
    r = yfinance_us.collect(mode="full")
    assert r["status"] == "blocked"
    assert base.get_item_states("yfinance") == {}


def test_empty_streak_with_dead_probe_is_forgotten(isolated, monkeypatch):
    for i in range(25):
        _register(f"US-E{i:02d}-20240101", f"E{i:02d}")
    state = {"probe_calls": 0}

    def fake(symbol, start):
        if symbol == "SPY":
            state["probe_calls"] += 1
            return (_frame(), None) if state["probe_calls"] == 1 else (pd.DataFrame(), "dead")
        return pd.DataFrame(), "YFTzMissingError: possibly delisted"

    monkeypatch.setattr(yfinance_us, "fetch_history", fake)
    monkeypatch.setattr("time.sleep", lambda x: None)
    r = yfinance_us.collect(mode="full")
    assert r["status"] == "blocked"
    assert not any(v["status"] == "empty" for v in base.get_item_states("yfinance").values())
