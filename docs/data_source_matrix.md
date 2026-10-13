# 데이터 원천 조사표 (data_source_matrix)

조사·검증일: 2026-10-12. "실제 검증" 칸은 이 저장소 개발 환경(클라우드 리눅스 컨테이너, 송신 네트워크 정책 적용)에서 직접 접속해 본 결과입니다.
사용자 맥북에서는 `marketlab sources` 로 다시 확인하세요. 결과는 `catalog.source_checks` 에 기록됩니다.
문서와 실제 파일이 다르면 실제 파일을 기준으로 했습니다. 예: marcap README 는 시가총액 단위를 '백만원'으로 적지만 실제 값은 원 단위입니다.

## 요약

| 원천 | 용도 | 기간 | 상장폐지 | 키 | 대량 | 개발환경 검증 | 채택 |
|---|---|---|---|---|---|---|---|
| FinanceData/marcap | KR 전 종목 일봉 | 1995-05-02~현재 | 포함(당시 상장 종목 전부) | 불필요 | 연도별 Parquet 32개 | **성공** (1995~2026 전부 다운로드·blob sha 검증) | 기본 원천 |
| fdr_krx_data_cache | KR 상장·폐지 목록, KRX 지수 | 목록: 최신 스냅샷 / 폐지: 1956~ / 지수: 1990~ | 폐지 목록 4,193건 | 불필요 | CSV | **성공** | 보완 원천 |
| FinanceDataReader | 위 캐시 등의 래퍼 | – | – | 불필요(일부 ECOS 키) | – | 패키지 설치 성공. KRX 직접 호출 경로는 차단 | 라이브러리(내부 URL 직접 사용) |
| KRX 정보데이터시스템 | KR 공식 | – | – | **로그인 필요** | 화면 다운로드 | 접속 차단(정책) | 사용 안 함(우회 금지) |
| Yahoo Finance / yfinance | US 일봉·배당·분할, 지수·환율 | 종목별 상장~현재(1962~) | **대부분 없음** | 불필요 | 종목별 호출 | 개발환경 차단 → 맥북에서 실행 필요 | US 기본 원천 |
| Stooq | US 교차검증, 대량 DB | 종목별 상이(수십 년) | 일부 | **2026-03경부터 CSV에 API 키(CAPTCHA 발급) 필요 보고** | DB zip(사람 확인 필요) | 개발환경 차단 | 사용자 수동 다운로드 zip 가져오기만 |
| Nasdaq Trader Symbol Directory | US 현재 상장 목록 | 당일 스냅샷 | **없음**(현재 목록) | 불필요 | 텍스트 2개 | 개발환경 차단 → 맥북에서 실행 필요 | US 종목 목록 |
| SEC EDGAR | CIK 매핑, 공시, XBRL 재무 | 2009~(XBRL) | 기업 단위 포함 | 불필요(User-Agent 필수) | bulk zip | 개발환경 차단 | 재무 확장(승인 후) |
| FRED (CSV) | 금리·금리차·VIX·환율 | 시리즈별(1954~ 등) | – | CSV 불필요 | 시리즈별 CSV | 개발환경 차단 | 지표 |
| OpenDART | KR 재무 | 2015~ | 포함 | **필요** | 기업별 API(일 20,000회) | 개발환경 차단 | 키가 있을 때 corpCode 만 |

## 원천별 상세

