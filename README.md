# market-research-lab

한국·미국 주식의 장기 일봉 데이터를 수집·정제·검증·저장하고 자동으로 갱신하는 개인 연구용 데이터 인프라입니다.
대상 환경은 MacBook M1(8GB RAM, 256GB SSD)입니다.
대용량 시세는 Parquet 파일로 저장하고, DuckDB 는 메타데이터 관리와 조회에 씁니다. 전체 데이터를 pandas 메모리에 올리지 않습니다.

- 원천 조사표: [docs/data_source_matrix.md](docs/data_source_matrix.md)
- 머신러닝 연계 데이터 계약: [docs/data_contract.md](docs/data_contract.md)
- 실행 후 생성되는 보고서: `reports/quality_report.md`, `reports/disk_plan.md`, `reports/storage_advice.md`(위험할 때만)

---

## 1. 설치 (처음 한 번)

터미널을 열고 프로젝트 폴더에서 실행합니다. Finder 에서 `scripts/setup.command` 를 더블클릭해도 됩니다.

```bash
cd ~/market-research-lab          # 저장소를 받은 위치
./scripts/setup.command
```

설치 스크립트가 하는 일:
1. Apple Silicon 네이티브 터미널인지 확인합니다(Rosetta 이면 경고).
2. `uv` 가 있으면 Python 3.12 가상환경을 만들고, 없으면 Python 3.11 이상을 찾아 `.venv` 를 만듭니다.
3. `requirements.lock` 의 고정 버전을 설치합니다. 모두 M1 arm64 휠이 제공되는 버전입니다.
4. `.env.example` 을 `.env` 로 복사합니다.
5. `doctor`(환경), `sources`(원천 접근), `disk-plan`(저장공간)을 차례로 실행합니다.

Python 3.11 이상이 없다면 다음 중 하나로 설치합니다: `curl -LsSf https://astral.sh/uv/install.sh | sh` (권장) 또는 `brew install python@3.12`

`.env` 에서 선택적으로 설정하는 값:
```bash
MARKETLAB_DATA_DIR=                       # 비우면 ./data (외장 SSD 로 옮길 때 사용)
MARKETLAB_SEC_USER_AGENT="홍길동 you@example.com"   # SEC 수집 시 필수 (SEC 정책)
DART_API_KEY=                             # OpenDART (선택)
```

## 2. 사용법 (복사해서 실행)

```bash
source .venv/bin/activate                 # 또는 각 명령 앞에 .venv/bin/ 을 붙임

marketlab doctor                          # macOS·CPU·Python·패키지·메모리·디스크·외장장치 점검
marketlab sources                         # 데이터 원천 실제 접근 점검
marketlab disk-plan                       # 저장공간 계획 + 외장 SSD 필요 여부 (SEC 포함: --with-sec)

marketlab ingest --market KR --mode full  # 한국 전체 (marcap 1995~ + KRX 목록·상장폐지·지수)  ≈ 5분, 0.9GB
marketlab ingest --market US --mode full  # 미국 전체 (Nasdaq Trader 목록 + yfinance 전 종목)   ≈ 수 시간
marketlab ingest --market IND             # 지수·환율·금리 (FRED, Yahoo, KRX)
marketlab update --all                    # 증분 갱신 + 최근 거래일 재검증 + 품질 검증
marketlab retry                           # 실패·차단된 종목만 재시도
marketlab stop                            # 진행 중인 수집을 안전하게 중지 (상태 저장 후 종료)
marketlab validate [--deep]               # 품질 검증 (--deep: 모든 파일 해시 재계산)
marketlab status [--ack]                  # 수집 현황 (--ack: 보류 경고 확인 처리)
marketlab dashboard                       # 관리 화면 → http://localhost:8501
marketlab schedule install                # launchd 자동 갱신 설치 (기본 07:30, 19:30)
marketlab schedule status | uninstall
marketlab import-stooq ~/Downloads/d_us_txt.zip   # 직접 받은 Stooq 대량 파일 가져오기
marketlab sec --tickers                   # SEC 티커↔CIK
marketlab sec --facts --submissions --approve-large   # SEC 재무 대용량 (disk-plan --with-sec 확인 후)
marketlab standardize                     # 원본에서 표준화 데이터 전체 재생성
```

