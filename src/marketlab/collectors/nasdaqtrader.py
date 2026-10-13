"""미국 현재 상장 종목 목록: Nasdaq Trader Symbol Directory.

주의: 이 목록은 '현재' 상장 종목만 담는다. 과거 상장폐지 종목의 전체 목록이 아니다.
실행할 때마다 스냅샷을 gzip 으로 보존하므로, 시간이 지나면 목록에서 사라진 티커를
상장폐지(추정) 이벤트로 기록할 수 있다 (시스템 도입 이후 구간만).
"""
from __future__ import annotations

import gzip
import io
import re
from datetime import date
from pathlib import Path

import pandas as pd

from ..config import get_settings
from ..logging_setup import get_logger, log_event
from ..net.http import HttpClient
from ..storage.catalog import catalog
from ..storage.parquet_io import sha256_file
from .base import Run, record_manifest

log = get_logger("nasdaqtrader")
SOURCE = "nasdaqtrader"

OTHER_EXCHANGE = {"A": "NYSE American", "N": "NYSE", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEX"}
NASDAQ_TIER = {"Q": "NASDAQ Global Select", "G": "NASDAQ Global Market", "S": "NASDAQ Capital Market"}


def raw_dir() -> Path:
    return get_settings().paths.raw / "nasdaqtrader"


def classify(name: str, etf_flag: str) -> tuple[str, str]:
    """(security_type, method). ETF 플래그만 거래소 제공값이고 나머지는 이름 기반 추정."""
    n = (name or "").lower()
    if etf_flag == "Y":
        return "etf", "reported"
    rules = [
        (r"warrant", "warrant"),
        (r"\bright(s)?\b", "right"),
        (r"\bunits?\b", "unit"),
        (r"american depositary|\bads\b|\badr\b", "adr"),
        (r"preferred|depositary shares? (each )?representing|% (series|fixed|cumulative)|\bperpetual\b", "preferred"),
        (r"\bnotes?\b|debenture|\bbonds?\b", "note"),
        (r"acquisition corp|acquisition co\b|\bspac\b", "spac"),
        (r"\breit\b|real estate investment trust", "reit"),
        (r"exchange traded|\betn\b|trust units|\bfund\b", "fund_other"),
        (r"common stock|ordinary shares|class [a-c] (common|ordinary)|common shares|\bshares\b", "common"),
    ]
    for pat, typ in rules:
        if re.search(pat, n):
            return typ, "heuristic"
    return "other", "heuristic"


def to_yahoo(symbol: str) -> str:
    """Nasdaq Trader(ACT/CQS) 표기 → Yahoo 표기."""
    s = symbol.strip()
    s = re.sub(r"\$([A-Z]*)$", lambda m: "-P" + m.group(1), s)   # 우선주 ABC$D → ABC-PD
    s = s.replace(".WS", "-WT").replace(".U", "-UN").replace(".R", "-RI")
    return s.replace(".", "-")


def _parse(text: str, kind: str) -> pd.DataFrame:
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("File Creation Time")]
    df = pd.read_csv(io.StringIO("\n".join(lines)), sep="|", dtype=str, keep_default_na=False)
    if kind == "nasdaqlisted":
        out = pd.DataFrame({
            "symbol": df["Symbol"], "name": df["Security Name"],
            "exchange": "NASDAQ", "market_tier": df["Market Category"].map(NASDAQ_TIER).fillna(df["Market Category"]),
            "etf": df["ETF"], "test_issue": df["Test Issue"], "financial_status": df.get("Financial Status", ""),
            "cqs_symbol": df["Symbol"], "nasdaq_symbol": df["Symbol"],
        })
    else:
        out = pd.DataFrame({
            "symbol": df["ACT Symbol"], "name": df["Security Name"],
            "exchange": df["Exchange"].map(OTHER_EXCHANGE).fillna(df["Exchange"]), "market_tier": "",
            "etf": df["ETF"], "test_issue": df["Test Issue"], "financial_status": "",
            "cqs_symbol": df["CQS Symbol"], "nasdaq_symbol": df["NASDAQ Symbol"],
        })
    return out


