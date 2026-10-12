"""한국 주식 기본 원천: FinanceData/marcap 연도별 Parquet (1995-05-02 ~ 현재).

- 원본 Parquet 를 변환 없이 data/raw/marcap/ 에 그대로 보존한다.
- 원격 blob sha 와 로컬 git_blob_sha1 이 같으면 다시 받지 않는다 (멱등).
- 과거 연도 파일이 바뀌면(과거 데이터 수정) 이전 판을 _history/ 에 보관한다.
"""
from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq

from ..config import get_settings
from ..logging_setup import get_logger, log_event
from ..net.http import HttpClient, SourceBlocked
from ..storage.diskguard import DiskGuard, StorageRisk
from ..storage.parquet_io import git_blob_sha1, sha256_file
from .base import Run, StopRequested, manifest_entries, record_manifest, stop_requested
from .github_repo import RemoteFile, list_repo_files, remote_size

log = get_logger("marcap")
SOURCE = "marcap"
FILE_RE = re.compile(r"^data/marcap-(\d{4})\.parquet$")
EXPECTED_COLUMNS = {"Code", "Name", "Open", "High", "Low", "Close", "Volume", "Amount", "Changes",
                    "ChangeCode", "ChangesRatio", "Marcap", "Stocks", "MarketId", "Market", "Dept",
                    "Rank", "Date"}


def raw_dir() -> Path:
    return get_settings().paths.raw / "marcap"


def local_path(year: int) -> Path:
    return raw_dir() / f"marcap-{year}.parquet"


def list_remote(client: HttpClient) -> tuple[str, dict[int, RemoteFile], str]:
    cfg = get_settings().source(SOURCE)
    sha, files, method = list_repo_files(cfg["repo"], cfg.get("branch", "master"), client,
                                         cfg.get("api_base", "https://api.github.com"))
    out: dict[int, RemoteFile] = {}
    for f in files:
        m = FILE_RE.match(f.path)
        if m:
            out[int(m.group(1))] = f
    return sha, out, method


def plan(client: HttpClient | None = None) -> dict:
    """다운로드 필요 파일과 크기를 계산 (disk-plan 용)."""
    client = client or HttpClient(SOURCE)
    cfg = get_settings().source(SOURCE)
    commit, remote, method = list_remote(client)
    have = manifest_entries(SOURCE)
    todo, total_remote, todo_bytes, unknown = [], 0, 0, 0
    for year, rf in sorted(remote.items()):
        size = rf.size
        if size is None:
            size = remote_size(client, f"{cfg['raw_base']}/{cfg.get('branch', 'master')}/{rf.path}")
        if size is None:
            unknown += 1
            size = 0
        total_remote += size
        h = have.get(f"marcap-{year}")
        if not (h and h["remote_sha"] == rf.sha and local_path(year).exists()):
            todo.append(year)
            todo_bytes += size
    return {"commit": commit, "method": method, "years": sorted(remote), "todo": todo,
            "remote_total_bytes": total_remote, "todo_bytes": todo_bytes, "unknown_sizes": unknown}


def _check_parquet(path: Path) -> int:
    pf = pq.ParquetFile(path)
    cols = set(pf.schema_arrow.names)
    missing = EXPECTED_COLUMNS - cols
    if missing:
        raise ValueError(f"스키마 불일치: 누락 컬럼 {sorted(missing)}")
    return pf.metadata.num_rows


