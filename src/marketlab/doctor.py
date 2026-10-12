"""환경·자원 점검(doctor)과 데이터 원천 접근 점검(sources)."""
from __future__ import annotations

import importlib.metadata as md
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import psutil

from .config import get_settings
from .storage.diskguard import GB, assess, ensure_writable, is_external_volume, project_usage_bytes

PACKAGES = ["duckdb", "pyarrow", "pandas", "numpy", "requests", "finance-datareader", "yfinance",
            "exchange-calendars", "streamlit", "plotly", "psutil", "PyYAML", "pytest"]


def _sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def doctor() -> dict:
    s = get_settings()
    info: dict = {"checked_at": datetime.now().isoformat(timespec="seconds")}
    info["os"] = {"system": platform.system(), "release": platform.release(),
                  "macos": platform.mac_ver()[0] or None, "machine": platform.machine()}
    if platform.system() == "Darwin":
        info["os"]["rosetta_translated"] = _sh(["sysctl", "-n", "sysctl.proc_translated"]) == "1"
        info["os"]["cpu"] = _sh(["sysctl", "-n", "machdep.cpu.brand_string"])
    info["python"] = {"version": sys.version.split()[0], "executable": sys.executable,
                      "in_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix)}
    pk = {}
    for p in PACKAGES:
        try:
            pk[p] = md.version(p)
        except md.PackageNotFoundError:
            pk[p] = None
    info["packages"] = pk
    info["tools"] = {t: shutil.which(t) for t in ("git", "brew", "uv", "launchctl", "osascript")}
    vm = psutil.virtual_memory()
    info["memory"] = {"total_gb": vm.total / GB, "available_gb": vm.available / GB, "percent": vm.percent,
                      "swap_used_gb": psutil.swap_memory().used / GB}
    info["cpu"] = {"logical": psutil.cpu_count(), "load_percent": psutil.cpu_percent(interval=0.5)}
    a = assess()
    info["disk"] = {"data_dir": str(s.paths.data), "external": is_external_volume(s.paths.data),
                    "total_gb": a.disk_total_gb, "free_gb": a.disk_free_gb, "project_used_gb": a.project_used_gb,
                    "effective_budget_gb": a.budget_gb, "min_free_gb": a.min_free_gb}
    try:
        ensure_writable(s.paths.data)
        info["disk"]["writable"] = True
    except Exception as e:
        info["disk"]["writable"] = False
        info["disk"]["error"] = str(e)
    vols = []
    if Path("/Volumes").exists():
        for v in Path("/Volumes").iterdir():
            try:
                u = shutil.disk_usage(v)
                vols.append({"name": v.name, "total_gb": u.total / GB, "free_gb": u.free / GB,
                             "external": is_external_volume(v)})
            except OSError:
                pass
    info["volumes"] = vols
    info["project"] = {"root": str(s.paths.root), "config": str(s.config_path),
                       "existing": sorted(p.name for p in s.paths.root.iterdir() if not p.name.startswith("."))}
    warnings = []
    if info["os"]["system"] == "Darwin" and info["os"]["machine"] != "arm64":
        warnings.append("Apple Silicon 네이티브(arm64) Python 이 아닙니다 (Rosetta). arm64 Python 사용 권장.")
    if info["os"]["system"] != "Darwin":
        warnings.append("macOS 가 아닙니다. launchd 자동 갱신·macOS 알림은 사용할 수 없습니다.")
    if not info["python"]["in_venv"]:
        warnings.append("가상환경이 아닌 Python 으로 실행 중입니다. scripts/setup.command 로 .venv 를 만드세요.")
    missing = [p for p, v in pk.items() if v is None and p != "pytest"]
    if missing:
        warnings.append(f"미설치 패키지: {missing}")
    if info["memory"]["total_gb"] < 7.5:
        warnings.append("메모리 8GB 미만: duckdb.memory_limit 를 1GB 로 낮추세요.")
    if info["memory"]["available_gb"] < 2.5:
        warnings.append(f"사용 가능 메모리 {info['memory']['available_gb']:.1f}GB: 다른 앱을 닫은 뒤 대량 작업을 실행하세요.")
    if a.risk or a.disk_free_gb < a.min_free_gb + 5:
        warnings.append(f"여유 공간 {a.disk_free_gb:.1f}GB: 예산 여유가 작습니다. disk-plan 을 확인하세요.")
    info["warnings"] = warnings
    return info


def sources() -> list[dict]:
    """각 원천의 실제 접근 가능 여부 (작은 요청만, 우회 없음)."""
    from .collectors.base import record_source_check
    from .net.http import HttpClient

    s = get_settings()
    probes = [
        ("marcap (raw.githubusercontent)", "marcap", f"{s.source('marcap')['raw_base']}/master/README.md", None),
        ("marcap (GitHub API)", "marcap", "https://api.github.com/repos/FinanceData/marcap", None),
        ("fdr_krx_data_cache", "krx_cache", f"{s.source('krx_cache')['raw_base']}/master/README.md", None),
        ("KRX 정보데이터시스템", "krx", "https://data.krx.co.kr/", None),
        ("Nasdaq Trader", "nasdaqtrader", s.source("nasdaqtrader")["urls"]["nasdaqlisted"], None),
        ("Yahoo Finance chart API", "yfinance", "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=5d", None),
        ("Stooq CSV", "stooq", s.source("stooq")["csv_url"].format(symbol="aapl.us"), None),
        ("FRED CSV", "fred", s.source("fred")["csv_url"].format(series="DGS10"), None),
        ("SEC EDGAR", "sec", s.source("sec")["tickers_url"],
         {"User-Agent": os.environ.get("MARKETLAB_SEC_USER_AGENT") or "market-research-lab contact@example.com"}),
        ("OpenDART", "opendart", "https://opendart.fss.or.kr/", None),
    ]
    out = []
    for label, key, url, headers in probes:
        c = HttpClient(key, min_interval_sec=0.2, headers=headers)
        c.max_retries = 1
        try:
            r = c.get(url, stream=True)
            head = next(r.iter_content(400), b"")
            r.close()
            ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
            if key == "stooq" and ok and not head.lower().startswith(b"date"):
                ok, detail = False, f"HTTP 200 이나 CSV 아님 (API 키/확인 요구 가능): {head[:80]!r}"
            if key == "yfinance" and ok and b"chart" not in head:
                ok, detail = False, "예상 형식 아님"
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {str(e)[:150]}"
        record_source_check(label, ok, None, detail)
        out.append({"source": label, "reachable": ok, "detail": detail, "url": url})
    return out