### 1. 한국 marcap — https://github.com/FinanceData/marcap
- 제공: FinanceData.KR (KRX 전종목 시세 화면 데이터를 연도별로 정리), 매일 GitHub Actions 로 갱신
- 종류: 일별 전 종목 시가·고가·저가·종가, 거래량, 거래대금, 전일대비, 등락률, 시가총액, 상장주식수, 시장, 소속부, 시총순위 (18개 필드)
- 시장: KOSPI, KOSDAQ(KOSDAQ GLOBAL 포함), KONEX
- 기간: 1995-05-02 ~ 원천 마지막 갱신일 (검증 시점 2026-10-08)
- 종목 수: 5,554개 증권 구간(코드 재사용 분리 후), 15,657,024행
- 상장폐지: 당시 상장된 종목이 모두 들어 있어 이후 상장폐지된 종목도 포함 (생존편향 없음)
- 원본/수정주가: **원본(당시 체결가)만**. '전일대비'가 기준가 대비라서 기준가 조정일(분할·증자 등)을 역추정할 수 있음 → 추정 기업행위 15,810건
- 배당·티커 변경: 배당 없음. 이름 변경은 일별 관측으로 복원
- 무료, 키 불필요, 대량 다운로드 가능 (연도별 5~25MB, 합계 약 560MB)
- 이용조건: 공개 저장소(별도 라이선스 표기 없음). 원자료는 KRX 공개 화면. **개인 연구 용도로 사용하고 재배포는 하지 않음**
- 검증: raw URL 다운로드, git blob sha1 대조 성공. GitHub API 는 이 환경에서 403(정책). 대체 경로(깊이 1·blob 제외 git 트리 조회)로 파일 목록 확보
- 품질 우려: 1995년 파일은 코드 앞자리 0 누락(예: 15760) → 6자리로 보정. 연도별 물리 타입 차이(string/large_string, ns/us). 시가총액은 원 단위(README 와 다름). 1995~1997 '신주' 행(구주와 별도 코드) 다수 → `new_shares`/`merged` 로 분류
- 대체: KRX 정보데이터시스템(로그인 필요), 네이버 금융(약관상 자동 수집 부적합), 유료 FnGuide/DataGuide
- clone 대 직접 다운로드: 전체 clone 은 매일 커밋 이력 때문에 .git 이 데이터의 수 배까지 커지고 작업 폴더와 이중 저장된다 → **필요한 Parquet 만 raw URL 로 받고 blob sha 로 변경분만 갱신**하는 방식을 채택

### 2. fdr_krx_data_cache — https://github.com/FinanceData/fdr_krx_data_cache
- FinanceDataReader 0.9.2xx 가 KRX 직접 호출 대신 사용하는 캐시 저장소(매일 갱신)
- `listing/krx/<날짜>.csv`: 현재 상장 전 종목(ISIN 포함, 당일 시세) / `listing/desc`: 업종·상장일·결산월 / `listing/delisting`: 1956년 이후 상장폐지 4,193건(사유, 이전 종목, 증권 종류) / `index/year_ks11|kq11|ks200|krx_index_1001`
- 검증: 성공. 교차검증 결과 2026-10-08 marcap 과 KRX 스냅샷 종가·거래량 **2,872/2,872 일치**
- 우려: 지수 캐시는 2026-09-17, krx_index_1001 은 2026-03-06 에서 갱신이 멈춤 → marcap 전 종목으로 시장 지표(`MARCAP:*`)를 직접 계산해 보완. KOSPI 지수 수익률과 상관 0.9992(7,906일)

### 3. FinanceDataReader — https://github.com/FinanceData/FinanceDataReader
- 0.9.202 설치 확인. KRX 직접 엔드포인트(data.krx.co.kr)는 로그인 요구로 막혀 있고, 라이브러리는 캐시 저장소를 쓴다
- 본 시스템은 라이브러리를 거치지 않고 같은 원본 파일을 직접 받아 해시·출처를 기록한다(재현성)

### 4. Yahoo Finance / yfinance — https://github.com/ranaroussi/yfinance
- 비공식 API (Yahoo 이용약관상 개인·비상업 용도). 키 불필요, 호출 제한은 비공개이며 429(YFRateLimitError)가 잦다
- 종류: OHLCV, Adj Close, 배당, 분할. 기간: 종목별 상장일~현재(오래된 종목은 1962~)
- **원본/수정 구분**: `auto_adjust=False` 의 Close 는 *분할 반영값*, Adj Close 는 분할+배당 반영값. 당시 실제 체결가는 제공하지 않음 → 분할 이력으로 역산(`raw_reconstructed`)
- 상장폐지: 티커가 현재 기업을 가리키므로 폐지 종목 이력은 대부분 조회되지 않는다. 티커 재사용 시 과거 기업 이력은 사라짐
- 대체: Stooq, Tiingo(무료 키), 유료(Norgate, CRSP, Sharadar)
- 개발환경: 정책상 접속 차단 → 맥북에서 `ingest --market US` 로 수집해야 함
- **주의**: 접속이 막혀도 yfinance 는 네트워크 오류 대신 `YFTzMissingError: possibly delisted` 를 돌려줍니다(개발환경에서 확인).
  그래서 수집기는 SPY 조회로 실제 응답 여부를 확인한 뒤 진행하고, 차단 중의 빈 응답을 상장폐지로 기록하지 않습니다.

