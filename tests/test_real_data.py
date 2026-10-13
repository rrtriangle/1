"""실제 수집 데이터로 완성 프로그램을 검증 (지시서 17절). 데이터가 없으면 건너뛴다."""
from datetime import date

import pyarrow.parquet as pq
import pytest

from marketlab.config import get_settings

pytestmark = pytest.mark.data


@pytest.fixture(scope="module")
def md():
    get_settings.cache_clear()
    from marketlab.api import MarketData
    m = MarketData()
    yield m
    m.close()


def test_marcap_old_and_recent_years_load():
    from marketlab.collectors import marcap
    for y in (1995, date.today().year):
        p = marcap.local_path(y)
        assert p.exists(), p
        pf = pq.ParquetFile(p)
        assert pf.metadata.num_rows > 100_000
        assert {"Code", "Close", "Date", "Marcap"} <= set(pf.schema_arrow.names)


def test_raw_vs_standardized_row_consistency():
    """원본 연도 파일 행 수 = 표준화 행 수 + 중복 제거 수."""
    import duckdb
    from marketlab.collectors import marcap
    from marketlab.transforms.kr_prices import std_dir
    con = duckdb.connect()
    for y in (1995, 2008, date.today().year):
        raw = con.execute(f"SELECT count(DISTINCT (lpad(CAST(Code AS VARCHAR),6,'0'), CAST(Date AS DATE))) "
                          f"FROM read_parquet('{marcap.local_path(y)}')").fetchone()[0]
        std = con.execute(f"SELECT count(*) FROM read_parquet('{std_dir()}/year={y}/*.parquet')").fetchone()[0]
        assert raw == std, (y, raw, std)


def test_kr_price_lookup(md):
    sid = md.resolve("005930", "2020-01-02", "KR")
    assert sid == "KR-005930-19950502"
    px = md.prices(sid, "2020-01-02", "2020-01-03")
    assert list(px["close"]) == [55200.0, 55500.0]   # 삼성전자 실제 종가
    assert (px["price_basis"] == "raw").all()


def test_kr_split_point_in_time(md):
    sid = "KR-005930-19950502"
    after = md.prices(sid, "2018-04-27", "2018-05-04", adjust="split", as_of="2018-05-04")
    before = md.prices(sid, "2018-04-27", "2018-04-27", adjust="split", as_of="2018-04-27")
    assert after.iloc[0]["close"] == pytest.approx(53000.0)     # 50:1 분할 반영
    assert before.iloc[0]["close"] == pytest.approx(2650000.0)  # 분할 전 시점엔 미반영


def test_delisted_and_code_reuse(md):
    # 000030: 상업/한빛은행(1995~2002) 과 우리은행(2014~2019) 은 다른 증권
    old = md.resolve("000030", "2000-01-04", "KR")
    new = md.resolve("000030", "2016-01-04", "KR")
    assert old != new and new == "KR-000030-20141119"
    assert md.listing_status(new, "2020-01-02")["status_as_of"] == "delisted"
    assert md.listing_status(new, "2016-01-04")["status_as_of"] == "listed"
    u = md.universe("2008-10-24", "KR")
    assert len(u) > 1500
    from marketlab.storage.catalog import catalog
    with catalog(read_only=True) as con:
        assert con.execute("SELECT count(*) FROM securities WHERE country='KR' AND status='delisted'").fetchone()[0] > 1500


def test_index_and_breadth(md):
    ind = md.indicators(["KRX:KS11", "MARCAP:KOSPI_CW_RETURN"], "2008-10-24", "2008-10-24")
    v = dict(zip(ind["indicator_id"], ind["value"]))
    assert v["KRX:KS11"] == pytest.approx(938.75, abs=0.01)
    assert v["MARCAP:KOSPI_CW_RETURN"] == pytest.approx(-0.1057, abs=0.002)
    # 시점 제한: 장 마감+지연 전에는 그날 값이 보이지 않는다
    from datetime import datetime
    from zoneinfo import ZoneInfo
    early = md.indicators(["KRX:KS11"], "2008-10-24", "2008-10-24", as_of=datetime(2008, 10, 24, 12, 0, tzinfo=ZoneInfo("Asia/Seoul")))
    assert early.empty


def test_no_duplicates_and_quality_columns():
    import duckdb
    from marketlab.transforms.kr_prices import std_dir
    con = duckdb.connect()
    n = con.execute(f"SELECT count(*) FROM (SELECT security_id, trade_date FROM read_parquet('{std_dir()}/year=*/*.parquet') "
                    "GROUP BY ALL HAVING count(*) > 1)").fetchone()[0]
    assert n == 0
    g = con.execute(f"SELECT DISTINCT quality_grade FROM read_parquet('{std_dir()}/year=*/*.parquet')").fetchall()
    assert {x[0] for x in g} <= {"A", "B", "C", "X"}


def test_incremental_is_idempotent():
    """원격 변경이 없으면 다시 받을 파일이 없다 (중복 수집 방지)."""
    from marketlab.collectors import marcap, base
    p = marcap.plan()
    have = base.manifest_entries("marcap")
    # 원격에서 오늘 갱신된 현재 연도 외에는 받을 것이 없어야 한다
    assert set(p["todo"]) <= {date.today().year}
    assert len(have) >= 30


def test_duckdb_memory_settings_applied():
    from marketlab.storage.catalog import memory_connection
    con = memory_connection()
    assert con.execute("SELECT current_setting('threads')").fetchone()[0] == 2
    lim = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    assert lim.endswith("GiB") or lim.endswith("GB") or "MiB" in lim
