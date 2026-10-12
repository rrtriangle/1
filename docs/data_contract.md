# 데이터 계약서 (머신러닝·백테스트 연계용)

이 문서는 다른 개발 세션(머신러닝 엔진, 백테스트, 과거 투자 시뮬레이터)이 데이터를 **복제하지 않고** 읽기 위한 계약입니다.
권장 접근 방법은 Python API `marketlab.api.MarketData` 이고, 필요하면 Parquet 파일을 DuckDB 나 Polars 로 직접 읽어도 됩니다.
모든 경로는 `MARKETLAB_DATA_DIR` 기준이며 기본값은 `<프로젝트>/data` 입니다.

## 1. 데이터셋과 물리 위치

| 데이터셋 | 위치 | 형식 | 기본 키 |
|---|---|---|---|
| daily_prices_raw (KR) | `raw/marcap/marcap-YYYY.parquet` | marcap 원본 그대로 | (Code, Date) |
| daily_prices_raw (US) | `raw/yfinance/daily/fetch_date=*/part-*.parquet` | yfinance 원본 + 수집 메타 | (security_id, trade_date, fetched_at) |
| daily_prices_standardized | `standardized/daily_prices/country={KR,US}/year=YYYY/part-0.parquet` | 표준화 일봉 | (security_id, trade_date, source, price_basis) |
| market_indicators | `standardized/market_indicators/*.parquet` | 지표 long 형식 | (indicator_id, obs_date) |
| financial_facts | `standardized/financial_facts/source=sec/part-*.parquet` | XBRL 사실 | (cik, taxonomy, concept, unit, period_start, period_end, accn) |
| sec_filings | `standardized/sec_filings/part-*.parquet` | 공시 이력 | (cik, accession_number) |
| securities, symbol_history, listing_events, corporate_actions, source_manifest, ingestion_runs, ingestion_items, data_quality_issues, trading_calendars | `metadata/catalog.duckdb` | DuckDB 테이블 | 아래 참고 |

DuckDB 카탈로그는 다른 프로세스가 쓰는 동안 잠깁니다. 읽기 전용(`read_only=True`)으로 짧게 열거나, `MarketData` 를 쓰세요(시작할 때 메타데이터를 메모리로 복사합니다).

## 2. 스키마

### daily_prices_standardized
| 컬럼 | 타입 | 단위·설명 |
|---|---|---|
| security_id | VARCHAR | 불변 내부 ID. KR: `KR-<코드>-<구간 첫 관측일>`, US: `US-<티커>-<최초 확인일>` |
| country | VARCHAR | KR / US |
| market | VARCHAR | 그날의 시장 구분 (KOSPI, KOSDAQ, KOSDAQ GLOBAL, KONEX / NASDAQ, NYSE, NYSE American, NYSE Arca …) |
| market_id | VARCHAR | KR: STK/KSQ/KNX |
| symbol | VARCHAR | 그날의 종목코드(KR 6자리) 또는 Yahoo 티커 |
| name | VARCHAR | 그날의 종목명(KR) / 현재 이름(US) |
| trade_date | DATE | 거래소 현지 날짜 (KR: Asia/Seoul, US: America/New_York) |
| currency | VARCHAR | KRW / USD |
| source | VARCHAR | marcap / yfinance |
| price_basis | VARCHAR | `raw`(KR 당시 체결가) / `raw_reconstructed`(US, 분할 이력으로 역산한 당시 가격) |
| open, high, low, close | DOUBLE | 통화 단위 가격. KR 은 거래가 없던 날 open/high/low=0, close=직전 가격 |
| volume | BIGINT | 주 |
| amount | DOUBLE | 거래대금 (KR: 원천값, US: close×volume 근사) |
| market_cap | DOUBLE | 시가총액, 원 (KR만) |
| shares_outstanding | BIGINT | 상장주식수 (KR만) |
| change, change_code, change_ratio | DOUBLE/VARCHAR/DOUBLE | KR 원천의 전일대비(기준가 대비), 등락 기호, 등락률(%) |
| mcap_rank, dept | INTEGER/VARCHAR | KR 시총 순위, 소속부 |
| prev_close | DOUBLE | 같은 security_id 의 직전 거래일 종가 |
| base_price | DOUBLE | KR 기준가 = close − change |
| base_adj_factor | DOUBLE | KR 기준가 조정 계수 (= base_price / prev_close), 조정일에만 값이 있음 |
| split_adj_close, adj_close_tr, split_factor, dividends, stock_splits | DOUBLE | US: Yahoo 분할 반영 종가, 분할+배당 반영 종가, 역산 계수, 당일 배당·분할 |
| is_traded | BOOLEAN | volume > 0 |
| is_final | BOOLEAN | 장 마감과 확정 지연 시간이 지난 일봉 (미확정 일봉은 false) |
| quality_flags | VARCHAR[] | 아래 플래그 목록 |
| quality_grade | VARCHAR | A(이상 없음) / B(정보성: 무거래·기준가 조정·분할일·가격 미형성) / C(경고: 설명 안 되는 급변 등) / X(오류) |
| usable | BOOLEAN | 등급이 X 가 아니고, 확정 일봉이며, 가격이 형성된 행 |
| source_file, ingested_at | VARCHAR/TIMESTAMP | 출처 파일, 수집 시각 |