### 5. Stooq — https://stooq.com/dbh/ , https://stooq.com/db/h/
- 종목별 CSV(`/q/d/l/?s=aapl.us&i=d`)는 2026년 3월경부터 사이트에서 CAPTCHA 로 발급받는 API 키를 요구한다는 보고가 있음(pandas-datareader 이슈). 일일 호출 한도 존재
- 대량 DB zip(일봉 전체)은 사람 확인 후 수동 다운로드 → **자동 우회하지 않음**. 사용자가 받은 zip 을 `marketlab import-stooq` 로 가져온다
- 가격은 조정값(원본 체결가 아님). 상장폐지 종목 포함 범위는 제한적
- 개발환경: 차단. 교차검증 원천으로만 사용

### 6. Nasdaq Trader — https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs
- `nasdaqlisted.txt`(NASDAQ), `otherlisted.txt`(NYSE, NYSE American, NYSE Arca, Cboe BZX, IEX): 티커, 이름, 거래소, ETF 여부, 테스트 종목 여부
- **현재 상장 목록이며 과거 상장폐지 전체 목록이 아니다.** 본 시스템은 실행마다 스냅샷을 보관해, 도입 이후 목록에서 사라진 종목만 폐지(추정)로 기록한다
- ADR/우선주/SPAC/워런트 구분은 이름 기반 추정(type_method='heuristic'). ETF 여부만 거래소 제공값

- 대체 경로: GitHub 미러 https://github.com/rreichel3/US-Stock-Symbols (Nasdaq 종목 스크리너 데이터를 매일 적재). 2026-10-13 개발환경에서 접근에 성공했습니다(NASDAQ 4,020, NYSE 2,689, AMEX 294 = 7,003종목).
  ETF 플래그와 NYSE Arca·Cboe 상장 종목이 없습니다. 우선주는 `^`, 클래스주는 `/` 로 표기되어 있어 ACT 표기(`$`, `.`)로 변환합니다.

### 7. KRX 정보데이터시스템 — https://data.krx.co.kr/
- 공식 원천이지만 자동 다운로드에 로그인(회원) 세션이 필요해졌다(FDR 캐시 전환이 그 증거). 로그인 자동화나 우회를 하지 않는다

### 8. SEC EDGAR — https://www.sec.gov/
- `company_tickers_exchange.json`: 현재 티커↔CIK↔거래소. `submissions.zip`(공시 이력, 접수 시각), `companyfacts.zip`(XBRL 재무 사실 전체)
- 무료, 키 불필요. **연락처가 포함된 User-Agent 필수, 초당 10회 이하**
- CIK 는 기업 단위 ID → 증권과 1:1 이 아님(보통주·우선주·워런트가 같은 CIK)
- 크기: companyfacts.zip 약 1.3GB 이상, 변환 Parquet 수 GB → `disk-plan --with-sec` 확인 후 `--approve-large` 로 승인해야 받는다
- 시점: `filed` 다음 날을 known_from 으로 둔다(보수적 가정)

### 9. FRED — https://fred.stlouisfed.org/
- `fredgraph.csv?id=<시리즈>` 는 키 없이 사용 가능(API 는 키 필요). DGS3MO/2/10/30, T10Y2Y, T10Y3M, DEXKOUS, VIXCLS, BAMLH0A0HYM2, DFF, NASDAQCOM, SP500·DJIA(라이선스로 최근 10년만)
- 시점: 빈티지(ALFRED) 미사용. 관측일 다음 영업일 18:00 ET 로 사용 가능 시점을 가정

### 10. OpenDART — https://opendart.fss.or.kr/
- 한국 재무정보 공식 원천. **API 키 필요**(무료 가입). 일 20,000회 한도
- 구현: 키가 있으면 corpCode(고유번호↔종목코드) 수집. 재무제표 전량 수집은 호출량이 커서(약 2,600개 기업 × 연·분기 × 보고서) 별도 승인 후 단계적으로 확장할 예정 (미구현)

### 11. 기타 무료 대량 데이터 검토
- Kaggle 등 비공식 미국 주가 덤프: 출처·조정 방식이 불명확하고 재배포 라이선스가 불분명해 채택하지 않음
- 유료 대안(미국 생존편향 해소): CRSP(학술), Norgate Data(상장폐지 포함, 연 수백 달러), Sharadar/Nasdaq Data Link SEP(폐지 포함), EODHD(폐지 티커 포함). 기대 효과는 README 의 '한계' 절 참고
