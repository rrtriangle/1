"""Parquet 원자적 쓰기와 해시 계산."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

COMPRESSION = "zstd"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def git_blob_sha1(path: Path, chunk: int = 1 << 20) -> str:
    """GitHub 트리의 blob sha 와 동일한 방식의 해시 (원격 파일 무결성 대조용)."""
    size = os.path.getsize(path)
    h = hashlib.sha1(f"blob {size}\0".encode())
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def atomic_replace(tmp: Path, final: Path) -> None:
    final.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, final)


def write_table_atomic(table: pa.Table, final: Path, row_group_size: int = 256_000) -> int:
    """임시 파일에 쓴 뒤 rename. 중단돼도 기존 파일이 깨지지 않는다. 반환: 바이트 수."""
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f".{final.name}.tmp-{os.getpid()}")
    pq.write_table(table, tmp, compression=COMPRESSION, row_group_size=row_group_size)
    atomic_replace(tmp, final)
    return final.stat().st_size
