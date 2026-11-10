# Dataiku DSS 설치·운영 안내

Dataiku DSS 14 기준으로 작성했다. Dataiku 공식 문서(doc.dataiku.com의 DSS 14 문서, developer.dataiku.com)와 대조해
아래 "문서 확인 결과"의 항목을 확인했다. **다만 실제 DSS에서는 아직 실행해 보지 못했다.** 로컬에서는 Dataiku처럼 미리 만든
Dash 앱을 하위 경로에 띄우고 Dataiku 로그인 확인만 흉내 내서, Dash 4.5와 Dash 3.4 두 버전으로 동작을 확인했다.
Dataiku 안에서 오류가 나면 메시지를 그대로 전달해 주면 맞춰 고친다.

## 문서 확인 결과

| 항목 | 결과 | 근거 |
|---|---|---|
| Dash 웹앱에서 `app`을 Dataiku가 미리 만들어 줌 | 일치 | Dash 웹앱 튜토리얼이 `app.layout`만 지정하는 방식 |
| 브라우저의 Dataiku 사용자 확인(`get_auth_info_from_browser_headers`, `authIdentifier`) | 일치 | DSS 14 "Webapps and security"의 Dash 웹앱 예시 |
| 노트북·레시피에서 내 로그인 ID(`get_auth_info()["authIdentifier"]`) | 일치 | Python API 레퍼런스 |
| 관리 폴더 경로(`Folder.get_path()`) | 일치. 서버 로컬 파일시스템 폴더에서만 동작 | Python API 레퍼런스 |
| 메일 채널 발송(`channel.send(project_key, to, subject, body)`) | 일치 | Python API 레퍼런스 |
| 프로젝트 변수(`dataiku.get_custom_variables()`), 데이터셋 읽기·쓰기 | 일치 | Python API 레퍼런스 |
| 공개 웹앱 등록 메뉴 | **고침**. 이전 안내의 메뉴 위치가 틀렸음 | DSS 14 "Public webapps" |
| 데이터셋 읽기 기본 옵션 | **고침**. 기본값은 형식을 다시 추정해 사번 앞자리 0이 사라질 수 있어, 스키마 형식대로 읽게 바꿈 | Python API 레퍼런스 |
| 지원 Dash 버전 | DSS 13에서 Dash 3 호환 템플릿으로 바뀜. Dash 3.x 권장 | DSS 13 변경 기록 |

## 전체 구조

```
[엑셀 업로드]                 [Python 레시피]          [관리 폴더 hrli_data]        [Python 레시피]          [분석용 데이터셋]
 roster_upload    ─┐
                   ├─→  hrli_stage  ─→  DB 파일(검토 대기)  ─→  hrli_export  ─→  hrli_training 등 7개
 training_upload  ─┘        │                  ↑
                            └→ hrli_stage_log  │ 검토·승인·수정·복구
                                         [Dash 웹앱]  ← 본인이 Dataiku 계정으로 접속
```

- **올리기**: Dataiku에 엑셀을 데이터셋으로 올리고 `hrli_stage` 레시피를 돌린다.
- **검토·승인**: 웹앱 > 데이터 관리에서 확인하고 승인한다. 승인 전에는 통계에 반영되지 않는다.
- **활용**: 웹앱 화면으로 분석하고, 필요하면 `hrli_export`로 데이터셋을 만들어 Dataiku 차트·대시보드에도 쓴다.

웹앱 화면에서 파일을 직접 올리는 방법도 그대로 남아 있다. 어느 쪽으로 올려도 같은 검토 절차를 거친다.

## 1. 코드 환경

Python 코드 환경에 다음 패키지를 넣는다(`requirements.txt`와 같다).

```
pandas  numpy  openpyxl  plotly  dash  scikit-learn  networkx
```

- **Dash는 3.x를 권장한다**(예: `dash>=3,<4`). Dataiku가 DSS 13에서 Dash 웹앱 템플릿을 Dash 3에 맞췄고,
  이 앱도 Dash 3.4에서 동작을 확인했다. Dash 4.5에서도 확인했지만 Dataiku 쪽 지원 여부는 문서에서 확인하지 못했다.
- 그 밖에 확인한 버전은 Python 3.13, plotly 7.1, pandas 3.0, scikit-learn 1.9, networkx 3.7이다.
  더 낮은 버전에서는 실행해 보지 않았다. 최소 Python 3.10을 권장한다.

