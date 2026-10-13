"""연구 결과 저장소 (data/metadata/research.duckdb) — 수집 카탈로그와 분리해 잠금 충돌을 피한다.

테이블
- experiments: 실험 설정·데이터 버전·기간·특징·하이퍼파라미터·시드·시간·메모리·성능·파일 경로
- fold_results: walk-forward 단위(fold)별 결과 (중단 시에도 완료분 보존 → 재개)
- predictions: 날짜×종목 예측 (OOS 예측과 최신 예측 모두; model_trained_until 로 시점 구분)
- backtests / backtest_trades: 백테스트 요약·거래 내역
- test_access_log: 최종 검증 구간 사용 기록 (반복 열람 = 독립성 훼손 경고)
- model_registry: 모델 버전, 상태(candidate/approved/retired) — 검증 없이 자동 교체 금지
- games: 과거 시장 게임 저장
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Iterator

import duckdb

from ..config import get_settings
from ..storage.catalog import configure_connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    exp_id VARCHAR PRIMARY KEY, created_at TIMESTAMP, kind VARCHAR, market VARCHAR, target VARCHAR,
    horizon INTEGER, algorithm VARCHAR, params JSON, features JSON, universe JSON, periods JSON,
    data_version JSON, seed INTEGER, n_train BIGINT, n_valid BIGINT, n_test BIGINT,
    train_seconds DOUBLE, peak_rss_gb DOUBLE, metrics JSON, model_path VARCHAR, code_version VARCHAR,
    status VARCHAR, note VARCHAR
);
CREATE TABLE IF NOT EXISTS fold_results (
    run_id VARCHAR, fold INTEGER, market VARCHAR, horizon INTEGER, algorithm VARCHAR, wf_window VARCHAR,
    train_start DATE, train_end DATE, test_start DATE, test_end DATE, n_train BIGINT, n_test BIGINT,
    metrics JSON, finished_at TIMESTAMP, PRIMARY KEY (run_id, fold, algorithm, wf_window)
);
CREATE TABLE IF NOT EXISTS predictions (
    run_id VARCHAR, market VARCHAR, horizon INTEGER, algorithm VARCHAR, security_id VARCHAR, trade_date DATE,
    pred_ret DOUBLE, pred_up DOUBLE, pred_rank DOUBLE, pred_vol DOUBLE, model_trained_until DATE, kind VARCHAR
);
CREATE TABLE IF NOT EXISTS backtests (
    bt_id VARCHAR PRIMARY KEY, created_at TIMESTAMP, market VARCHAR, strategy VARCHAR, params JSON,
    start_date DATE, end_date DATE, costs JSON, constraints JSON, metrics JSON, benchmark JSON,
    selection_period VARCHAR, equity_path VARCHAR, trades_path VARCHAR, note VARCHAR
);
CREATE TABLE IF NOT EXISTS test_access_log (accessed_at TIMESTAMP, who VARCHAR, purpose VARCHAR, ref VARCHAR);
CREATE TABLE IF NOT EXISTS model_registry (
    model_id VARCHAR PRIMARY KEY, created_at TIMESTAMP, market VARCHAR, horizon INTEGER, algorithm VARCHAR,
    trained_until DATE, exp_id VARCHAR, path VARCHAR, status VARCHAR, valid_metrics JSON, note VARCHAR
);
CREATE TABLE IF NOT EXISTS games (
    game_id VARCHAR PRIMARY KEY, updated_at TIMESTAMP, name VARCHAR, state JSON
);
CREATE TABLE IF NOT EXISTS strategy_search_log (
    search_id VARCHAR, strategy_id VARCHAR, market VARCHAR, params JSON, period VARCHAR, metric_name VARCHAR,
    metric_value DOUBLE, n_trades BIGINT, cost_bps DOUBLE, risk JSON, selected BOOLEAN, created_at TIMESTAMP
);
"""


def db_path():
    s = get_settings()
    return s.paths.metadata / s.get("research.db", "research.duckdb")


@contextmanager
def research_db(read_only: bool = False, retries: int = 40) -> Iterator[duckdb.DuckDBPyConnection]:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if read_only and not p.exists():
        read_only = False
    last = None
    for i in range(retries):
        try:
            con = duckdb.connect(p.as_posix(), read_only=read_only)
            break
        except duckdb.IOException as e:
            last = e
            time.sleep(min(0.25 * (i + 1), 3))
    else:
        raise RuntimeError(f"research DB 잠금: {last}")
    try:
        configure_connection(con)
        if not read_only:
            con.execute(SCHEMA)
        yield con
    finally:
        con.close()


def jdump(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)
