"""수집 후 자동 품질 검증과 품질 지표 산출.

행 단위 검사(OHLC 논리, 음수, 급변, 무거래, 기준가 조정)는 표준화 단계에서 quality_flags/등급으로 붙는다.
여기서는 데이터셋 단위 검사를 수행하고 data_quality_issues 와 reports/ 에 결과를 남긴다.

전체 시장의 역사적 종목 수를 알 수 없는 지표는 임의 계산하지 않고 '측정 불가'로 표시한다.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from ..collectors import krx_cache, marcap
from ..config import get_settings
from ..logging_setup import get_logger, log_event
from ..storage.catalog import catalog, memory_connection
from ..storage.diskguard import dir_size, project_usage_bytes
from ..storage.parquet_io import git_blob_sha1, sha256_file

log = get_logger("validate")

REQUIRED_STD_COLUMNS = {
    "security_id": "VARCHAR", "country": "VARCHAR", "symbol": "VARCHAR", "trade_date": "DATE",
    "currency": "VARCHAR", "source": "VARCHAR", "price_basis": "VARCHAR", "open": "DOUBLE", "high": "DOUBLE",
    "low": "DOUBLE", "close": "DOUBLE", "volume": "BIGINT", "is_traded": "BOOLEAN", "is_final": "BOOLEAN",
    "quality_flags": "VARCHAR[]", "quality_grade": "VARCHAR", "usable": "BOOLEAN",
}


def std_glob(country: str) -> str:
    return (get_settings().paths.standardized / "daily_prices" / f"country={country}" / "year=*" / "*.parquet").as_posix()


def has_std(country: str) -> bool:
    return any((get_settings().paths.standardized / "daily_prices" / f"country={country}").glob("year=*/*.parquet"))


def check_file_integrity(deep: bool = False) -> list[dict]:
    """manifest 의 로컬 파일 존재·크기·해시 확인. deep=False 면 크기만, True 면 sha256 재계산."""
    issues = []
    with catalog(read_only=True) as cat:
        rows = cat.execute("SELECT source, file_key, local_path, size_bytes, sha256, remote_sha FROM source_manifest "
                           "WHERE status = 'ok' AND local_path IS NOT NULL").fetchall()
    for src, key, lp, size, sha, rsha in rows:
        p = Path(lp)
        if not p.exists():
            issues.append({"check": "file_missing", "severity": "error", "source": src, "detail": f"{key}: {lp}"})
            continue
        if size is not None and p.stat().st_size != size:
            issues.append({"check": "file_size_mismatch", "severity": "error", "source": src,
                           "detail": f"{key}: 기록 {size}, 실제 {p.stat().st_size}"})
            continue
        if deep:
            if sha and sha256_file(p) != sha:
                issues.append({"check": "file_hash_mismatch", "severity": "error", "source": src, "detail": key})
            elif rsha and src in ("marcap", "krx_cache") and git_blob_sha1(p) != rsha:
                issues.append({"check": "remote_hash_mismatch", "severity": "error", "source": src, "detail": key})
    return issues


def check_schema(con, country: str) -> list[dict]:
    if not has_std(country):
        return []
    desc = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{std_glob(country)}', hive_partitioning=true, "
                       "union_by_name=true)").fetchall()
    cols = {r[0]: r[1] for r in desc}
    out = []
    for c, t in REQUIRED_STD_COLUMNS.items():
        if c not in cols:
            out.append({"check": "schema_missing_column", "severity": "error", "market": country, "detail": c})
        elif cols[c] != t:
            out.append({"check": "schema_type_mismatch", "severity": "error", "market": country,
                        "detail": f"{c}: {cols[c]} (기대 {t})"})
    return out


def check_duplicates(con, country: str) -> list[dict]:
    if not has_std(country):
        return []
    n = con.execute(f"""
        SELECT count(*) FROM (SELECT security_id, trade_date, source, price_basis, count(*) c
        FROM read_parquet('{std_glob(country)}', hive_partitioning=true) GROUP BY ALL HAVING c > 1)""").fetchone()[0]
    return [{"check": "duplicate_rows", "severity": "error", "market": country, "detail": f"{n} 중복 키"}] if n else []


def check_identifier_conflicts(con, country: str) -> list[dict]:
    if not has_std(country):
        return []
    out = []
    n = con.execute(f"""
        SELECT count(*) FROM (SELECT symbol, trade_date FROM read_parquet('{std_glob(country)}', hive_partitioning=true)
        GROUP BY ALL HAVING count(DISTINCT security_id) > 1)""").fetchone()[0]
    if n:
        out.append({"check": "symbol_maps_to_multiple_ids", "severity": "error", "market": country,
                    "detail": f"같은 날 같은 코드가 여러 security_id 에 매핑: {n}건"})
    with catalog(read_only=True) as cat:
        m = cat.execute("""SELECT count(*) FROM (SELECT symbol FROM symbol_history WHERE symbol_type='isin'
                           GROUP BY symbol HAVING count(DISTINCT security_id) > 1)""").fetchone()[0]
    if m:
        out.append({"check": "isin_multiple_ids", "severity": "warn", "market": country, "detail": f"{m} ISIN"})
    return out


def check_missing_days(con, country: str) -> tuple[list[dict], dict]:
    """상장 구간(첫~마지막 관측일) 내 거래일인데 행이 없는 경우. 무거래(거래량 0)와 구분한다."""
    if not has_std(country):
        return [], {}
    with catalog(read_only=True) as cat:
        cal = cat.execute("SELECT trade_date FROM trading_calendars WHERE market=? AND source IN ('both','observed')",
                          [country]).df()
    if cal.empty:
        return [], {"note": "거래일 달력 없음"}
    con.register("cal", cal)
    df = con.execute(f"""
        WITH r AS (SELECT security_id, min(trade_date) f, max(trade_date) l, count(*) n
                   FROM read_parquet('{std_glob(country)}', hive_partitioning=true) GROUP BY 1),
        e AS (SELECT r.security_id, r.n, (SELECT count(*) FROM cal WHERE trade_date BETWEEN r.f AND r.l) AS expected FROM r)
        SELECT security_id, expected, n, expected - n AS missing FROM e
    """).df()
    total_expected = int(df["expected"].sum())
    total_missing = int(df["missing"].clip(lower=0).sum())
    worst = df[df["missing"] > 0].sort_values("missing", ascending=False).head(200)
    issues = [{"check": "missing_trading_days", "severity": "warn", "market": country, "security_id": r.security_id,
               "detail": f"상장 구간 내 결측 {r.missing}일 / 기대 {r.expected}일"} for r in worst.itertuples()]
    return issues, {"expected_rows": total_expected, "missing_rows": total_missing,
                    "missing_rate": (total_missing / total_expected) if total_expected else None,
                    "securities_with_gaps": int((df["missing"] > 0).sum())}


def check_staleness(con, country: str) -> tuple[list[dict], dict]:
    if not has_std(country):
        return [], {}
    s = get_settings()
    last = con.execute(f"SELECT max(trade_date) FROM read_parquet('{std_glob(country)}', hive_partitioning=true)").fetchone()[0]
    with catalog(read_only=True) as cat:
        exp = cat.execute("SELECT max(trade_date) FROM trading_calendars WHERE market=? AND trade_date < current_date",
                          [country]).fetchone()[0]
        lag_sessions = cat.execute("SELECT count(*) FROM trading_calendars WHERE market=? AND trade_date > ? AND trade_date < current_date",
                                   [country, last]).fetchone()[0] if last else None
    info = {"last_data_date": str(last), "last_expected_session": str(exp), "sessions_behind": lag_sessions}
    warn = int(s.get("validation.stale_days_warn", 7))
    if last and (date.today() - last).days > warn:
        return [{"check": "stale_data", "severity": "warn", "market": country,
                 "detail": f"마지막 데이터 {last}, 기대 {exp} ({lag_sessions} 거래일 지연)"}], info
    return [], info


def check_kr_cross_source(con) -> tuple[list[dict], dict]:
    """marcap 종가 vs KRX 현재 상장 스냅샷(같은 날짜) 종가 비교."""
    if not has_std("KR"):
        return [], {"note": "비교 대상 없음"}
    last = con.execute(f"SELECT max(trade_date) FROM read_parquet('{std_glob('KR')}', hive_partitioning=true)").fetchone()[0]
    snaps = sorted((krx_cache.raw_dir() / "listing" / "krx").glob("*.csv"))
    usable = [p for p in snaps if p.stem <= str(last)]
    snap = usable[-1] if usable else (snaps[-1] if snaps else None)
    if not snap:
        return [], {"note": "비교 대상 없음"}
    snap_date = snap.stem
    tol = float(get_settings().get("validation.cross_source_tolerance", 0.005))
    res = con.execute(f"""
        WITH k AS (SELECT lpad(Code, 6, '0') AS symbol, CAST(Close AS DOUBLE) AS close, CAST(Volume AS DOUBLE) AS volume
                   FROM read_csv('{snap.as_posix()}', header=true, all_varchar=true)),
             m AS (SELECT symbol, close, volume, security_id FROM read_parquet('{std_glob("KR")}', hive_partitioning=true)
                   WHERE trade_date = DATE '{snap_date}')
        SELECT count(*) AS both_n,
               sum(CASE WHEN abs(m.close / nullif(k.close, 0) - 1) > {tol} THEN 1 ELSE 0 END) AS close_mismatch,
               sum(CASE WHEN m.volume <> k.volume THEN 1 ELSE 0 END) AS volume_mismatch,
               (SELECT count(*) FROM k) AS krx_n, (SELECT count(*) FROM m) AS marcap_n
        FROM m JOIN k USING (symbol)
    """).fetchone()
    info = {"date": snap_date, "matched": res[0], "close_mismatch": res[1], "volume_mismatch": res[2],
            "krx_listed": res[3], "marcap_rows": res[4]}
    if res[4] == 0:
        info["note"] = f"marcap 에 {snap_date} 데이터가 아직 없음 (원천 갱신 지연) → 비교 불가"
        return [], info
    issues = []
    if res[1]:
        issues.append({"check": "cross_source_close_mismatch", "severity": "warn", "market": "KR",
                       "source": "marcap_vs_krx_listing", "detail": f"{snap_date}: {res[1]}/{res[0]} 종목 종가 불일치"})
    return issues, info


def check_kr_index_consistency(con) -> dict:
    """marcap 전 종목 시총가중 수익률 vs KRX KOSPI 지수 수익률 (원천 간 정합성)."""
    base = get_settings().paths.standardized / "market_indicators"
    a, b = base / "krx_ks11.parquet", base / "kr_breadth.parquet"
    if not (a.exists() and b.exists()):
        return {"note": "지표 없음"}
    r = con.execute(f"""
        WITH k AS (SELECT obs_date, value / lag(value) OVER (ORDER BY obs_date) - 1 AS r FROM read_parquet('{a.as_posix()}')),
             m AS (SELECT obs_date, value AS r FROM read_parquet('{b.as_posix()}') WHERE indicator_id = 'MARCAP:KOSPI_CW_RETURN')
        SELECT count(*), corr(k.r, m.r), avg(abs(k.r - m.r)), quantile_cont(abs(k.r - m.r), 0.99)
        FROM k JOIN m USING (obs_date) WHERE k.r IS NOT NULL AND m.r IS NOT NULL
    """).fetchone()
    return {"days": r[0], "corr": r[1], "mean_abs_diff": r[2], "p99_abs_diff": r[3]}


def check_us_cross_source(con) -> dict:
    """yfinance(분할 반영 종가) vs Stooq(사용자 가져오기/CSV) 종가 비교."""
    st = get_settings().paths.raw / "stooq"
    files = list(st.glob("bulk/*/parts/*.parquet")) + list(st.glob("per_symbol/*.parquet"))
    if not files or not has_std("US"):
        return {"note": "Stooq 데이터 없음 → 원천 간 비교 측정 불가"}
    tol = float(get_settings().get("validation.cross_source_tolerance", 0.005))
    con.execute(f"CREATE OR REPLACE TEMP VIEW stq AS SELECT * FROM read_parquet([{','.join(repr(f.as_posix()) for f in files)}], union_by_name=true)")
    r = con.execute(f"""
        WITH y AS (SELECT lower(replace(symbol, '.', '-')) || '.us' AS stooq_symbol, trade_date, split_adj_close
                   FROM read_parquet('{std_glob("US")}', hive_partitioning=true) WHERE trade_date >= current_date - INTERVAL 3 YEAR)
        SELECT count(*), sum(CASE WHEN abs(y.split_adj_close / nullif(s.close, 0) - 1) > {tol} THEN 1 ELSE 0 END)
        FROM y JOIN stq s USING (stooq_symbol, trade_date)
    """).fetchone()
    return {"compared_rows_3y": r[0], "mismatch_rows": r[1], "mismatch_rate": (r[1] / r[0]) if r[0] else None}


def compute_metrics(con) -> dict:
    s = get_settings()
    m: dict = {"generated_at": datetime.now().isoformat(timespec="seconds")}
    for country in ("KR", "US"):
        if not has_std(country):
            m[country] = {"status": "데이터 없음"}
            continue
        g = std_glob(country)
        base = con.execute(f"""
            SELECT count(*), count(DISTINCT security_id), min(trade_date), max(trade_date),
                   sum(CASE WHEN quality_grade='X' THEN 1 ELSE 0 END), sum(CASE WHEN quality_grade='C' THEN 1 ELSE 0 END),
                   sum(CASE WHEN NOT is_traded THEN 1 ELSE 0 END), sum(CASE WHEN usable THEN 1 ELSE 0 END)
            FROM read_parquet('{g}', hive_partitioning=true)""").fetchone()
        by_market = con.execute(f"""
            SELECT coalesce(market, '?'), count(DISTINCT security_id), count(*) FROM read_parquet('{g}', hive_partitioning=true)
            GROUP BY 1 ORDER BY 3 DESC""").fetchall()
        by_year = con.execute(f"""
            SELECT year(trade_date) y, count(DISTINCT security_id), count(DISTINCT trade_date), count(*),
                   sum(CASE WHEN quality_grade IN ('X','C') THEN 1 ELSE 0 END)
            FROM read_parquet('{g}', hive_partitioning=true) GROUP BY 1 ORDER BY 1""").fetchall()
        with catalog(read_only=True) as cat:
            status = dict(cat.execute("SELECT status, count(*) FROM securities WHERE country=? GROUP BY 1", [country]).fetchall())
        m[country] = {
            "rows": base[0], "securities_with_prices": base[1], "first_date": str(base[2]), "last_date": str(base[3]),
            "grade_x_rows": base[4], "grade_c_rows": base[5], "no_trade_rows": base[6], "usable_rows": base[7],
            "securities_by_status": status,
            "by_market": [{"market": a, "securities": b, "rows": c} for a, b, c in by_market],
            "by_year": [{"year": a, "securities": b, "trading_days": c, "rows": d, "flagged_rows": e} for a, b, c, d, e in by_year],
            "std_bytes": dir_size(Path(g.split("/year=")[0])),
        }
    # 상장폐지 종목 확보 수준
    with catalog(read_only=True) as cat:
        kr_del = cat.execute("""SELECT count(*) FROM securities WHERE country='KR' AND status IN ('delisted','merged')""").fetchone()[0]
        us_del = cat.execute("""SELECT count(*) FROM securities WHERE country='US' AND status='delisted' AND first_trade_date IS NOT NULL""").fetchone()[0]
        src = cat.execute("""SELECT source, status, count(*), sum(rows_written), max(finished_at) FROM ingestion_runs
                             GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
        dq = cat.execute("SELECT market, check_name, severity, count(*) FROM data_quality_issues GROUP BY ALL ORDER BY 4 DESC").fetchall()
    snap = krx_cache.latest_snapshot("delisting")
    kr_del_total = None
    if snap:
        kr_del_total = con.execute(f"""SELECT count(*) FROM read_csv('{snap.as_posix()}', header=true, all_varchar=true)
                                       WHERE try_cast(DelistingDate AS DATE) >= DATE '1995-05-02'""").fetchone()[0]
    m["delisted"] = {
        "KR_securities_with_prices": kr_del,
        "KR_krx_delisting_records_since_1995": kr_del_total,
        "KR_note": "KRX 상장폐지 목록에는 신주·워런트·전환주 등 marcap 미포함 증권도 있어 단순 비율은 상한이 아님",
        "US_securities_with_prices": us_del,
        "US_note": "무료 원천으로 미국 역사적 상장폐지 전체 목록 확보 불가 → 확보율 측정 불가",
    }
    m["ingestion_by_source"] = [{"source": a, "status": b, "runs": c, "rows": d, "last": str(e)} for a, b, c, d, e in src]
    m["quality_issues"] = [{"market": a, "check": b, "severity": c, "count": d} for a, b, c, d in dq]
    m["storage"] = {"project_bytes": project_usage_bytes(s), "raw_bytes": dir_size(s.paths.raw),
                    "standardized_bytes": dir_size(s.paths.standardized), "metadata_bytes": dir_size(s.paths.metadata)}
    m["coverage_rate"] = {
        "KR": "연도별 전체 상장 종목 수(정답)가 무료로 제공되지 않아 측정 불가. 최신일 KRX 스냅샷과의 비교는 cross_source 참고",
        "US": "측정 불가 (역사적 전체 종목 목록 부재)",
    }
    return m


