"""미래정보 누출 검사 (실제 데이터).

특정 날짜 T 이후의 원천 데이터를 아예 보지 않고 다시 계산한 특징이, 전체 데이터로 계산해 저장한 특징의 T일 값과
같아야 한다. 다르면 특징 계산에 미래 데이터가 섞인 것이다.
"""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from marketlab.research import features as F
from marketlab.storage.catalog import memory_connection

pytestmark = pytest.mark.data


def _has_features():
    return any(F.features_dir("KR").glob("year=*/*.parquet"))


@pytest.mark.skipif(not _has_features(), reason="특징 파일 없음")
@pytest.mark.parametrize("cut", [date(2015, 6, 30), date(2008, 10, 24)])
def test_features_identical_when_future_removed(tmp_path, cut):
    con = memory_connection()
    try:
        F.build_market_env(con, "KR")
        F.build_year(con, "KR", cut.year, cutoff=cut, out_path=tmp_path / "cut.parquet")
        a = con.execute(f"SELECT * FROM read_parquet('{tmp_path / 'cut.parquet'}') WHERE trade_date = DATE '{cut}'").df()
        b = con.execute(f"SELECT * FROM read_parquet('{F.feature_glob('KR')}') WHERE trade_date = DATE '{cut}'").df()
    finally:
        con.close()
    assert len(a) > 500 and len(a) == len(b)
    a, b = a.set_index("security_id").sort_index(), b.set_index("security_id").sort_index()
    # 순위 특징은 같은 날 투자가능 종목 집합에 의존 → 집합이 같으면 같아야 함
    for c in F.FEATURES:
        x, y = a[c].to_numpy(dtype=float), b[c].to_numpy(dtype=float)
        ok = np.isclose(x, y, rtol=1e-4, atol=1e-6, equal_nan=True)
        assert ok.mean() == 1.0, f"{c}: {(~ok).sum()}개 불일치 → 미래 데이터 유입 의심"
    # 라벨은 미래가 필요하므로 cutoff 계산본에서는 비어 있어야 한다
    assert a["y_ret_5"].isna().all()


@pytest.mark.skipif(not _has_features(), reason="특징 파일 없음")
def test_label_definition_next_open_to_close():
    """y_ret_5(t) = close(t+5) / open(t+1) − 1 (기업행위 없는 구간, 삼성전자 2008-10-20)."""
    con = memory_connection()
    try:
        f = con.execute(f"SELECT y_ret_5 FROM read_parquet('{F.feature_glob('KR')}') WHERE security_id='KR-005930-19950502' AND trade_date='2008-10-20'").fetchone()[0]
    finally:
        con.close()
    assert f == pytest.approx(438000 / 526000 - 1, rel=1e-5)


def test_purge_cutoff_removes_overlapping_labels():
    from marketlab.research.dataset import purge_cutoff, trading_days
    b = date(2021, 1, 4)
    c = purge_cutoff("KR", b, 20)
    days = [d for d in trading_days("KR") if c < d < b]
    assert len(days) >= 21   # 마지막 학습 행의 라벨(t+1~t+20)이 경계 이전에 끝남
