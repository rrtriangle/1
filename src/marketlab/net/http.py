"""원천별 호출 제한, 지수 백오프 재시도, 차단 감지(서킷 브레이커)를 갖춘 HTTP 클라이언트.

접근통제·CAPTCHA·로그인 제한은 우회하지 않는다. 차단이 지속되면 SourceBlocked 를 던져
해당 원천 수집을 안전하게 중단한다.
"""
from __future__ import annotations

import os
import random
import threading
import time
from pathlib import Path
from typing import Iterator

import requests

from ..config import get_settings
from ..logging_setup import get_logger, log_event

log = get_logger("http")

RETRY_STATUS = {429, 500, 502, 503, 504, 520, 522, 524}
BLOCK_STATUS = {401, 403, 407, 429}


class SourceBlocked(RuntimeError):
    """원천이 지속적으로 접근을 거부함 (호출 제한, 로그인 요구, 정책 차단 등)."""


class HttpClient:
    def __init__(self, source: str, min_interval_sec: float | None = None,
                 headers: dict[str, str] | None = None):
        s = get_settings()
        h = s.get("http", {}) or {}
        src = s.source(source)
        self.source = source
        self.min_interval = float(min_interval_sec if min_interval_sec is not None
                                  else src.get("min_interval_sec", 1.0))
        self.timeout = float(h.get("timeout_sec", 60))
        self.max_retries = int(h.get("max_retries", 5))
        self.backoff_base = float(h.get("backoff_base_sec", 2.0))
        self.backoff_max = float(h.get("backoff_max_sec", 120.0))
        self.breaker_threshold = int(h.get("circuit_breaker_threshold", 8))
        self.session = requests.Session()
        self.session.headers["User-Agent"] = h.get("user_agent", "market-research-lab/0.1")
        if headers:
            self.session.headers.update(headers)
        self._last = 0.0
        self._lock = threading.Lock()
        self.consecutive_blocks = 0

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after and retry_after.isdigit():
            delay = min(float(retry_after), self.backoff_max)
        else:
            delay = min(self.backoff_base * (2 ** attempt), self.backoff_max)
            delay *= 0.5 + random.random()  # jitter
        time.sleep(delay)

    def request(self, method: str, url: str, stream: bool = False, **kw) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.request(method, url, timeout=self.timeout, stream=stream, **kw)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                log_event(log, "network error", source=self.source, url=url, attempt=attempt, error=str(e))
                if attempt >= self.max_retries:
                    break
                self._sleep_backoff(attempt, None)
                continue
            if resp.status_code in BLOCK_STATUS:
                self.consecutive_blocks += 1
                if self.consecutive_blocks >= self.breaker_threshold:
                    raise SourceBlocked(f"{self.source}: 연속 {self.consecutive_blocks}회 접근 거부 "
                                        f"(HTTP {resp.status_code}) - 안전 중단")
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                log_event(log, "retryable status", source=self.source, url=url,
                          status=resp.status_code, attempt=attempt)
                self._sleep_backoff(attempt, resp.headers.get("Retry-After"))
                continue
            if resp.status_code < 400:
                self.consecutive_blocks = 0
            return resp
        if last_exc:
            raise last_exc
        return resp  # type: ignore[possibly-undefined]

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def head(self, url: str, **kw) -> requests.Response:
        return self.request("HEAD", url, allow_redirects=True, **kw)

    def download(self, url: str, dest: Path, chunk: int = 1 << 20,
                 checkpoint=None) -> int:
        """스트리밍 다운로드 → 임시 파일 → rename. checkpoint(bytes) 는 디스크 가드 훅."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.part-{os.getpid()}")
        resp = self.get(url, stream=True)
        if resp.status_code != 200:
            resp.close()
            raise requests.HTTPError(f"HTTP {resp.status_code} for {url}", response=resp)
        written = 0
        try:
            with open(tmp, "wb") as fh:
                for i, block in enumerate(resp.iter_content(chunk)):
                    fh.write(block)
                    written += len(block)
                    if checkpoint and i % 64 == 0:
                        checkpoint(written)
                fh.flush()
                os.fsync(fh.fileno())
            expected = resp.headers.get("Content-Length")
            if expected and resp.headers.get("Content-Encoding") in (None, "identity") \
                    and int(expected) != written:
                raise IOError(f"크기 불일치: 기대 {expected}, 실제 {written}")
            os.replace(tmp, dest)
        finally:
            resp.close()
            if tmp.exists():
                tmp.unlink()
        return written


def iter_lines(resp: requests.Response) -> Iterator[str]:
    for line in resp.iter_lines(decode_unicode=True):
        if line is not None:
            yield line
