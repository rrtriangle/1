"""수집 작업 공통: 실행 기록(ingestion_runs), 항목별 재개 상태(ingestion_items), 중지 신호, 실행 잠금."""
from __future__ import annotations

import fcntl
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from ..config import get_settings
from ..logging_setup import get_logger, log_event
from ..storage.catalog import catalog

log = get_logger("run")


def stop_flag_path() -> Path:
    return get_settings().paths.state_dir / "STOP"


def stop_requested() -> bool:
    return stop_flag_path().exists()


def request_stop() -> None:
    p = stop_flag_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(datetime.now().isoformat(), encoding="utf-8")


def clear_stop() -> None:
    p = stop_flag_path()
    if p.exists():
        p.unlink()


class StopRequested(RuntimeError):
    pass


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def exclusive_lock(name: str = "ingest") -> Iterator[None]:
    """중복 실행 방지 (launchd 와 수동 실행이 겹쳐도 하나만 진행)."""
    d = get_settings().paths.state_dir
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.lock"
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise AlreadyRunning(f"다른 수집 작업이 이미 실행 중입니다 ({path})")
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
        fh.flush()
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


@dataclass
class Run:
    source: str
    market: str
    mode: str
    run_id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:6])
    items_total: int = 0
    items_ok: int = 0
    items_failed: int = 0
    rows_written: int = 0
    bytes_written: int = 0
    status: str = "running"
    message: str = ""

    def start(self) -> "Run":
        with catalog() as con:
            con.execute(
                "INSERT INTO ingestion_runs VALUES (?,?,?,?,?,now(),NULL,?,?,?,?,?,?,?)",
                [self.run_id, self.source, self.market, self.mode, self.status, self.items_total,
                 self.items_ok, self.items_failed, self.rows_written, self.bytes_written, self.message,
                 os.getpid()],
            )
        log_event(log, "run started", run_id=self.run_id, source=self.source, market=self.market, mode=self.mode)
        return self

    def progress(self) -> None:
        with catalog() as con:
            con.execute(
                "UPDATE ingestion_runs SET items_total=?, items_ok=?, items_failed=?, rows_written=?, "
                "bytes_written=?, message=? WHERE run_id=?",
                [self.items_total, self.items_ok, self.items_failed, self.rows_written,
                 self.bytes_written, self.message, self.run_id],
            )

    def finish(self, status: str, message: str = "") -> None:
        self.status = status
        if message:
            self.message = message
        with catalog() as con:
            con.execute(
                "UPDATE ingestion_runs SET status=?, finished_at=now(), items_total=?, items_ok=?, "
                "items_failed=?, rows_written=?, bytes_written=?, message=? WHERE run_id=?",
                [status, self.items_total, self.items_ok, self.items_failed, self.rows_written,
                 self.bytes_written, self.message, self.run_id],
            )
        log_event(log, "run finished", run_id=self.run_id, source=self.source, status=status,
                  ok=self.items_ok, failed=self.items_failed, rows=self.rows_written, message=self.message)


def mark_stale_runs() -> int:
    """비정상 종료로 'running' 에 남은 실행을 'interrupted' 로 정리 (재개 시 호출)."""
    import psutil

    with catalog() as con:
        rows = con.execute("SELECT run_id, pid FROM ingestion_runs WHERE status='running'").fetchall()
        n = 0
        for run_id, pid in rows:
            if pid is None or not psutil.pid_exists(int(pid)) or int(pid) == os.getpid():
                con.execute("UPDATE ingestion_runs SET status='interrupted', finished_at=now() WHERE run_id=?",
                            [run_id])
                n += 1
        return n


def get_item_states(source: str) -> dict[str, dict]:
    with catalog(read_only=True) as con:
        rows = con.execute(
            "SELECT item_key, status, first_date, last_success_date, attempts, rows, error "
            "FROM ingestion_items WHERE source=?", [source]).fetchall()
    return {r[0]: {"status": r[1], "first_date": r[2], "last_success_date": r[3], "attempts": r[4],
                   "rows": r[5], "error": r[6]} for r in rows}


def upsert_items(source: str, items: list[tuple]) -> None:
    """items: (item_key, status, first_date, last_success_date, rows, error)"""
    if not items:
        return
    with catalog() as con:
        con.executemany(
            """
            INSERT INTO ingestion_items AS t VALUES (?, ?, ?, ?, ?, now(), 1, ?, ?)
            ON CONFLICT (source, item_key) DO UPDATE SET
                status = excluded.status,
                first_date = coalesce(least(t.first_date, excluded.first_date), excluded.first_date, t.first_date),
                last_success_date = CASE WHEN excluded.status = 'ok'
                    THEN coalesce(greatest(t.last_success_date, excluded.last_success_date), excluded.last_success_date)
                    ELSE t.last_success_date END,
                last_attempt_at = now(),
                attempts = t.attempts + 1,
                rows = CASE WHEN excluded.status = 'ok' THEN coalesce(t.rows, 0) + coalesce(excluded.rows, 0) ELSE t.rows END,
                error = excluded.error
            """,
            [(source, k, st, fd, ld, rows, err) for (k, st, fd, ld, rows, err) in items],
        )


def record_manifest(source: str, file_key: str, remote_url: str | None, remote_sha: str | None,
                    local_path: Path | None, size: int | None, sha256: str | None,
                    row_count: int | None, version: str | None, status: str) -> None:
    with catalog() as con:
        con.execute(
            "INSERT OR REPLACE INTO source_manifest VALUES (?,?,?,?,?,?,?,?,now(),?,?)",
            [source, file_key, remote_url, remote_sha, str(local_path) if local_path else None,
             size, sha256, row_count, version, status],
        )


def manifest_entries(source: str) -> dict[str, dict]:
    with catalog(read_only=True) as con:
        rows = con.execute("SELECT file_key, remote_sha, sha256, local_path, size_bytes, status, fetched_at "
                           "FROM source_manifest WHERE source=?", [source]).fetchall()
    return {r[0]: {"remote_sha": r[1], "sha256": r[2], "local_path": r[3], "size": r[4],
                   "status": r[5], "fetched_at": r[6]} for r in rows}


def record_source_check(source: str, reachable: bool, status: int | None, detail: str) -> None:
    with catalog() as con:
        con.execute("INSERT OR REPLACE INTO source_checks VALUES (?, now(), ?, ?, ?)",
                    [source, reachable, status, detail[:1000]])


def to_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.fromisoformat(str(v)[:10]).date()