## 2. 프로젝트 라이브러리에 코드 올리기

프로젝트 > Libraries 에서 `python/` 아래에 `hrli` 폴더를 통째로 만든다(하위 `web` 폴더 포함).
zip으로 받았다면 파일 이름 끝의 `.txt`를 지워 `.py`로 되돌린 뒤 올린다.

```
python/
  hrli/
    __init__.py  analysis.py  auth.py  classify.py  config.py  db.py  flow.py  ingest.py
    metrics.py  sample_data.py  seed.py  summary.py
    web/
      __init__.py  admin.py  app.py  charts.py  common.py  pages.py  person_plan.py  style.py
```

## 3. 관리 폴더 만들기

Flow에서 관리 폴더(Managed folder)를 만든다. 이름은 `hrli_data`로 한다.

- **DSS 서버의 로컬 파일시스템에 있는 폴더여야 한다.** DB 파일(`hrli.sqlite3`)과 업로드 원본(`raw/`)이 여기에 쌓인다.
  S3 같은 원격 저장소 폴더에서는 쓸 수 없다.
- 직원 개인정보가 들어가므로 프로젝트 권한을 본인(나중에는 교육 담당자)으로 제한한다.
- 폴더를 지우면 누적 데이터가 모두 사라진다. DSS 백업 대상에 포함되는지 확인한다.

## 4. 초기 설정(한 번만)

웹앱과 같은 코드 환경으로 Python 노트북을 열고 `dataiku/setup_notebook.py` 내용을 붙여 넣는다.
`ADMIN_EMAIL`을 본인 이메일로 바꿔 실행하면 DB가 만들어지고, **노트북을 실행한 Dataiku 계정이 시스템 관리자로 등록된다.**

## 5. 웹앱 만들기

1. 프로젝트 > Webapps > 새 웹앱 > Code webapp > **Dash** 를 고른다.
2. Settings에서 코드 환경을 1번의 환경으로 지정한다.
3. Python 탭의 예시 코드를 모두 지우고 `dataiku/webapp_backend.py` 내용을 붙여 넣는다.
4. DSS 주소가 `https://`가 아니면 마지막 줄을 `https=False`로 바꾼다.
5. 저장하고 백엔드를 시작한 뒤 웹앱을 연다. 본인 Dataiku 계정으로 바로 들어가져야 한다.

웹앱 백엔드는 한 프로세스로 둔다. DB 파일 하나를 쓰는 구조이기 때문이다.

**웹앱이 실행되는 계정**: Dataiku 웹앱의 코드는 기본으로 "웹앱을 마지막으로 수정한 사람"의 권한으로 돈다(run-as-user).
관리 폴더 쓰기, 데이터셋 읽기, 메일 채널 발송이 모두 이 계정 권한으로 이루어진다. 본인이 만들고 수정하면 본인 권한이다.
다른 사람이 웹앱 코드를 수정하면 실행 계정이 바뀌므로, 웹앱 수정 권한도 최소한으로 둔다.

## 6. 데이터 올리기: 레시피 방식

### 데이터셋 준비

엑셀을 업로드해 데이터셋 두 개를 만든다. 이름은 `roster_upload`(직원 명부), `training_upload`(교육 이력)로 한다.
다른 이름을 쓰면 레시피 코드 맨 위의 이름도 바꾼다.

- **사번은 문자열로**: 데이터셋 스키마에서 사번 컬럼 형식을 string으로 둔다. 레시피는 스키마 형식대로 읽으므로
  여기서 string으로 두면 앞자리 0이 보존된다. 업로드할 때 Dataiku가 형식을 자동으로 정하니 한 번 확인한다.
- **매월 갱신**: 같은 데이터셋에 새 파일을 덮어 올리면 된다. 원본 파일은 레시피가 관리 폴더에 따로 보관한다.
- **과거 명부 한꺼번에**: 명부에 `기준월` 컬럼(예: 2025-01)을 넣어 여러 달을 한 파일로 올리면, 레시피가 달별로 나눠
  오래된 달부터 따로 올린다. `기준월` 컬럼이 없으면 프로젝트 변수 `hrli_roster_month`의 값을 쓴다.

