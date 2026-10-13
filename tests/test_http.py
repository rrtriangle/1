"""로컬 HTTP 서버로 재시도·백오프·차단 감지를 검증한다 (외부 접속 없음)."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from marketlab.net.http import HttpClient, SourceBlocked


class _H(BaseHTTPRequestHandler):
    calls = {"n": 0}

    def log_message(self, *a):
        pass

    def do_GET(self):
        _H.calls["n"] += 1
        if self.path.startswith("/flaky") and _H.calls["n"] < 3:
            self.send_response(503); self.send_header("Retry-After", "0"); self.end_headers(); return
        if self.path.startswith("/blocked"):
            self.send_response(403); self.end_headers(); return
        body = b"date,open\n2020-01-01,1\n"
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    srv = HTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _H.calls["n"] = 0
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_retry_then_success(server, isolated):
    c = HttpClient("test", min_interval_sec=0)
    c.backoff_base = 0.01
    r = c.get(server + "/flaky")
    assert r.status_code == 200 and _H.calls["n"] == 3


def test_circuit_breaker(server, isolated):
    c = HttpClient("test", min_interval_sec=0)
    c.breaker_threshold = 3
    with pytest.raises(SourceBlocked):
        for _ in range(5):
            c.get(server + "/blocked")


def test_download_atomic(server, isolated, tmp_path):
    c = HttpClient("test", min_interval_sec=0)
    dest = tmp_path / "x.csv"
    n = c.download(server + "/ok", dest)
    assert n == dest.stat().st_size and not list(tmp_path.glob(".x.csv.part*"))
