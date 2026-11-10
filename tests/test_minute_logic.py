"""분봉 수집 로직 검증.

Yahoo 접속 없이 돌도록 fetch_minutes / yahoo_alive 만 대체(test double)하고,
구간 계획·확정 세션 판정·중복 방지·이어받기·시간 예산은 실제 코드 경로를 그대로 실행한다.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from marketlab.collectors import yfinance_minute as ym

KST = ZoneInfo("Asia/Seoul")


def _bars(day: str, times=("09:00", "09:01", "09:02"), volumes=(100, 0, 300), base=70000.0):
    idx = pd.DatetimeIndex([f"{day} {t}" for t in times], tz="Asia/Seoul")
    n = len(times)
    return pd.DataFrame({"Open": [base + i for i in range(n)], "High": [base + i + 5 for i in range(n)],
                         "Low": [base + i - 5 for i in range(n)], "Close": [base + i + 1 for i in range(n)],
                         "Adj Close": [base + i + 1 for i in range(n)], "Volume": list(volumes)}, index=idx)


# ------------------------------------------------------------------ 순수 함수
def test_yahoo_symbol_kr():
    assert ym.yahoo_symbol_kr("005930", "KOSPI") == "005930.KS"
    assert ym.yahoo_symbol_kr("247540", "KOSDAQ") == "247540.KQ"
    assert ym.yahoo_symbol_kr("247540", "KOSDAQ GLOBAL") == "247540.KQ"
    assert ym.yahoo_symbol_kr("123456", "KONEX") is None


def test_finalized_through_waits_for_close_plus_delay():
    before = datetime(2026, 11, 10, 16, 0, tzinfo=KST)   # 마감(15:30)+60분 이전
    after = datetime(2026, 11, 10, 16, 31, tzinfo=KST)
    assert ym.finalized_through("KR", before) == (date(2026, 11, 9), date(2026, 11, 10))
    assert ym.finalized_through("KR", after) == (date(2026, 11, 10), date(2026, 11, 10))
    # 미국: 한국 시각 11일 아침 = 뉴욕 10일 저녁 → 10일 세션 확정
    us = datetime(2026, 11, 11, 8, 0, tzinfo=KST)
    assert ym.finalized_through("US", us) == (date(2026, 11, 10), date(2026, 11, 10))


def test_plan_windows_respects_yahoo_limits():
    today, through = date(2026, 11, 10), date(2026, 11, 10)
    first = ym.plan_windows(None, through, today)
    assert first[0][0] == date(2026, 10, 12)                      # 29일 전부터
    assert first[-1][1] == date(2026, 11, 11)                     # 끝일 제외
    assert all((b - a).days <= 7 for a, b in first)
    assert all(first[i][1] == first[i + 1][0] for i in range(len(first) - 1))   # 빈틈·겹침 없음
    assert ym.plan_windows(date(2026, 11, 9), through, today) == [(date(2026, 11, 10), date(2026, 11, 11))]
    assert ym.plan_windows(date(2026, 11, 10), through, today) == []            # 이미 최신
    # 오래 쉬었다 돌아오면 Yahoo 가 보관하는 범위까지만 거슬러 올라간다
    assert ym.plan_windows(date(2026, 1, 1), through, today)[0][0] == date(2026, 10, 12)


def test_normalize_filters_dates_zero_volume_and_duplicates():
    df = pd.concat([_bars("2026-11-06"), _bars("2026-11-09"), _bars("2026-11-10"), _bars("2026-11-09")])
    out = ym.normalize(df, "KR-005930-19950502", "005930.KS", "KR", after=date(2026, 11, 6), through=date(2026, 11, 9))
    assert list(out.columns) == ym.COLUMNS
    assert set(out["trade_date"]) == {date(2026, 11, 9)}          # 이미 받은 날·미확정 날 제외
    assert len(out) == 2 and (out["volume"] > 0).all()            # 거래량 0 봉 제외, 중복 제거
    assert str(out["ts"].dt.tz) == "UTC" and out["ts"].iloc[0] == pd.Timestamp("2026-11-09 00:00", tz="UTC")
    kept = ym.normalize(df, "x", "y", "KR", None, date(2026, 11, 10), drop_zero_volume=False)
    assert len(kept) == 9
    assert ym.normalize(pd.DataFrame(), "x", "y", "KR", None, date(2026, 11, 10)).empty


def test_build_todo_orders_by_oldest_attempt_and_skips_current():
    through = date(2026, 11, 10)
    targets = [("A", "a"), ("B", "b"), ("C", "c"), ("D", "d"), ("E", "e")]
    states = {"A": {"status": "ok", "done": through, "age_sec": 10},                # 최신 → 제외
              "B": {"status": "ok", "done": date(2026, 11, 7), "age_sec": 100},
              "C": {"status": "ok", "done": date(2026, 11, 3), "age_sec": 5000},
              "D": {"status": "empty", "done": None, "age_sec": 3600}}              # 최근 빈 응답 → 잠시 제외
    assert [t[0] for t in ym.build_todo(targets, states, through)] == ["E", "C", "B"]
    states["D"]["age_sec"] = 80 * 3600
    assert [t[0] for t in ym.build_todo(targets, states, through)] == ["E", "D", "C", "B"]


# ------------------------------------------------------------------ 수집 전체 경로
def _register(sid, code, market="KOSPI"):
    from marketlab.storage.catalog import catalog
    with catalog() as con:
        con.execute("INSERT INTO securities (security_id, country, market, security_type, name, status) "
                    "VALUES (?, 'KR', ?, 'common', ?, 'listed')", [sid, market, code])
        con.execute("INSERT INTO symbol_history VALUES (?, 'krx_code', ?, ?, ?, DATE '2020-01-01', NULL, 'test', 'observed')",
                    [sid, code, market, code])


@pytest.fixture
def fast(roomy, monkeypatch):
    roomy.raw.setdefault("sources", {}).setdefault("yfinance_minute", {})["min_interval_sec"] = 0
    monkeypatch.setattr(ym, "yahoo_alive", lambda: (True, "ok"))
    return roomy


def test_collect_no_duplicates_and_resume(fast, monkeypatch):
    import duckdb
    _register("KR-005930-19950502", "005930")
    _register("KR-247540-20190305", "247540", "KOSDAQ")
    _register("KR-999999-20200101", "999999", "KONEX")            # Yahoo 에 없는 시장 → 대상 아님
    calls = []

    def fake_fetch(symbol, start, end, interval="1m"):
        calls.append((symbol, start, end))
        if symbol == "247540.KQ":
            return pd.DataFrame(), "YFPricesMissingError: possibly delisted; no price data found"
        days = [d for d in ("2026-11-06", "2026-11-09", "2026-11-10") if start <= date.fromisoformat(d) < end]
        return (pd.concat([_bars(d) for d in days]) if days else pd.DataFrame()), None

    monkeypatch.setattr(ym, "fetch_minutes", fake_fetch)
    day1 = datetime(2026, 11, 9, 18, 0, tzinfo=KST)
    r = ym.collect("KR", now=day1)
    assert r["ok"] == 1 and r["failed"] == 1 and r["through"] == "2026-11-09"
    assert {c[0] for c in calls} == {"005930.KS", "247540.KQ"}
    assert r["rows"] == 4                                         # 6일·9일 × 거래 있는 봉 2개

    # 다음 날: 받은 날짜 다음부터만 요청하고, 같은 봉을 다시 저장하지 않는다
    calls.clear()
    r2 = ym.collect("KR", now=datetime(2026, 11, 10, 18, 0, tzinfo=KST))
    assert calls == [("005930.KS", date(2026, 11, 10), date(2026, 11, 11))]      # 빈 응답 종목은 72시간 뒤 재확인
    assert r2["rows"] == 2
    files = (ym.raw_dir("KR") / "fetch_date=*" / "*.parquet").as_posix()
    n, distinct = duckdb.sql(f"SELECT count(*), count(DISTINCT (security_id, ts)) FROM read_parquet('{files}')").fetchone()
    assert n == distinct == 6

    # 이미 최신이면 아무것도 요청하지 않는다
    calls.clear()
    assert ym.collect("KR", now=datetime(2026, 11, 10, 19, 0, tzinfo=KST))["ok"] == 0 and calls == []

    # 조회 API
    from marketlab.api import MarketData
    with MarketData() as md:
        bars = md.minute_bars("KR-005930-19950502", "2026-11-09", "2026-11-10")
    assert len(bars) == 4 and bars["ts"].is_monotonic_increasing


def test_collect_time_budget_leaves_rest_for_next_run(fast, monkeypatch):
    for i in range(3):
        _register(f"KR-00000{i}-20200101", f"00000{i}")
    monkeypatch.setattr(ym, "fetch_minutes", lambda s, a, b, interval="1m": (_bars("2026-11-09"), None))
    ticks = iter(range(0, 100000, 40))                            # 시계를 호출마다 40초씩 진행
    monkeypatch.setattr(ym, "_clock", lambda: next(ticks))
    now = datetime(2026, 11, 9, 18, 0, tzinfo=KST)
    r = ym.collect("KR", max_minutes=1, now=now)
    assert r["status"] == "partial" and r["ok"] == 1 and r["remaining"] == 2
    # 남은 종목은 다음 실행에서 이어받는다
    monkeypatch.setattr(ym, "_clock", lambda: 0.0)
    r2 = ym.collect("KR", now=now)
    assert r2["ok"] == 2 and r2["remaining"] == 0


def test_blocked_source_does_not_mark_symbols_empty(fast, monkeypatch):
    from marketlab.collectors import base
    _register("KR-005930-19950502", "005930")
    monkeypatch.setattr(ym, "yahoo_alive", lambda: (False, "ProxyError"))
    r = ym.collect("KR", now=datetime(2026, 11, 9, 18, 0, tzinfo=KST))
    assert r["status"] == "blocked"
    assert base.get_item_states(ym.SOURCE) == {}