### 레시피 hrli_stage

Python 레시피를 만들고 코드에 `dataiku/recipe_stage.py`를 붙여 넣는다.

- 입력: `roster_upload`, `training_upload`
- 출력: 관리 폴더 `hrli_data`, 새 데이터셋 `hrli_stage_log`
- 실행하면 검토 대기 배치가 생긴다. 같은 내용을 다시 돌려도 중복으로 올라가지 않는다.
- `hrli_stage_log`에서 이번 실행 결과(배치 번호, 상태, 건수)를 볼 수 있다.

프로젝트 변수(Settings > Variables)로 바꿀 수 있는 값:

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `hrli_roster_month` | 없음 | 명부 기준월(YYYY-MM). 명부에 `기준월` 컬럼이 없으면 필수 |
| `hrli_roster_file_type` | `full` | 명부가 전체 명부면 `full`, 일부만이면 `incremental` |
| `hrli_training_file_type` | `full` | 교육 이력이 과거 전체를 포함하면 `full`, 신규만이면 `incremental` |

### 검토·승인

웹앱 > 데이터 관리 > 업로드·검토·승인에서 배치를 확인하고 승인한다.
과거 명부를 여러 달 올렸다면 **"확인할 것이 없는 검토 대기 배치 모두 승인"** 버튼으로 한 번에 승인할 수 있다.
'중복 의심'처럼 판단이 필요한 배치는 남겨 두고, 그 뒤의 같은 종류 배치도 순서를 지키기 위해 남긴다.

**처음 적재 순서**: 직원 명부를 먼저 올려 승인하고, 그다음 교육 이력을 올린다.
교육 이력을 먼저 올리면 사번이 모두 '명부 미매칭'으로 분류된다(명부가 들어오면 자동으로 통계에 포함된다).

### 레시피 hrli_export (선택)

Dataiku 차트·대시보드나 다른 레시피에서 데이터를 쓰고 싶을 때만 만든다. 코드는 `dataiku/recipe_export.py`.

- 입력: 관리 폴더 `hrli_data`
- 출력: 아래 데이터셋 중 필요한 것. 필요 없는 것은 코드의 `OUTPUTS` 목록에서도 지운다.

| 데이터셋 | 내용 |
|---|---|
| `hrli_employee` | 직원 최신 정보 |
| `hrli_snapshot` | 월별 명부 스냅샷 |
| `hrli_training` | 분석용 교육 이력(통계 반영 건만, 분류·분석용 시간·현재 조직 포함) |
| `hrli_training_all` | 전체 교육 이력(미매칭·통계 제외 포함, 반영 여부 표시) |
| `hrli_course` | 교육 과정 기준정보 |
| `hrli_batches` | 업로드 배치 이력 |
| `hrli_change_log` | 데이터 수정 이력 |

내보낸 데이터셋에도 개인정보가 들어 있다. 웹앱처럼 권한을 나눌 수 없으니, 이 프로젝트에 접근할 수 있는 사람은
모두 볼 수 있다는 점을 염두에 둔다.

### 시나리오로 묶기(선택)

시나리오를 만들어 "`hrli_stage_log` 빌드 → (승인은 사람이) → `hrli_export` 출력 빌드" 순서로 두면 매월 버튼 한 번으로
올리기와 내보내기를 할 수 있다. 승인은 사람이 해야 하므로 두 단계를 별도 시나리오로 나누는 것이 편하다.

## 7. 사용자와 로그인

### 기본: Dataiku 계정 로그인

- 웹앱을 열면 Dataiku에 로그인한 계정을 확인해, 등록된 사용자면 바로 들어간다.
- 웹앱 > 데이터 관리 > 사용자에서 사람을 추가할 때 **Dataiku 로그인 ID**를 적으면 그 계정으로 들어올 수 있다.
- Dataiku 계정이 있어도 여기 등록되지 않은 사람은 들어오지 못한다.
- 사용자를 "중지"하면 로그인 중이어도 다음 동작부터 막힌다.

### 선택: 이메일 인증 로그인 (Dataiku 계정이 없는 사람용)

기본으로 꺼져 있다. 계정이 없는 교육 담당자 등을 추가할 때만 켠다. 다음 세 가지가 모두 필요하다.