Python 을 몰라도 쓸 수 있도록 `scripts/` 에 더블클릭 실행 파일을 두었습니다:
`setup.command`, `ingest_kr.command`, `ingest_us.command`(잠자기 방지 `caffeinate` 포함), `update.command`, `dashboard.command`, `schedule_install.command`, `move_data.command`

미국 전체 수집은 Yahoo 호출 제한 때문에 오래 걸립니다. 중간에 멈추거나 Mac 을 꺼도 괜찮습니다. 같은 명령을 다시 실행하면 이미 받은 종목은 건너뛰고 이어서 진행합니다.
`--limit 500` 처럼 나눠서 실행할 수도 있습니다.

## 3. 처리 흐름

```
원천 ──► data/raw/ (원본 그대로·해시·manifest) ──► data/standardized/ (표준화 Parquet + 품질등급)
                                                      │
              metadata/catalog.duckdb ◄───────────────┤ 증권 마스터·이력·기업행위·수집상태·품질이슈·거래일
                                                      ▼
                                   marketlab.api.MarketData (시점 일관 조회) / 대시보드
```

- **원본 보존**: marcap Parquet 는 변환하지 않고 그대로 둡니다. 과거 연도 파일이 바뀌면 이전 판을 `raw/marcap/_history/` 에 보관합니다.
- **증분·멱등**: 원격 blob sha 가 같으면 다시 받지 않습니다. US 는 마지막 성공일에서 최근 N 거래일 앞부터 다시 받아 덮어쓰고, 표준화 단계에서 키 기준으로 최신본 하나만 남깁니다.
- **재개**: 종목 단위 상태(`ingestion_items`)와 실행 기록(`ingestion_runs`)을 남깁니다. 비정상 종료된 실행은 다음 실행 때 `interrupted` 로 정리합니다.
- **재검증**: US 는 종목마다 `revalidate_every_days`(30일) 주기로 전체 이력을 다시 받아 과거 값 변경을 `data_quality_issues(source_revision)` 에 기록합니다. KR 은 원격 해시 비교로 과거 연도 수정을 감지합니다.
- **호출 제한**: 원천별 최소 간격을 `config/settings.yaml` 에서 정합니다. 429·5xx 는 지수 백오프로 재시도하고, 차단이 계속되면 서킷 브레이커가 해당 원천만 안전하게 중단합니다. 로그인·CAPTCHA 는 우회하지 않습니다.
- **메모리**: DuckDB `memory_limit=2GB`, `threads=2`, `preserve_insertion_order=false`, 임시공간 4GB 상한으로 시작합니다. 연도 단위로 스트리밍 처리합니다(실측: 한국 전체 표준화 4분).
- **날짜**: 거래일은 거래소 현지 날짜입니다. 지표에는 실제 사용 가능 시각(`available_at`, UTC)이 붙으며 서머타임을 반영합니다. 장 마감과 지연 시간이 지나지 않은 일봉은 `is_final=false` 로 둡니다.

## 4. 저장공간과 외장 SSD

