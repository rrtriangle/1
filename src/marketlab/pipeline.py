"""수집 → 표준화 → 검증 오케스트레이션. 원천 하나가 실패해도 나머지는 계속 진행한다."""
from __future__ import annotations

import json
import socket
import traceback
from datetime import datetime
from typing import Callable

from .collectors import base, indicators, krx_cache, marcap, nasdaqtrader, opendart, sec_edgar, yfinance_us
from .config import get_settings
from .logging_setup import get_logger, log_event
from .storage.catalog import init_catalog
from .storage.diskguard import StorageRisk, StorageUnavailable, raise_alert
from .transforms import kr_breadth, kr_prices, us_prices
from .validation import checks

log = get_logger("pipeline")


def network_available(host: str = "raw.githubusercontent.com", port: int = 443, timeout: float = 5.0) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        # 프록시 환경 등에서 직접 연결이 막힐 수 있으므로 HTTP 로 한 번 더 확인
        try:
            import requests
            return requests.head(f"https://{host}", timeout=timeout).status_code < 500
        except Exception:
            return False


def _step(summary: dict, name: str, fn: Callable, *a, **kw):
    t0 = datetime.now()
    try:
        res = fn(*a, **kw)
        summary["steps"].append({"step": name, "ok": True, "result": res, "sec": (datetime.now() - t0).seconds})
        return res
    except (StorageRisk, StorageUnavailable):
        summary["steps"].append({"step": name, "ok": False, "error": "storage", "sec": (datetime.now() - t0).seconds})
        raise
    except base.StopRequested as e:
        summary["steps"].append({"step": name, "ok": False, "error": f"stopped: {e}"})
        raise
    except Exception as e:
        log_event(log, "step failed", step=name, error=str(e), tb=traceback.format_exc()[-2000:])
        summary["steps"].append({"step": name, "ok": False, "error": str(e)[:500], "sec": (datetime.now() - t0).seconds})
        return None


def _save_summary(summary: dict, name: str) -> None:
    s = get_settings()
    s.paths.reports.mkdir(parents=True, exist_ok=True)
    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    (s.paths.reports / f"{name}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def ingest_kr(mode: str = "full", summary: dict | None = None) -> dict:
    summary = summary if summary is not None else {"steps": []}
    m = _step(summary, "marcap", marcap.collect, mode="full" if mode == "full" else "incremental")
    _step(summary, "krx_cache", krx_cache.collect, mode="incremental")
    years = None if mode == "full" else (m or {}).get("changed_years", [])
    if mode != "full" and not years:
        years = []  # 원본 변화 없음 → 마스터·지표만 갱신
    _step(summary, "kr_standardize", lambda: _compact(kr_prices.run(years=years)))
    _step(summary, "kr_breadth", kr_breadth.run)
    _step(summary, "krx_indices", indicators.build_krx_indices)
    _step(summary, "opendart_corp_codes", opendart.collect_corp_codes)
    return summary


def ingest_us(mode: str = "full", summary: dict | None = None, limit: int | None = None,
              symbols: list[str] | None = None, revalidate: bool = False) -> dict:
    summary = summary if summary is not None else {"steps": []}
    _step(summary, "nasdaqtrader", nasdaqtrader.collect)
    _step(summary, "sec_tickers", sec_edgar.collect_tickers)
    _step(summary, "yfinance", yfinance_us.collect, mode=mode, limit=limit, symbols=symbols, revalidate=revalidate)
    _step(summary, "us_standardize", lambda: _compact(us_prices.run()))
    return summary


def ingest_indicators(summary: dict | None = None) -> dict:
    summary = summary if summary is not None else {"steps": []}
    _step(summary, "fred", indicators.collect_fred)
    _step(summary, "yahoo_indices", indicators.collect_yahoo_indices)
    _step(summary, "krx_indices", indicators.build_krx_indices)
    return summary


def _compact(res: dict) -> dict:
    res = dict(res or {})
    if "per_year" in res:
        res["per_year"] = f"{len(res['per_year'])} years"
    return res


def run_job(kind: str, market: str | None = None, mode: str = "incremental", **kw) -> dict:
    """CLI·launchd·대시보드 공통 진입점. 중복 실행 잠금, 중단 플래그, 경고 처리를 담당."""
    init_catalog()
    get_settings().paths.ensure()
    summary: dict = {"kind": kind, "market": market, "mode": mode, "started_at": datetime.now().isoformat(timespec="seconds"),
                     "steps": []}
    with base.exclusive_lock("ingest"):
        base.clear_stop()
        base.mark_stale_runs()
        if kind in ("ingest", "update") and not network_available():
            summary["skipped"] = "네트워크 연결 없음"
            _save_summary(summary, "last_" + kind)
            return summary
        try:
            if kind == "ingest":
                if market in ("KR", "ALL"):
                    ingest_kr(mode, summary)
                if market in ("US", "ALL"):
                    ingest_us(mode, summary, limit=kw.get("limit"), symbols=kw.get("symbols"))
                if market in ("IND", "ALL"):
                    ingest_indicators(summary)
            elif kind == "update":
                ingest_kr("incremental", summary)
                ingest_us("incremental", summary, revalidate=True)
                ingest_indicators(summary)
            elif kind == "retry":
                _step(summary, "yfinance_retry", yfinance_us.collect, mode="incremental", retry_failed_only=True)
                _step(summary, "us_standardize", lambda: _compact(us_prices.run()))
            summary["validation"] = _step(summary, "validate", lambda: {
                k: v for k, v in checks.run(deep=kw.get("deep", False)).items() if k in ("dataset_issues", "checks")})
        except (StorageRisk, StorageUnavailable) as e:
            summary["storage_error"] = str(e)
            raise_alert("error", "storage", f"저장공간/저장장치 문제로 중단: {e}")
        except base.StopRequested:
            summary["stopped"] = True
        finally:
            _check_staleness_alert(summary)
            _save_summary(summary, "last_" + kind)
    return summary


def _check_staleness_alert(summary: dict) -> None:
    v = (summary.get("validation") or {}).get("checks", {}) if isinstance(summary.get("validation"), dict) else {}
    for c in ("KR", "US"):
        st = v.get(f"staleness_{c}") or {}
        behind = st.get("sessions_behind")
        if isinstance(behind, int) and behind >= 3:
            raise_alert("warn", "stale", f"{c} 데이터가 {behind} 거래일 지연됨 (마지막 {st.get('last_data_date')})")