품질 플래그 목록: `nonpositive_close`, `negative_volume_or_amount`, `ohlc_inconsistent`, `zero_ohlc_with_volume`, `no_trade`, `no_price`, `amount_out_of_range`, `base_price_adjusted`, `big_move_unexplained`, `split_day`, `raw_reconstructed_from_splits`

### securities
security_id(PK), country, market, exchange, currency, security_type(common/preferred/new_shares/spac/reit/etf/adr/warrant/right/unit/note/fund_other/other), type_method(reported/heuristic), isin, cik(기업 단위), name(최근), first_trade_date, last_trade_date, listing_date, delisting_date, status(listed/delisted/merged/unknown), delisting_reason, id_confidence(observed/inferred_reuse/unverified), sources, updated_at

### symbol_history
(security_id, symbol_type[krx_code/ticker/yahoo/isin], symbol, name, valid_from) PK. 컬럼: exchange, valid_to, source, confidence(observed/reported/inferred).
`valid_to IS NULL` 이면 현재 유효한 이력입니다.

### listing_events
security_id, symbol, event_type(listed/first_observed/delisted/merged/code_reused/disappeared_unexplained/first_seen_in_listing), event_date, detail, to_symbol, to_name, source, confidence

### corporate_actions
(security_id, ex_date, action_type, source) PK. 컬럼: ratio, cash_amount, currency, known_from, confidence, detail
- KR (`source='marcap_inferred'`, `confidence='inferred_unverified'`): `ratio` 는 **이전 가격에 곱할 계수**이고, 상장주식수 변화와 맞으면 split/reverse_split, 아니면 base_price_adjustment(유·무상증자, 감자 등)로 분류합니다.
- US (`source='yahoo'`): split 의 ratio 는 분할 비율(4:1 → 4.0)이고, dividend 는 cash_amount(분할 반영 금액)입니다.
- `known_from`: 이 정보를 썼다고 간주할 최초 일자입니다. 공시일을 알 수 없어 ex_date 로 둡니다(보수적).

### market_indicators
indicator_id, name, source, market, currency, unit, obs_date, value(종가/값), open, high, low, volume, amount, market_cap, **available_at(TIMESTAMPTZ, UTC)**, ingested_at
- `KRX:KS11|KQ11|KS200|KRX_INDEX_1001` (krx_cache)
- `MARCAP:<KOSPI|KOSDAQ|KONEX>_<ADV_RATIO|N_LISTED|N_UP|N_DOWN|TOTAL_AMOUNT|TOTAL_MCAP|CW_RETURN|EW_RETURN>` (전 종목에서 직접 계산)
- `FRED:<시리즈>`, `YAHOO:<^GSPC|^IXIC|^DJI|^VIX|^TNX|^KS11|^KQ11|KRW=X|TLT|GLD>`

### financial_facts (SEC)
cik, entity_name, taxonomy, concept(원본 XBRL 개념명), unit, period_start, period_end, value, accn, fy, fp, form, filed, frame, known_from(= filed + 1일)

### trading_calendars
(market, trade_date) PK, tz, source(both/observed/exchange_calendars). `observed` 는 데이터에서 관측된 날, `exchange_calendars` 는 휴장일 규칙으로 계산된 날입니다(1998년 이전 토요일 거래 등은 관측 기준).

### data_quality_issues
run_id, check_name, severity(info/warn/error), market, security_id, symbol, trade_date, source, detail, resolution(kept/supplemented/quarantined/restricted/open), detected_at

## 3. 조인 규칙
- 가격 ↔ 증권: `security_id` 로 조인합니다. **종목코드나 티커로 조인하지 마세요.** 코드는 재사용됩니다(예: 000030 은 1995~2002년 상업/한빛은행, 2014~2019년 우리은행).
- 특정 일자의 코드 → ID 변환은 `MarketData.resolve(symbol, on=date)` 또는 symbol_history 의 `valid_from <= d <= valid_to` 조건으로 합니다.
- 지표 ↔ 가격: `obs_date` 를 그대로 맞추지 말고 `available_at <= 의사결정 시각` 조건으로 맞춥니다. 미국 지표의 t일 값은 한국 t+1일 장 시작 전에야 확정됩니다(서머타임에 따라 시차가 달라지며 available_at 이 이를 반영합니다).
- 재무 ↔ 증권: `securities.cik` 는 현재 티커가 일치하는 경우에만 채워집니다(`ticker_match_current`). CIK 하나에 여러 증권이 연결될 수 있습니다.