- 예산: 프로젝트가 추가로 쓰는 공간은 30GB 까지이고, 내장 SSD 여유는 항상 20GB 이상 남깁니다. 실제 여유가 60GB 보다 적으면 예산을 비례해서 줄입니다.
- 대용량 작업 전에는 `DiskGuard.preflight()` 로 확인하고, 쓰는 동안에는 `checkpoint()` 로 반복 확인합니다. 위험하면 신규 다운로드를 멈추고 상태를 저장한 뒤 `reports/storage_advice.md` 를 만듭니다. 이 경고는 CLI, 대시보드, macOS 알림에 표시됩니다.
- 그때 실행 중이 아니어서 받지 못한 경고는 다음 실행 때 CLI 와 대시보드에 다시 표시합니다(`pending_alerts`).
- 프로그램은 파일을 삭제하거나 저장장치를 구매하지 않습니다.
- 외장 SSD 로 옮기기:
  ```bash
  ./scripts/move_data.command /Volumes/MarketSSD/market-research-lab-data
  ```
  복사가 끝나면 `.env` 의 `MARKETLAB_DATA_DIR` 를 바꿉니다. 소스코드는 수정하지 않습니다. 기존 폴더는 확인한 뒤 직접 지우세요.
  외장장치가 분리되었거나 쓸 수 없으면 `StorageUnavailable` 로 쓰기를 즉시 멈춥니다. 쓰기는 임시 파일에 한 뒤 rename 하는 방식이라 중간에 끊겨도 기존 파일이 깨지지 않습니다.

**예상 용량** (실측과 추정): 한국 전체 0.93GB(실측), 미국 yfinance 약 3GB(±40% 추정, 첫 배치 후 실측치로 자동 보정), 지표 0.05GB, SEC 재무 약 5~7GB(선택).
여유가 60GB 인 맥북이라면 SEC 까지 포함해도 예산 안이므로 **외장 SSD 는 지금 필요하지 않습니다.** `disk-plan` 이 실측 기준으로 다시 판정합니다.

## 5. 자동 갱신 (launchd)

```bash
marketlab schedule install --times 07:30 19:30
```
- 07:30(KST) 실행은 미국 전 거래일 일봉이 확정된 뒤이고, 19:30 실행은 한국 장 마감 후 원천(marcap)이 갱신된 뒤입니다.
- Mac 이 켜져 있고 로그인된 동안에만 실행됩니다. 클라우드 서비스처럼 항상 돌지 않습니다.
  예정 시각에 잠자기 중이었다면 깨어난 직후 한 번 실행하고, 전원이 꺼져 있었다면 다음 로그인 때 `RunAtLoad` 로 밀린 갱신을 복구합니다.
- 네트워크가 없으면 건너뜁니다. 파일 잠금으로 중복 실행을 막고, 로그는 `logs/`(`marketlab.jsonl`, `launchd.*.log`)에 남습니다.
- 갱신이 끝나면 품질 검증을 실행하고, 데이터가 3 거래일 이상 지연되면 경고합니다.

## 6. 관리 화면

`marketlab dashboard` → http://localhost:8501 (본인 컴퓨터에서만 접속)
- **데이터 현황**: 종목 수, 행 수, 기간, 용량, 최신 업데이트, 원천별 성공·실패
- **확보 범위**: 시장별·연도별 종목 수, 상장·폐지 구분, 결측 구간
- **종목 조회**: 검색, 캔들 차트(원가/조정가 선택), 페이지 단위 OHLCV, 기업행위, 이력, 품질 이슈
- **수집 관리**: 전체 수집·재개, 증분, 재시도, 중지, 진행률·남은 개수
- **시스템 상태**: RAM, CPU, SSD, 프로젝트 용량, 외장 SSD 권고, 원천 점검, 오류 로그
- **품질**: 품질 보고서

화면은 필요한 기간만 DuckDB 로 조회하며, 전체 데이터를 브라우저로 보내지 않습니다.

## 7. 머신러닝에서 읽기

```python
from marketlab.api import MarketData
md = MarketData()
sid = md.resolve("005930", on="2020-01-02", country="KR")          # → 'KR-005930-19950502'
md.prices(sid, "2018-01-01", "2018-12-31", adjust="all_corporate", as_of="2018-12-31")
md.universe("2008-10-24", country="KR")                            # 그날 거래된 종목 (상장폐지 포함)
md.indicators(["KRX:KS11", "MARCAP:KOSPI_ADV_RATIO"], "2020-01-01", "2020-12-31", as_of="2020-12-31")
```
자세한 내용은 [docs/data_contract.md](docs/data_contract.md) 를 보세요.

## 8. 검증

