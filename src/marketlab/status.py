"""수집 현황 요약 (CLI status 와 대시보드가 공유)."""
from __future__ import annotations

from datetime import datetime

from .config import get_settings
from .storage.catalog import catalog
from .storage.diskguard import assess, dir_size


def summary() -> dict:
    s = get_settings()
    out: dict = {"generated_at": datetime.now().isoformat(timespec="seconds"), "data_dir": str(s.paths.data)}
    with catalog(read_only=True) as con:
        out["securities"] = con.execute("""
            SELECT country, status, count(*) n FROM securities GROUP BY ALL ORDER BY ALL""").df().to_dict("records")
        out["securities_with_prices"] = con.execute("""
            SELECT country, count(*) n, min(first_trade_date) first, max(last_trade_date) last
            FROM securities WHERE first_trade_date IS NOT NULL GROUP BY 1 ORDER BY 1""").df().to_dict("records")
        out["latest_runs"] = con.execute("""
            SELECT source, status, started_at, finished_at, items_ok, items_failed, rows_written, message
            FROM ingestion_runs QUALIFY row_number() OVER (PARTITION BY source ORDER BY started_at DESC) = 1
            ORDER BY source""").df().to_dict("records")
        out["running"] = con.execute("SELECT run_id, source, started_at, message FROM ingestion_runs WHERE status='running'").df().to_dict("records")
        out["items"] = con.execute("""SELECT source, status, count(*) n FROM ingestion_items GROUP BY ALL ORDER BY ALL""").df().to_dict("records")
        out["alerts"] = con.execute("""SELECT created_at, level, kind, message FROM pending_alerts
                                       WHERE NOT acknowledged ORDER BY created_at DESC LIMIT 20""").df().to_dict("records")
        out["source_checks"] = con.execute("SELECT * FROM source_checks ORDER BY source").df().to_dict("records")
    a = assess()
    out["storage"] = {"free_gb": a.disk_free_gb, "project_gb": a.project_used_gb, "budget_gb": a.budget_gb,
                      "raw_gb": dir_size(s.paths.raw) / 1024 ** 3, "standardized_gb": dir_size(s.paths.standardized) / 1024 ** 3}
    return out


def acknowledge_alerts() -> int:
    with catalog() as con:
        n = con.execute("SELECT count(*) FROM pending_alerts WHERE NOT acknowledged").fetchone()[0]
        con.execute("UPDATE pending_alerts SET acknowledged = TRUE")
    return n