def run(deep: bool = False, run_id: str | None = None) -> dict:
    run_id = run_id or datetime.now().strftime("validate-%Y%m%dT%H%M%S")
    con = memory_connection()
    issues: list[dict] = []
    details: dict = {}
    try:
        issues += check_file_integrity(deep)
        for c in ("KR", "US"):
            issues += check_schema(con, c)
            issues += check_duplicates(con, c)
            issues += check_identifier_conflicts(con, c)
            mi, md = check_missing_days(con, c)
            issues += mi
            details[f"missing_{c}"] = md
            si, sd = check_staleness(con, c)
            issues += si
            details[f"staleness_{c}"] = sd
        ci, cd = check_kr_cross_source(con)
        issues += ci
        details["cross_source_KR"] = cd
        details["kr_index_consistency"] = check_kr_index_consistency(con)
        details["cross_source_US"] = check_us_cross_source(con)
        metrics = compute_metrics(con)
    finally:
        con.close()
    with catalog() as cat:
        cat.execute("DELETE FROM data_quality_issues WHERE source IS NULL OR source NOT IN ('marcap')")
        if issues:
            df = pd.DataFrame([{
                "run_id": run_id, "check_name": i["check"], "severity": i.get("severity"), "market": i.get("market"),
                "security_id": i.get("security_id"), "symbol": None, "trade_date": None, "source": i.get("source", "validator"),
                "detail": i.get("detail"), "resolution": "open"} for i in issues])
            cat.register("vi", df)
            cat.execute("INSERT INTO data_quality_issues SELECT *, now() FROM vi")
    metrics["checks"] = details
    metrics["dataset_issues"] = {"error": sum(1 for i in issues if i.get("severity") == "error"),
                                 "warn": sum(1 for i in issues if i.get("severity") == "warn")}
    write_reports(metrics, issues)
    log_event(log, "validation finished", errors=metrics["dataset_issues"]["error"], warns=metrics["dataset_issues"]["warn"])
    return metrics