```bash
.venv/bin/python -m pytest -q          # 24개: 실제 데이터 검증(data) + 로직 + 네트워크(network)
```
실제 데이터 테스트는 수집된 데이터가 있을 때만 실행됩니다. 확인 항목: marcap 1995·최신 연도 로딩, 원본과 표준화 행 수 일치, 삼성전자 실제 종가, 2018 액면분할의 시점별 조정, 코드 재사용 구분과 상장폐지 상태, KOSPI 지수 값과 사용 가능 시점, 중복 없음, 증분 멱등성, DuckDB 메모리 설정.
US 로직 테스트는 Yahoo 응답 함수만 대체하고, 재개·중지·서킷 브레이커·분할 역산·멱등성은 실제 코드 경로로 확인합니다.

## 9. 한계와 추가 데이터

**미국 상장폐지 종목(가장 큰 한계)**
- Nasdaq Trader 는 현재 목록만 제공하고, Yahoo 는 현재 티커 보유 기업의 이력만 줍니다.
  따라서 1990~2020년대에 상장폐지·합병된 미국 종목 대부분이 빠지고, 티커가 재사용된 경우 과거 기업 이력도 사라집니다.
- 결과적으로 미국 데이터는 **생존편향**이 있습니다. 모멘텀·소형주·가치 전략의 성과가 과대평가될 수 있습니다.
- 이 시스템을 도입한 날 이후로는 목록에서 사라지는 종목을 스냅샷으로 기록하므로, 앞으로 발생하는 상장폐지는 확보됩니다.
- 유료 대안과 기대 효과:
  - CRSP(학술 라이선스): 1925년 이후 전 종목과 상장폐지 수익률. 생존편향 완전 해소
  - Norgate Data(연 약 $300~600): 상장폐지 포함 1990년 이후, 원가·조정가 구분
  - Nasdaq Data Link Sharadar SEP/SF1(월 수십 달러): 1998년 이후 폐지 포함 일봉과 재무
  - EODHD(월 약 $20~80): 폐지 티커 포함

**한국**
- marcap 은 1995-05-02 부터 시작합니다. 그 이전 상장폐지 종목 1,120건은 목록만 있고 가격이 없습니다.
- 배당 데이터가 없어 한국 총수익률은 계산할 수 없습니다(OpenDART 배당 공시로 확장 가능).
- 기업행위는 거래소 기준가 변화로 추정한 것(inferred)이며 공시와 대조되지 않았습니다.
- 50개 증권은 마지막 거래 이후 사유를 확인하지 못해 `unknown` 상태입니다(일부는 최근 거래정지).
- KRX 지수 캐시는 2026-09-17 이후 갱신되지 않았습니다. 그래서 전 종목에서 직접 계산한 시장 지표를 함께 제공합니다.

**기타**
- Stooq 종목별 CSV 는 2026년부터 API 키(CAPTCHA 발급)를 요구한다는 보고가 있습니다. 대량 파일은 직접 받아 `import-stooq` 로 가져오세요.
- 재무: SEC 는 승인 후 대량 수집을 구현했습니다. OpenDART 는 키가 필요하며 현재 corpCode 만 구현되어 있습니다.

## 10. 디렉터리

```
config/settings.yaml      설정 (경로, 예산, DuckDB, 원천별 호출 간격, 갱신 시각, 검증 기준)
src/marketlab/
  collectors/             marcap, krx_cache, nasdaqtrader, yfinance_us, stooq, indicators, sec_edgar, opendart
  transforms/             kr_prices(표준화·증권 마스터·기업행위·거래일), kr_breadth, us_prices
  validation/checks.py    데이터셋 검증, 원천 간 비교, 품질 지표, 보고서
  storage/                catalog(DuckDB), diskguard, diskplan, parquet_io
  scheduling/launchd.py   macOS 자동 갱신
  dashboard/app.py        Streamlit
  cli/main.py             marketlab 명령
  api.py                  머신러닝 조회 API
data/  raw | standardized | curated | metadata | quarantine | tmp     (Git 제외)
reports/ logs/ models/                                               (Git 제외)
```