def collect(mode: str = "incremental", years: list[int] | None = None) -> dict:
    """반환: {'changed_years': [...], 'run_id': ...}"""
    s = get_settings()
    cfg = s.source(SOURCE)
    client = HttpClient(SOURCE)
    run = Run(SOURCE, "KR", mode).start()
    changed: list[int] = []
    try:
        commit, remote, method = list_remote(client)
        have = manifest_entries(SOURCE)
        targets = sorted(remote) if not years else [y for y in sorted(remote) if y in years]
        todo: list[tuple[int, RemoteFile]] = []
        for y in targets:
            rf = remote[y]
            h = have.get(f"marcap-{y}")
            lp = local_path(y)
            if mode != "full" and h and h["remote_sha"] == rf.sha and lp.exists():
                continue
            if lp.exists() and git_blob_sha1(lp) == rf.sha:
                # 파일은 이미 같고 manifest 만 없거나 오래됨 → 다시 받지 않는다
                if not (h and h["remote_sha"] == rf.sha):
                    record_manifest(SOURCE, f"marcap-{y}", None, rf.sha, lp, lp.stat().st_size,
                                    sha256_file(lp), _check_parquet(lp), commit, "ok")
                    changed.append(y)
                continue
            todo.append((y, rf))
        run.items_total = len(todo)
        run.message = f"listing via {method}; commit {commit[:12]}; {len(todo)} files to fetch"
        run.progress()

        sizes = {}
        for y, rf in todo:
            url = f"{cfg['raw_base']}/{cfg.get('branch', 'master')}/{rf.path}"
            sizes[y] = rf.size or remote_size(client, url) or 30 * 1024 ** 2
        planned = sum(sizes.values()) * 2  # 원본 + 표준화 산출물
        guard = DiskGuard("marcap 다운로드", planned, peak_temp_bytes=max(sizes.values(), default=0))
        guard.preflight()

        current_year = date.today().year
        written = 0
        for y, rf in todo:
            if stop_requested():
                raise StopRequested("사용자 중지 요청")
            guard.checkpoint(written)
            url = f"{cfg['raw_base']}/{cfg.get('branch', 'master')}/{rf.path}"
            lp = local_path(y)
            tmp_dest = s.paths.tmp / "marcap" / lp.name
            try:
                n = client.download(url, tmp_dest, checkpoint=lambda b: guard.checkpoint(written + b))
                got = git_blob_sha1(tmp_dest)
                if got != rf.sha:
                    raise IOError(f"blob sha 불일치 (원격 {rf.sha[:10]}, 수신 {got[:10]}) - 원격 갱신 중일 수 있음")
                rows = _check_parquet(tmp_dest)
                if lp.exists() and y < current_year:
                    # 과거 연도 데이터가 수정됨 → 이전 판 보관 (감사 추적)
                    hist = raw_dir() / "_history"
                    hist.mkdir(parents=True, exist_ok=True)
                    old_sha = have.get(f"marcap-{y}", {}).get("remote_sha") or git_blob_sha1(lp)
                    shutil.move(lp, hist / f"marcap-{y}.{old_sha[:10]}.parquet")
                    log_event(log, "과거 연도 파일 변경 감지, 이전 판 보관", year=y)
                lp.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(tmp_dest, lp)
                record_manifest(SOURCE, f"marcap-{y}", url, rf.sha, lp, n, sha256_file(lp), rows, commit, "ok")
                changed.append(y)
                written += n
                run.items_ok += 1
                run.rows_written += rows
                run.bytes_written += n
                log_event(log, "downloaded", year=y, bytes=n, rows=rows)
            except (SourceBlocked, StorageRisk, StopRequested):
                raise
            except Exception as e:
                run.items_failed += 1
                record_manifest(SOURCE, f"marcap-{y}", url, rf.sha, None, None, None, None, commit,
                                f"failed: {e}"[:500])
                log_event(log, "download failed", year=y, error=str(e))
            finally:
                if tmp_dest.exists():
                    tmp_dest.unlink()
            run.progress()
        status = "success" if run.items_failed == 0 else ("partial" if run.items_ok else "failed")
        run.finish(status)
    except StopRequested as e:
        run.finish("stopped", str(e))
    except StorageRisk as e:
        run.finish("stopped", f"저장공간 위험: {e}")
        raise
    except SourceBlocked as e:
        run.finish("blocked", str(e))
    except Exception as e:
        run.finish("failed", str(e))
        raise
    return {"changed_years": sorted(set(changed)), "run_id": run.run_id, "status": run.status}