def _fetch_primary(client: HttpClient, cfg: dict, today: str) -> list[pd.DataFrame]:
    frames = []
    for kind, url in cfg["urls"].items():
        r = client.get(url)
        if r.status_code != 200 or "|" not in r.text[:500]:
            raise RuntimeError(f"{kind}: HTTP {r.status_code}, 예상 형식 아님")
        dest = raw_dir() / today / f"{kind}.txt.gz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(dest, "wt", encoding="utf-8") as fh:
            fh.write(r.text)
        record_manifest(SOURCE, f"{today}/{kind}", url, None, dest, dest.stat().st_size,
                        sha256_file(dest), None, today, "ok")
        frames.append(_parse(r.text, kind))
    return frames


MIRROR_EXCHANGE = {"nasdaq": "NASDAQ", "nyse": "NYSE", "amex": "NYSE American"}


def _fetch_mirror(cfg: dict, today: str) -> list[pd.DataFrame]:
    """대체 원천: rreichel3/US-Stock-Symbols (Nasdaq 종목 스크리너를 매일 GitHub 에 적재).

    ETF 여부·NYSE Arca·테스트 종목 정보가 없으므로 Nasdaq Trader 보다 범위가 좁다.
    """
    import json

    client = HttpClient(SOURCE + "_mirror", min_interval_sec=0.5)
    base = cfg.get("mirror_raw_base", "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main")
    frames = []
    for ex, exchange in MIRROR_EXCHANGE.items():
        url = f"{base}/{ex}/{ex}_full_tickers.json"
        r = client.get(url)
        if r.status_code != 200:
            raise RuntimeError(f"mirror {ex}: HTTP {r.status_code}")
        dest = raw_dir() / today / f"mirror_{ex}.json.gz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(dest, "wb") as fh:
            fh.write(r.content)
        record_manifest(SOURCE, f"{today}/mirror_{ex}", url, None, dest, dest.stat().st_size,
                        sha256_file(dest), None, today, "ok")
        rows = json.loads(r.content)
        df = pd.DataFrame(rows)
        # 표기 통일: 우선주 ABR^D → ABR$D (ACT 표기), 클래스 BRK/A → BRK.A
        sym = df["symbol"].str.strip().str.replace("^", "$", regex=False).str.replace("/", ".", regex=False)
        frames.append(pd.DataFrame({
            "symbol": sym, "name": df["name"].str.strip(), "exchange": exchange, "market_tier": "",
            "etf": "N", "test_issue": "N", "financial_status": "", "cqs_symbol": sym, "nasdaq_symbol": sym,
        }))
    return frames


def collect() -> dict:
    s = get_settings()
    cfg = s.source(SOURCE)
    client = HttpClient(SOURCE)
    run = Run(SOURCE, "US", "snapshot").start()
    today = date.today().isoformat()
    try:
        try:
            frames = _fetch_primary(client, cfg, today)
            used = "nasdaqtrader"
        except Exception as e:
            log_event(log, "Nasdaq Trader 접근 실패 → GitHub 미러로 대체", error=str(e)[:300])
            frames = _fetch_mirror(cfg, today)
            used = "us_symbols_mirror"
        run.items_ok = len(frames)
        df = pd.concat(frames, ignore_index=True)
        df = df[df["symbol"].str.len() > 0].drop_duplicates("symbol")
        types = df.apply(lambda r: classify(r["name"], r["etf"]), axis=1, result_type="expand")
        df["security_type"], df["type_method"] = types[0], types[1]
        df["yahoo_symbol"] = df["symbol"].map(to_yahoo)
        df["snapshot_date"] = today
        df["list_source"] = used
        out = s.paths.metadata / "us_listings" / f"{today}-{used}.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out, index=False)
        run.rows_written = len(df)
        # 대체 원천은 범위가 좁아(ETF 등 누락) 사라진 종목을 상장폐지로 판정하지 않는다
        n_new, n_gone = register_us_securities(df, today, source=used, detect_gone=(used == "nasdaqtrader"))
        run.finish("success", f"[{used}] {len(df)} symbols; new {n_new}; disappeared {n_gone}")
        return {"list_source": used, "symbols": len(df), "new": n_new, "disappeared": n_gone, "run_id": run.run_id}
    except Exception as e:
        run.finish("failed", str(e))
        raise