## 4. 가격 조정 방법
| adjust | 의미 | 용도 |
|---|---|---|
| `raw` | 당시 실제 거래가격 | **주문·체결 시뮬레이션** (호가 단위, 상하한가, 최소 주문금액) |
| `split` | 분할·병합만 반영 | 가격 수준 비교 |
| `all_corporate` | 분할 + 거래소 기준가 조정(증자·감자 등) 전부 반영 | 수익률, 기술적 지표 |
| `total_return` | + 배당 재투자 | 총수익률 (US 만. KR marcap 은 배당 정보가 없어 all_corporate 와 같음) |

조정 가격은 다음과 같이 계산합니다. 조정가(d) = 원가(d) × Π f_i, 여기서 i 는 `d < ex_date_i ≤ as_of` 이고 `known_from_i ≤ as_of` 인 기업행위입니다.
→ **as_of 이후의 기업행위는 반영하지 않습니다.** 날짜별로 다른 as_of 를 쓰는 백테스트에서는 결정 시점마다 as_of 를 그날로 넘기세요.

## 5. 상장·상장폐지 상태 조회
```python
md.universe("2008-10-24", country="KR", security_types=["common"])   # 그날 시세가 있는 종목 (이후 상장폐지 종목 포함)
md.listing_status("KR-000030-20141119", on="2016-01-04")              # as_of 기준 상태, 이후 사건은 숨김
md.sql("SELECT * FROM listing_events WHERE security_id = ?", [sid])
```
상장폐지 종목의 청산·합병 보상가치는 원천에 없습니다. 마지막 거래 이후의 가치를 0원이나 직전 종가로 확정하지 않습니다.
백테스트에서는 `status in ('delisted','merged','unknown')` 이고 `last_trade_date` 이후 보유 중인 포지션을 '가치 미확정'으로 따로 처리해야 합니다(예: 정리매매 마지막 종가로 청산하는 가정을 명시적으로 선택).

## 6. 임의의 과거 시점 데이터 조회 (point-in-time)
```python
from marketlab.api import MarketData
md = MarketData()
as_of = "2015-06-30"
uni = md.universe(as_of, country="KR")                                   # 생존편향 없는 유니버스
px  = md.prices(uni.security_id.tolist(), "2014-06-30", as_of, adjust="all_corporate", as_of=as_of)
ind = md.indicators(["FRED:DGS10", "MARCAP:KOSPI_ADV_RATIO"], "2014-06-30", as_of, as_of=as_of)
fin = md.financial_facts("0000320193", ["Revenues", "NetIncomeLoss"], as_of=as_of)   # 그 시점까지 제출된 최신본
```

## 7. 추출 API 요약 (`marketlab.api.MarketData`)
| 메서드 | 설명 |
|---|---|
| `search(text)` | 이름·코드·티커 검색 |
| `resolve(symbol, on, country)` | 일자 기준 코드 → security_id |
| `universe(on, country, markets, security_types, usable_only)` | 그날 거래된 종목 |
| `prices(ids, start, end, adjust, as_of, usable_only, columns)` | 일봉 |
| `returns(ids, start, end, as_of, total_return)` | 조정 수익률 |
| `panel(start, end, country, field)` | 날짜×종목 피벗 (기간을 좁게) |
| `corporate_actions(ids, as_of)` / `listing_status(id, on)` | 기업행위, 상태 |
| `indicators(ids, start, end, as_of)` / `indicator_list()` | 지표 |
| `financial_facts(cik, concepts, as_of)` | 재무 사실 |
| `calendar(market, start, end)` | 거래일 |
| `sql(query, params)` | 자유 SQL (views: prices, indicators, financial_facts) |

## 8. 미래정보 누출 방지 제한사항
1. `prices(..., end=E)` 의 기본 as_of 는 E 입니다. 학습 구간을 길게 잡고 as_of 를 끝 날짜로 두면, 그 구간 안에서 일어난 분할은 앞쪽 가격에 반영됩니다. 이는 **표시용으로만** 안전합니다. 시점별 피처를 만들 때는 시점마다 as_of 를 넘기세요.
2. `securities.name`, `security_type`, `status` 는 **현재 기준 정적 속성**입니다. 과거 시점 필터에는 `universe()`(그날의 이름·시장) 또는 symbol_history 를 쓰세요.
3. US 데이터는 Yahoo 가 현재 기업 기준으로 다시 조정한 값을 역산한 것입니다. 상장폐지 종목이 거의 없어 **생존편향이 큽니다.** 미국 유니버스 기반 전략 성과는 과대평가될 수 있습니다.
4. KR 추정 기업행위의 `known_from` 은 ex_date 입니다. 실제 공시는 그보다 이르지만 공시일 정보가 없습니다.
5. FRED 지표는 빈티지 데이터가 아니라 **현재 수정본**입니다(GDP 같은 수정 지표는 아직 수집하지 않습니다. 금리·시장지표는 수정이 거의 없습니다).
6. `is_final=false` 행(당일 미확정)과 `usable=false` 행은 학습에서 제외하세요.
7. 상장폐지 이후의 가치, 거래정지 기간의 가격은 존재하지 않습니다. 전일 가격을 복사하거나(forward-fill) 0으로 채우지 마세요.