1. **메일 발송 방법**: `webapp_backend.py`의 `mailer`를 Dataiku 메일 채널이나 사내 SMTP로 바꾼다.
   - Dataiku 메일 채널: DSS 관리자가 만든 채널 ID가 필요하고, 웹앱 실행 계정(run-as-user)이 그 채널을 쓸 권한이 있어야 한다.
   - 사내 SMTP: DSS 서버에서 SMTP 서버로 접속되는지, 발신 주소가 허용되는지 확인한다. 비밀번호를 코드에 적지 않는다.
2. **웹앱 공개 등록**: DSS 관리자가 **Administration > Settings > Security & Audit > Other security settings**의
   인증 예외 목록(authentication whitelist)에 `프로젝트키.웹앱ID`를 추가하고 웹앱 백엔드를 재시작한다.
   웹앱ID는 웹앱 주소에서 밑줄(_) 앞부분이다. 예: 주소가 `/projects/HRLI/webapps/kUDF1mQ_hrli/view`면 `HRLI.kUDF1mQ`.
3. **스위치 켜기**: 웹앱 > 데이터 관리 > 사용자 > "이메일 인증 로그인 켜기".

공개 등록을 하면 Dataiku는 접속자를 확인하지 않고, **이 앱의 이메일 인증이 유일한 관문**이 된다. 그래서 다음을 지킨다.

- 보안 담당 부서의 승인을 먼저 받는다(인사 데이터가 들어 있다).
- DSS가 HTTPS로 서비스되고, 사내망에서만 접속되는 주소인지 확인한다.
- 등록 인원은 최소로 하고, 퇴직·이동자는 바로 "중지"한다.

공개 등록만 하고 스위치를 켜지 않으면, 계정이 없는 사람은 웹앱 주소에 들어와도 로그인할 방법이 없다.
스위치만 켜고 공개 등록을 하지 않으면, 계정이 없는 사람은 웹앱 주소 자체에 들어오지 못한다.

인증 코드는 6자리 1회용이고 10분 뒤 만료되며 5번 틀리면 폐기된다. 60초 안에는 다시 보낼 수 없다.
스위치를 끄면 이미 보낸 코드도 모두 폐기된다.

## 문제가 생기면

| 증상 | 확인할 것 |
|---|---|
| 백엔드가 시작되지 않고 `No module named hrli` | 2번의 폴더 위치(`python/hrli/`)와 파일 확장자(.py) |
| `No module named dash` 등 | 웹앱·레시피·노트북의 코드 환경 지정 |
| `get_path` 관련 오류 | 관리 폴더가 서버 로컬 파일시스템인지(3번). 레시피를 컨테이너에서 실행하도록 설정했다면 로컬 실행으로 바꾼다 |
| 웹앱을 열면 "등록되어 있지 않습니다" | 사용자 화면의 Dataiku 로그인 ID가 실제 로그인 ID와 같은지. 처음이면 4번을 다시 확인 |
| 웹앱을 열면 로그인 버튼만 있고 눌러도 실패 | DSS 관리자 설정 Administration > Settings > Login & Security의 웹앱 보안 항목 "Hide access tokens"가 켜져 있으면 웹앱이 브라우저의 Dataiku 세션을 읽지 못할 수 있다. 그래도 안 되면 웹앱 Log 탭의 오류 메시지를 전달해 주면 맞춰 고친다 |
| 웹앱 화면이 일부만 뜨거나 버튼이 반응하지 않음 | 코드 환경의 dash 버전(3.x 권장) |
| 로그인 직후 다시 로그인 화면으로 돌아감 | HTTP로 접속 중인데 `https=True`로 둔 경우. 접속 주소와 설정을 맞춘다 |
| 레시피 실행 시 "기준월 값을 해석할 수 없습니다" | 명부의 기준월 값 형식(예: 2026-01) |
| 레시피 실행 시 "직원 명부는 기준월이 필요합니다" | 명부에 `기준월` 컬럼을 넣거나 프로젝트 변수 `hrli_roster_month`를 지정 |
| 교육 이력이 전부 '사번 미매칭' | 명부를 먼저 승인했는지, 사번 컬럼이 문자열인지 |
| 인증 코드가 안 옴 | 메일 발송 설정, 데이터 관리 > 업로드·수정 이력 > 접근 이력의 "인증 코드 발송 실패" 사유 |
