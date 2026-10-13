"""연구 작업 자원 관리: 메모리 감시, 사전 용량·메모리 추정, 실행 잠금.

- 학습 프로세스 RSS 가 hard_memory_gb 를 넘거나 시스템 가용 메모리가 min_available_gb 아래로
  떨어지면 MemoryPressure 를 던져 작업을 안전하게 중단한다(완료된 단위는 보존).
- 외장 SSD 는 저장공간만 늘릴 뿐 RAM 부족을 해결하지 못한다. 메모리 초과 시 권고는
  특징 축소 → 타입 축소 → 시장·기간 분할 → 표본추출 → 외부(고RAM) 환경 순서로 제시한다.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import psutil

from ..config import get_settings

GB = 1024 ** 3


class MemoryPressure(RuntimeError):
    pass


def rss_gb() -> float:
    return psutil.Process(os.getpid()).memory_info().rss / GB


@dataclass
class MemoryMonitor:
    """백그라운드에서 0.5초마다 메모리를 확인. check() 를 루프 안에서 호출해 중단 신호를 받는다."""
    hard_gb: float | None = None
    min_available_gb: float | None = None
    peak_rss_gb: float = 0.0
    tripped: str | None = None
    _stop: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self):
        r = get_settings().get("research.resources", {}) or {}
        self.hard_gb = self.hard_gb or float(r.get("hard_memory_gb", 4.0))
        self.min_available_gb = self.min_available_gb or float(r.get("min_available_gb", 1.0))

    def _run(self):
        while not self._stop.is_set():
            cur = rss_gb()
            self.peak_rss_gb = max(self.peak_rss_gb, cur)
            avail = psutil.virtual_memory().available / GB
            if cur > self.hard_gb:
                self.tripped = f"프로세스 메모리 {cur:.2f}GB > 한도 {self.hard_gb}GB"
            elif avail < self.min_available_gb:
                self.tripped = f"시스템 가용 메모리 {avail:.2f}GB < {self.min_available_gb}GB (스와핑 위험)"
            time.sleep(0.5)

    def start(self) -> "MemoryMonitor":
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def check(self) -> None:
        if self.tripped:
            raise MemoryPressure(self.tripped + " → 특징 수 축소, 기간·시장 분할, 표본 간격 확대 후 재시도하세요.")


@contextmanager
def monitored():
    m = MemoryMonitor().start()
    try:
        yield m
    finally:
        m.stop()


def estimate_training_memory(n_rows: int, n_features: int) -> dict:
    """float32 행렬 + LightGBM 히스토그램/복사본 경험치(약 2.5배)."""
    matrix = n_rows * n_features * 4 / GB
    est = matrix * 2.5 + 0.4
    r = get_settings().get("research.resources", {}) or {}
    soft = float(r.get("soft_memory_gb", 3.0))
    return {"rows": n_rows, "features": n_features, "matrix_gb": matrix, "estimated_peak_gb": est,
            "fits": est <= soft, "max_rows_within_soft": int(max(0, (soft - 0.4) / 2.5) * GB / (4 * max(1, n_features)))}


def threads() -> int:
    return int((get_settings().get("research.resources", {}) or {}).get("threads", 2))


def release_memory() -> None:
    """fold 사이에 호출: 파이썬 GC + (Linux) 해제된 힙을 운영체제에 반환해 RSS 누적을 막는다."""
    import gc
    import sys
    gc.collect()
    if sys.platform.startswith("linux"):
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