def register_us_securities(df: pd.DataFrame, snapshot_date: str, source: str = "nasdaqtrader",
                           detect_gone: bool = True) -> tuple[int, int]:
    """현재 목록을 증권 마스터에 반영.

    - 활성(listed) 증권 중 같은 티커가 없으면 새 security_id 를 만든다: US-<티커>-<최초 확인일>.
    - 이전 스냅샷에 있었는데 이번 목록에서 사라진 티커는 delisted(추정) 이벤트로 기록한다.
    - 이름이 바뀐 경우 symbol_history 에 새 이력을 추가한다.
    무료 데이터로는 시스템 도입 이전의 티커 재사용을 판별할 수 없어 id_confidence='unverified'.
    """
    sd = snapshot_date.replace("-", "")
    with catalog() as cat:
        cat.register("snap", df)
        cat.execute("""
            CREATE OR REPLACE TEMP TABLE active AS
            SELECT s.security_id, h.symbol, s.name FROM securities s
            JOIN symbol_history h ON h.security_id = s.security_id AND h.symbol_type = 'ticker' AND h.valid_to IS NULL
            WHERE s.country = 'US' AND s.status = 'listed'
        """)
        new = cat.execute(f"""
            SELECT 'US-' || replace(n.symbol, '$', '_P') || '-{sd}' AS security_id, n.*
            FROM snap n ANTI JOIN active a ON a.symbol = n.symbol
        """).df()
        gone = cat.execute("""SELECT a.* FROM active a ANTI JOIN snap n ON a.symbol = n.symbol""").df()
        cat.register("new_df", new)
        cat.execute(f"""
            INSERT OR IGNORE INTO securities
            SELECT security_id, 'US', exchange, exchange, 'USD', security_type, type_method, NULL, NULL, name,
                   NULL, NULL, NULL, NULL, 'listed', NULL, 'unverified', '{source}', now()
            FROM new_df
        """)
        cat.execute(f"""
            INSERT OR IGNORE INTO symbol_history
            SELECT security_id, 'ticker', symbol, exchange, name, DATE '{snapshot_date}', NULL, '{source}', 'reported' FROM new_df
            UNION ALL
            SELECT security_id, 'yahoo', yahoo_symbol, exchange, name, DATE '{snapshot_date}', NULL, '{source}', 'inferred' FROM new_df
        """)
        cat.execute(f"""
            INSERT INTO listing_events
            SELECT security_id, symbol, 'first_seen_in_listing', DATE '{snapshot_date}',
                   '현재 상장 목록({source})에서 최초 확인 (실제 상장일 아님)', NULL, NULL, '{source}', 'observed'
            FROM new_df
        """)
        # 이름·거래소 변경 반영
        cat.execute(f"""
            UPDATE securities SET name = n.name, exchange = n.exchange, market = n.exchange, updated_at = now()
            FROM snap n, active a
            WHERE securities.security_id = a.security_id AND a.symbol = n.symbol
              AND (securities.name IS DISTINCT FROM n.name OR securities.exchange IS DISTINCT FROM n.exchange)
        """)
        if not detect_gone:
            gone = gone.iloc[0:0]
        if len(gone):
            cat.register("gone_df", gone)
            cat.execute(f"""
                UPDATE securities SET status = 'delisted', delisting_date = DATE '{snapshot_date}',
                       delisting_reason = 'Nasdaq Trader 목록에서 사라짐 (사유 미확인)', updated_at = now()
                WHERE security_id IN (SELECT security_id FROM gone_df)
            """)
            cat.execute(f"""
                UPDATE symbol_history SET valid_to = DATE '{snapshot_date}'
                WHERE symbol_type IN ('ticker', 'yahoo') AND valid_to IS NULL
                  AND security_id IN (SELECT security_id FROM gone_df)
            """)
            cat.execute(f"""
                INSERT INTO listing_events
                SELECT security_id, symbol, 'delisted', DATE '{snapshot_date}',
                       '현재 목록에서 사라짐 - 상장폐지/티커 변경/합병 여부 미확인', NULL, NULL, 'nasdaqtrader', 'inferred'
                FROM gone_df
            """)
    log_event(log, "registered", new=len(new), gone=len(gone))
    return len(new), len(gone)
