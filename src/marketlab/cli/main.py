"""marketlab 명령행 도구.

    marketlab doctor                       환경·자원 확인
    marketlab sources                      데이터 원천 접근 확인
    marketlab disk-plan [--with-sec]       저장공간 계획 / 외장 SSD 판단
    marketlab ingest --market KR --mode full
    marketlab ingest --market US --mode full [--limit N] [--symbols AAPL MSFT]
    marketlab ingest --market IND          지수·환율·금리
    marketlab update --all                 증분 갱신 (launchd 가 호출)
    marketlab retry                        실패 종목 재시도
    marketlab stop                         진행 중인 수집 중지 요청
    marketlab validate [--deep]            품질 검증 + reports/quality_report.md
    marketlab status                       수집 현황
    marketlab dashboard                    관리 화면 (Streamlit)
    marketlab schedule install|uninstall|status [--times 07:30 19:30]
    marketlab import-stooq <zip>           사용자가 받은 Stooq 대량 파일 가져오기
    marketlab sec --facts --approve-large  SEC 재무정보 대량 수집 (승인 필요)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _alerts_banner() -> None:
    """보류된 경고를 실행 시작 시 다시 보여준다 (Mac 이 꺼져 있어 알림을 못 받은 경우 대비)."""
    try:
        from ..storage.catalog import catalog
        with catalog(read_only=True) as con:
            rows = con.execute("SELECT created_at, level, message FROM pending_alerts WHERE NOT acknowledged "
                               "ORDER BY created_at DESC LIMIT 5").fetchall()
        for t, lvl, msg in rows:
            print(f"[보류 경고 {lvl}] {t:%Y-%m-%d %H:%M} {msg}", file=sys.stderr)
        if rows:
            print("  (확인 처리: marketlab status --ack)", file=sys.stderr)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="marketlab", description="한국·미국 주식 데이터 수집·관리")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("sources")
    dp = sub.add_parser("disk-plan")
    dp.add_argument("--with-sec", action="store_true", help="SEC 대량 재무 파일 포함 계산")
    dp.add_argument("--stooq-gb", type=float, default=0.0, help="가져올 Stooq zip 크기(GB)")
    ig = sub.add_parser("ingest")
    ig.add_argument("--market", required=True, choices=["KR", "US", "IND", "ALL"])
    ig.add_argument("--mode", default="full", choices=["full", "incremental"])
    ig.add_argument("--limit", type=int, help="(US) 이번 실행에서 처리할 최대 종목 수 — 나머지는 다음 실행에서 재개")
    ig.add_argument("--symbols", nargs="*", help="(US) 특정 티커만")
    up = sub.add_parser("update")
    up.add_argument("--all", action="store_true", required=True)
    up.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("retry")
    sub.add_parser("stop")
    va = sub.add_parser("validate")
    va.add_argument("--deep", action="store_true", help="모든 파일 해시 재계산")
    st = sub.add_parser("status")
    st.add_argument("--ack", action="store_true", help="보류 경고 확인 처리")
    db = sub.add_parser("dashboard")
    db.add_argument("--port", type=int, default=8501)
    sc = sub.add_parser("schedule")
    sc.add_argument("action", choices=["install", "uninstall", "status"])
    sc.add_argument("--times", nargs="*")
    isq = sub.add_parser("import-stooq")
    isq.add_argument("zip_path")
    se = sub.add_parser("sec")
    se.add_argument("--tickers", action="store_true")
    se.add_argument("--facts", action="store_true")
    se.add_argument("--submissions", action="store_true")
    se.add_argument("--approve-large", action="store_true")
    sub.add_parser("standardize", help="원본으로부터 표준화 데이터 전체 재생성")
    args = p.parse_args(argv)

    from ..config import get_settings
    from ..logging_setup import setup_logging
    setup_logging()
    get_settings().paths.ensure()
    if args.cmd not in ("dashboard",):
        _alerts_banner()

    if args.cmd == "doctor":
        from ..doctor import doctor
        _print(doctor())
    elif args.cmd == "sources":
        from ..doctor import sources
        rows = sources()
        for r in rows:
            print(f"{'OK ' if r['reachable'] else 'NO '} {r['source']:<32} {r['detail']}")
    elif args.cmd == "disk-plan":
        from ..storage.diskplan import plan
        r = plan(include_sec=args.with_sec, include_stooq_bulk_gb=args.stooq_gb)
        print(Path(get_settings().paths.reports / "disk_plan.md").read_text(encoding="utf-8"))
        return 2 if r["assessment"]["risk"] else 0
    elif args.cmd in ("ingest", "update", "retry"):
        from ..collectors.base import AlreadyRunning
        from ..pipeline import run_job
        try:
            if args.cmd == "ingest":
                r = run_job("ingest", market=args.market, mode=args.mode, limit=args.limit, symbols=args.symbols)
            elif args.cmd == "update":
                r = run_job("update", market="ALL", mode="incremental")
            else:
                r = run_job("retry")
        except AlreadyRunning as e:
            print(str(e), file=sys.stderr)
            return 3
        for s_ in r.get("steps", []):
            print(f"{'OK ' if s_['ok'] else 'ERR'} {s_['step']:<22} {json.dumps(s_.get('result') if s_['ok'] else s_.get('error'), ensure_ascii=False, default=str)[:300]}")
        if r.get("skipped"):
            print("건너뜀:", r["skipped"])
        if r.get("storage_error"):
            print("저장공간 문제로 중단:", r["storage_error"], "→ reports/storage_advice.md", file=sys.stderr)
            return 2
    elif args.cmd == "stop":
        from ..collectors.base import request_stop
        request_stop()
        print("중지 요청을 기록했습니다. 진행 중인 작업이 현재 항목을 마친 뒤 안전하게 멈춥니다.")
    elif args.cmd == "validate":
        from ..validation.checks import run
        m = run(deep=args.deep)
        print(Path(get_settings().paths.reports / "quality_report.md").read_text(encoding="utf-8")[:6000])
        return 1 if m["dataset_issues"]["error"] else 0
    elif args.cmd == "status":
        from ..status import acknowledge_alerts, summary
        if args.ack:
            print(f"{acknowledge_alerts()}건 확인 처리")
        _print(summary())
    elif args.cmd == "dashboard":
        app = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
        return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(args.port),
                                "--server.address", "127.0.0.1", "--browser.gatherUsageStats", "false"])
    elif args.cmd == "schedule":
        from ..scheduling import launchd
        if args.action == "install":
            print("설치:", launchd.install(args.times))
        elif args.action == "uninstall":
            print("제거:", launchd.uninstall())
        else:
            _print(launchd.status())
    elif args.cmd == "import-stooq":
        from ..collectors.stooq import import_zip
        _print(import_zip(Path(args.zip_path)))
    elif args.cmd == "sec":
        from ..collectors import sec_edgar
        if args.tickers or not (args.facts or args.submissions):
            _print(sec_edgar.collect_tickers())
        if args.submissions:
            _print(sec_edgar.build_submissions(approve_large=args.approve_large))
        if args.facts:
            _print(sec_edgar.build_companyfacts(approve_large=args.approve_large))
    elif args.cmd == "standardize":
        from ..transforms import kr_breadth, kr_prices, us_prices
        r = kr_prices.run()
        r["per_year"] = len(r.get("per_year", []))
        _print(r)
        _print(kr_breadth.run())
        u = us_prices.run(years=None)
        _print({k: v for k, v in u.items() if k != "per_year"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