def write_reports(metrics: dict, issues: list[dict]) -> None:
    s = get_settings()
    s.paths.reports.mkdir(parents=True, exist_ok=True)
    (s.paths.reports / "quality_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str),
                                                          encoding="utf-8")
    L = ["# 데이터 품질 보고서", "", f"생성: {metrics['generated_at']}", ""]
    for c in ("KR", "US"):
        d = metrics.get(c, {})
        L += [f"## {c}", ""]
        if "rows" not in d:
            L += [f"- {d.get('status', '데이터 없음')}", ""]
            continue
        L += [f"- 행 수 {d['rows']:,} / 가격 보유 증권 {d['securities_with_prices']:,} / 기간 {d['first_date']} ~ {d['last_date']}",
              f"- 사용 가능 행 {d['usable_rows']:,} / 오류(X) {d['grade_x_rows']:,} / 경고(C) {d['grade_c_rows']:,} / 무거래 {d['no_trade_rows']:,}",
              f"- 증권 상태: {d['securities_by_status']}",
              f"- 결측: {metrics['checks'].get('missing_' + c)}",
              f"- 최신성: {metrics['checks'].get('staleness_' + c)}", "",
              "| 시장 | 증권 수 | 행 수 |", "|---|---:|---:|"]
        L += [f"| {x['market']} | {x['securities']:,} | {x['rows']:,} |" for x in d["by_market"]]
        L += ["", "| 연도 | 증권 수 | 거래일 | 행 수 | 플래그 행 |", "|---|---:|---:|---:|---:|"]
        L += [f"| {x['year']} | {x['securities']:,} | {x['trading_days']} | {x['rows']:,} | {x['flagged_rows']:,} |" for x in d["by_year"]]
        L += [""]
    L += ["## 원천 간 비교", "", f"- KR marcap vs KRX 스냅샷: {metrics['checks'].get('cross_source_KR')}",
          f"- KR marcap 시총가중 수익률 vs KOSPI 지수: {metrics['checks'].get('kr_index_consistency')}",
          f"- US yfinance vs Stooq: {metrics['checks'].get('cross_source_US')}", "",
          "## 상장폐지 종목", "", f"- {metrics['delisted']}", "",
          "## 확보율", "", f"- {metrics['coverage_rate']}", "",
          "## 데이터셋 단위 이슈", ""]
    L += [f"- [{i.get('severity')}] {i['check']} {i.get('market') or ''} {i.get('security_id') or ''}: {i.get('detail')}" for i in issues[:100]]
    if len(issues) > 100:
        L += [f"- ... 외 {len(issues) - 100}건 (catalog.data_quality_issues 참고)"]
    L += ["", "## 품질 이슈 집계 (행 단위 포함)", "", "| 시장 | 검사 | 심각도 | 건수 |", "|---|---|---|---:|"]
    L += [f"| {q['market']} | {q['check']} | {q['severity']} | {q['count']:,} |" for q in metrics["quality_issues"][:40]]
    (s.paths.reports / "quality_report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
