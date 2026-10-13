"""KRX 보완 원천: FinanceData/fdr_krx_data_cache (FinanceDataReader 가 내부적으로 쓰는 KRX 캐시).

data.krx.co.kr 는 로그인 요구로 자동 수집이 제한되므로(우회하지 않음),
FinanceDataReader 저자가 공개 저장소에 매일 적재하는 KRX 원본 CSV 를 사용한다.

- listing/krx/<date>.csv      : 현재 상장 전 종목 (ISIN 포함)
- listing/desc/<date>.csv     : 상장회사 상세 (업종, 상장일, 결산월)
- listing/delisting/<date>.csv: 상장폐지 종목 전체 이력 (1956~, 사유·이전 종목 포함)
- index/year_<idx>/<year>.csv : KOSPI/KOSDAQ/KOSPI200 일별 지수 (1995~)
원본 CSV 는 그대로 보존하고 Parquet 표준화는 transforms 에서 한다.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..config import get_settings
from ..logging_setup import get_logger, log_event
from ..net.http import HttpClient, SourceBlocked
from ..storage.diskguard import DiskGuard, StorageRisk
from ..storage.parquet_io import git_blob_sha1, sha256_file
from .base import Run, StopRequested, manifest_entries, record_manifest, stop_requested
from .github_repo import RemoteFile, list_repo_files

log = get_logger("krx_cache")
SOURCE = "krx_cache"
SNAPSHOT_RE = re.compile(r"^data/listing/(krx|desc|delisting)/(\d{4}-\d{2}-\d{2})\.csv$")
INDEX_RE = re.compile(r"^data/index/year_([a-z0-9_]+)/(\d{4})\.csv$")


def raw_dir() -> Path:
    return get_settings().paths.raw / "krx_cache"


def select_targets(files: list[RemoteFile], indices: list[str], keep_listing_snapshots: int = 7) -> list[RemoteFile]:
    """지수 연도 파일 전부 + 목록류 최신 스냅샷.

    현재 상장 목록(krx)은 최근 여러 날을 받아 marcap 갱신 지연이 있어도 같은 날짜끼리 교차검증한다.
    """
    snaps: dict[str, list[tuple[str, RemoteFile]]] = {}
    out: list[RemoteFile] = []
    for f in files:
        m = SNAPSHOT_RE.match(f.path)
        if m:
            kind, d = m.groups()
            snaps.setdefault(kind, []).append((d, f))
            continue
        m = INDEX_RE.match(f.path)
        if m and m.group(1) in indices:
            out.append(f)
    for kind, lst in snaps.items():
        lst.sort(key=lambda x: x[0])
        keep = keep_listing_snapshots if kind == "krx" else 1
        out.extend(f for _, f in lst[-keep:])
    return out


def local_for(remote_path: str) -> Path:
    return raw_dir() / remote_path.removeprefix("data/")


def collect(mode: str = "incremental") -> dict:
    s = get_settings()
    cfg = s.source(SOURCE)
    client = HttpClient(SOURCE)
    run = Run(SOURCE, "KR", mode).start()
    fetched: list[str] = []
    try:
        commit, files, method = list_repo_files(cfg["repo"], cfg.get("branch", "master"), client)
        targets = select_targets(files, cfg.get("indices", ["ks11", "kq11", "ks200"]))
        have = manifest_entries(SOURCE)
        todo = []
        for f in targets:
            lp = local_for(f.path)
            h = have.get(f.path)
            if mode != "full" and h and h["remote_sha"] == f.sha and lp.exists():
                continue
            todo.append(f)
        run.items_total = len(todo)
        run.message = f"listing via {method}; {len(todo)} files"
        run.progress()
        guard = DiskGuard("KRX 캐시 다운로드", planned_additional_bytes=len(todo) * 700 * 1024)
        guard.preflight()
        for i, f in enumerate(todo):
            if stop_requested():
                raise StopRequested("사용자 중지 요청")
            if i % 20 == 0:
                guard.checkpoint()
            url = f"{cfg['raw_base']}/{cfg.get('branch', 'master')}/{f.path}"
            lp = local_for(f.path)
            try:
                n = client.download(url, lp)
                if git_blob_sha1(lp) != f.sha:
                    raise IOError("blob sha 불일치")
                record_manifest(SOURCE, f.path, url, f.sha, lp, n, sha256_file(lp), None, commit, "ok")
                fetched.append(f.path)
                run.items_ok += 1
                run.bytes_written += n
            except (SourceBlocked, StorageRisk):
                raise
            except Exception as e:
                run.items_failed += 1
                record_manifest(SOURCE, f.path, url, f.sha, None, None, None, None, commit, f"failed: {e}"[:500])
                log_event(log, "download failed", path=f.path, error=str(e))
            if i % 25 == 0:
                run.progress()
        run.finish("success" if run.items_failed == 0 else "partial")
    except StopRequested as e:
        run.finish("stopped", str(e))
    except SourceBlocked as e:
        run.finish("blocked", str(e))
    except StorageRisk as e:
        run.finish("stopped", f"저장공간 위험: {e}")
        raise
    except Exception as e:
        run.finish("failed", str(e))
        raise
    return {"fetched": fetched, "run_id": run.run_id, "status": run.status}


def latest_snapshot(kind: str) -> Path | None:
    d = raw_dir() / "listing" / kind
    if not d.exists():
        return None
    files = sorted(d.glob("*.csv"))
    return files[-1] if files else None
