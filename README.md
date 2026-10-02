# 버크만 팀 리포트 분석기 (Dataiku용)

팀장·파트장이 **팀의 성향**과 **팀원 개인의 성향**을 쉽게 파악하도록 돕는 Dataiku 웹앱입니다.
버크만 맵과 조직지향점을 중심으로 보여주고, 직업 흥미는 참고 정보로만 제공합니다.
사내 메일로 받은 **인증번호로 로그인**하고, 로그인한 사람의 직책에 따라 **볼 수 있는 범위가 제한**됩니다.

```
[버크만 JSON/PDF] ──업로드──▶ 관리 폴더 birkman_reports
                                   │
[수기 보정 엑셀(선택)] ────────────┤   01_recipe_parse_birkman.py (Python 레시피)
                                   ▼
                     birkman_scores (1인 1행)  +  birkman_parse_log (파일별 결과)
                                   │
[조직 엑셀 org_members] ───────────┤   02_webapp_dash.py (Dash 웹앱)
                                   ▼
                  이메일 인증 → 권한 범위 확인 → 팀 리포트 화면
```

| 파일 | 용도 | Dataiku에서 넣을 곳 |
|---|---|---|
| `dss/01_recipe_parse_birkman.py` | JSON/PDF → 점수 데이터셋 | Python 레시피 |
| `dss/02_webapp_dash.py` | 로그인 + 팀 리포트 화면 | Dash 웹앱의 Python 탭 |
| `templates/org_members_template.csv` | 조직 정보 양식 | 업로드 데이터셋 `org_members` |
| `templates/birkman_manual_input_template.csv` | 수기 보정 양식 (선택) | 업로드 데이터셋 `birkman_manual_input` |
| `tools/inspect_json_keys.py` | JSON 키 구조만 추출 (값은 가림) | PC에서 실행 (형식 공유용) |
| `tools/make_sample_data.py` | 가상 테스트 데이터 생성 | PC에서 실행 (테스트용) |

코드 파일 2개(`dss/` 폴더)는 각각 **파일 하나만 붙여넣으면 동작**하도록 만들었습니다. 메일로 코드를 옮길 때 이 두 파일만 보내면 됩니다.

---

## 1. Dataiku 설정 순서

### ① 프로젝트 만들기 + 권한 잠그기
- 새 프로젝트(예: `BIRKMAN_TEAM`)를 만듭니다.
- **프로젝트 권한은 HR 담당자와 관리자만** 갖게 하세요. 팀장들은 DSS 프로젝트가 아니라 웹앱으로만 접근합니다.
  (프로젝트 권한이 있으면 데이터셋을 직접 열 수 있습니다.)

### ② 개인 레포트 업로드 (관리 폴더)
1. Flow → **+ Dataset → Folder** → 이름: `birkman_reports`
2. 폴더를 열고 **JSON/PDF 파일을 드래그 앤 드롭**합니다. 여러 파일을 한 번에 올릴 수 있고, 하위 폴더도 괜찮습니다.
3. **파일명 규칙(권장): `사번_이름.json`** (예: `2023015_홍길동.json`)
   - 파일 안에 사번·메일이 없어도 파일명의 사번으로 조직 정보와 연결됩니다.
   - 파일명에 `시그니처`/`베이직`을 넣으면 진단 종류 판별에 도움이 됩니다.
4. 파일이 많거나 계속 추가된다면, 공유 폴더(네트워크 드라이브/SharePoint 등)를 DSS 관리자가 **폴더 연결(Connection)** 로 붙여 주면 업로드 없이 같은 레시피를 쓸 수 있습니다.

> **JSON이 있으면 JSON, 없으면 PDF를 그대로 올려도 됩니다.** 같은 사람의 파일이 둘 다 있으면 내용이 더 많은 쪽을 씁니다.
>
> | 항목 | JSON (사내 추출본) | PDF 직접 |
> |---|---|---|
> | 맵 색(사분면) + 레포트 문장 | O | O (맵 설명 페이지 문장으로 인식) |
> | 맵 위치(좌표) | O | X → 사분면 가운데 근처에 대략 표시 |
> | 조직지향점 점수 | O (그림 인식, 신뢰도 표시) | X (그림이라 글자로 못 읽음) → 필요하면 ④ 수기 보정 |
> | 직업 흥미 | O | 레포트에 "항목명 점수"가 한 줄로 있으면 O |
> | 구성요소 11개 | 지금 JSON에는 없음 | 레포트에 "항목명 평소 욕구"가 한 줄로 있으면 O |
>
> 스캔한 이미지 PDF는 글자가 없어서 읽을 수 없습니다(로그에 "글자가 거의 없음"으로 표시).

### ③ 조직 정보 업로드
- `templates/org_members_template.csv` 양식으로 엑셀을 만들어 **+ Dataset → Upload your files** 로 올리고 이름을 `org_members`로 지정합니다.
- 컬럼: `사번, 이름, 이메일, 팀, 파트, 직책` (영문 `emp_id, name, email, team, part, role`도 인식)
- 직책 값: `팀장` / `파트장` / `팀원`
  (그룹장·실장, 셀장 같은 직책이 생기면 웹앱 상단의 `TEAM_LEADER_ROLES`, `PART_LEADER_ROLES`에 추가. 목록에 없는 직책은 팀원으로 취급)
- 엑셀(.xlsx)을 그대로 올려도 됩니다. 머리글이 한글이어도 인식합니다.
- 팀장은 `파트`를 비워 둡니다.

### ④ (선택) 수기 보정 데이터
- PDF라서 맵이 비었거나, 파싱이 틀린 값을 고칠 때 사용합니다.
- `templates/birkman_manual_input_template.csv` 양식 → 업로드 데이터셋 `birkman_manual_input`
- `emp_id` 기준으로 **값을 넣은 칸만 덮어씁니다.** 파일이 없는 사람은 새로 추가됩니다.
- 색상 값: `red / green / yellow / blue` (또는 빨강·초록·노랑·파랑)

### ⑤ Python 레시피
1. `birkman_reports` 폴더 선택 → **+ Recipe → Python**
2. 입력: `birkman_reports` (+ 수기 보정을 쓰면 `birkman_manual_input`도 입력에 추가)
3. 출력: 새 데이터셋 `birkman_scores`, `birkman_parse_log` 두 개
4. 코드 칸 전체를 지우고 `dss/01_recipe_parse_birkman.py` 내용을 붙여넣고 **Run**
5. **`birkman_parse_log` 확인**
   - `status`: OK / PARTIAL(일부 누락) / ERROR
   - `matched_paths`: 각 항목을 JSON의 어느 키에서 읽었는지
   - `json_keys`: JSON 키 목록 → 자동 인식이 틀리면 레시피 상단 `JSON_PATH_OVERRIDES`에 경로를 지정
- 코드 환경: JSON만 쓰면 기본 환경으로 충분합니다. PDF를 쓰면 `pdfplumber` 또는 `pypdf`가 설치된 코드 환경이 필요합니다.

### ⑥ Dash 웹앱
1. 프로젝트 → **Code → Webapps → + New webapp → Code webapp → Dash**
2. Python 탭의 내용을 지우고 `dss/02_webapp_dash.py` 내용을 붙여넣습니다.
3. Settings
   - **Code env**: `dash`, `plotly`가 설치된 코드 환경
   - **Run backend as**: `birkman_scores`, `org_members`를 읽을 수 있는 계정
4. 상단 설정값 수정 (아래 2장)
5. **Start backend** → View에서 확인
6. 팀장들이 DSS 계정 없이 접속하려면 DSS 관리자가 이 웹앱을 **공개 웹앱(public webapp)으로 허용**해야 합니다.
   (DSS 계정이 있는 사람만 쓰려면 이 단계는 필요 없습니다.)

### ⑦ 자동 갱신 (선택)
- Scenario를 만들어 `birkman_scores`를 빌드하도록 하면, 폴더에 파일을 추가하고 시나리오만 돌리면 됩니다.
- 웹앱은 5분마다 데이터를 다시 읽습니다 (`DATA_CACHE_SEC`).

---

## 2. 웹앱 설정값 (`02_webapp_dash.py` 상단)

| 설정 | 설명 |
|---|---|
| `ALLOWED_EMAIL_DOMAINS` | 로그인 허용 도메인. 현재 `["dwchem.co.kr"]` |
| `ADMIN_EMAILS` | 모든 팀을 볼 수 있는 계정 (HR 담당자) |
| `TEAM_LEADER_ROLES`, `PART_LEADER_ROLES` | 직책명 목록. 현재 `["팀장"]`, `["파트장"]` |
| `ALLOW_MEMBER_SELF_VIEW` | `True`면 팀원은 **본인 결과만** 조회, `False`면 팀원 로그인 불가 |
| `MAIL_MODE` | `"auto"`(DSS 메일 채널 → SMTP 순서로 시도) / `"dss_channel"` / `"smtp"` |
| `DSS_MAIL_CHANNEL_ID` | DSS 관리자 화면에 등록된 메일 채널 ID (비우면 첫 번째 메일 채널) |
| `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM` … | 사내 SMTP를 직접 쓸 때 |
| `SESSION_HOURS` | 로그인 유지 시간 (탭을 닫으면 로그아웃) |

**메일 발송 확인 방법**: 어떤 방식이 되는지 모르면 `MAIL_MODE="auto"`로 두고 본인 메일로 인증번호를 요청해 보세요.
웹앱 **Log** 탭에 `[AUDIT] ... OTP_SENT ... dss_channel`처럼 성공한 방식이 남고, 실패하면 `[MAIL-ERROR]`에 이유가 나옵니다.
둘 다 안 되면 DSS 관리자에게 *"Administration → Settings → Notifications & Integrations에 메일 채널(SMTP)을 등록해 달라"* 고 요청하면 됩니다.

> SMTP 비밀번호는 코드에 적지 말고 **프로젝트 변수** `smtp_password`에 넣으세요.
> 프로젝트 변수 `birkman_app_secret`에 임의의 긴 문자열을 넣으면 웹앱을 재시작해도 로그인이 유지됩니다. 넣지 않으면 재시작 때 모두 로그아웃됩니다.

---

## 3. 권한과 보안

| 로그인한 사람 | 볼 수 있는 범위 |
|---|---|
| 팀장 | 자기 팀 전체 + 팀 안의 파트별 보기 |
| 파트장 | 자기 파트만 |
| 팀원 | 본인 결과만 (`ALLOW_MEMBER_SELF_VIEW=True`일 때) |
| `ADMIN_EMAILS` | 전체 팀 |

- 인증번호: 6자리, 5분 유효, 5회 틀리면 무효, 재발송 60초 대기, 메일당 시간당 5회 제한, 한 번 쓰면 재사용 불가
- 인증번호는 서버 메모리에 **해시로만** 보관하고 평문으로 저장하지 않습니다.
- 로그인 후에는 서명된 토큰을 사용하고, **모든 화면 요청마다 서버에서 권한을 다시 확인**합니다. 브라우저에서 팀 값을 조작해도 권한 밖 데이터는 오지 않습니다.
- 등록되지 않은 메일로 요청해도 같은 안내 문구를 보여줘서, 어떤 메일이 등록돼 있는지 알아낼 수 없습니다.
- 로그인, 실패, 팀 조회, 개인 조회 기록이 웹앱 로그에 `[AUDIT]`으로 남습니다.

---

## 4. 화면 구성

| 탭 | 내용 |
|---|---|
| **팀 개요** | 인원/진단 현황, 자동 해석 요약(팀 분위기, 사각지대, 팀 욕구, 평소와 욕구가 다른 팀원, 조직지향점, 리더와 팀원 비교), 색상 분포, 구성원 표 |
| **버크만 맵** | 팀원 전체를 한 맵에 표시. 흥미(✱)·평소(◆)·욕구(○)·스트레스(□) 선택, 평소→욕구 화살표 |
| **조직지향점** | 팀 평균, 구성원별 히트맵, 영역별 강점 인원 상위 3명 |
| **구성요소 (시그니처)** | 11개 구성요소 평소/욕구 팀 평균, 구성원별 히트맵, 평소와 욕구 차이가 큰 항목 |
| **개인별** | 개인 맵, 색상별 성향 요약, "이 팀원과 일할 때" 팁, 스트레스 신호, 구성요소 표, 조직지향점 |
| **직업 흥미 (참고)** | 10개 흥미 영역 히트맵 |

- 베이직 진단자는 맵·조직지향점 위주로, 시그니처 진단자는 구성요소까지 보여줍니다.
- 해석 문구는 사내 교육용 요약입니다. 정식 해석은 원 레포트와 버크만 코리아 자료를 기준으로 하세요.
  문구는 `02_webapp_dash.py`의 `COLOR_INFO`, `COMPONENTS`에서 바로 고칠 수 있습니다.

---

## 5. JSON 형식 맞추기 (중요)

**현재 사내 추출 JSON 형식(`employee_id`, `map_symbols`, `map_texts`, `interests`, `org_orientation`, `org_path`)은 그대로 인식됩니다.**

| JSON 항목 | 사용 방식 |
|---|---|
| `map_symbols.*.x / y` | 중앙이 0인 좌표(음수 포함) → 0~100으로 변환해 맵에 표시 (아래 "맵 좌표 기준") |
| `map_symbols.*.quadrant`, `map_texts.*.color` | 사분면 색 (노랑/파랑/빨강/초록) |
| `map_texts.*.raw` 의 `•` 항목 | 개인별 화면에 "레포트" 문장으로 표시 |
| `org_orientation.scores` | 조직지향점 4개 점수. `confidence`가 0.8 미만이면 화면에 확인 안내 |
| `interests` | 직업 흥미 10개 (숫자, 관리, 과학, 문학, 기술, 음악, 예술, 야외, 설득, 사회복지) |
| `org_path` | 소속 경로 (참고용. 권한 판단은 조직 엑셀 기준) |
| `report_type` | 시그니처 / 베이직 |

※ 시그니처 **구성요소 11개(평소/욕구 점수)** 는 현재 JSON에 없어서 "구성요소" 탭이 비어 있습니다. 추출기에서 `components` 항목을 추가하면 자동으로 표시됩니다.

**맵 좌표 기준**: 데이터 안에서는 모두 **왼쪽 아래 끝 = (0, 0), 오른쪽 위 끝 = (100, 100)** 으로 맞춥니다.
화면에는 숫자 없이 맵 그림으로만 보여주므로, 원본 맵의 정확한 끝 값을 몰라도 됩니다.
가운데가 0인 원본은 반경 `MAP_CENTER_HALF_RANGE`(기본 `"auto"` = 최소 4)로 나눠 변환하며, 이 값은 점이 가운데/가장자리로 조금 이동할 뿐 사분면(색)은 바뀌지 않습니다.

다른 형식의 파일이 생기면:

레시피는 JSON 키 이름(영문/한글)으로 맵·조직지향점·구성요소·직업흥미를 **자동 인식**합니다.
실제 파일 형식이 다르면 일부 항목이 빠질 수 있습니다. 아래 방법으로 키 구조만 공유해 주면 정확히 맞출 수 있습니다.

```bash
python tools/inspect_json_keys.py 2023015_홍길동.json --labels
```

값(점수·이름·메일)은 가려지고 `map.usual.x | number 0~100` 같은 **경로와 값 종류만** 출력됩니다.
(`--labels`는 구성요소 이름처럼 리스트 안의 이름표를 보여줍니다. 공유 전에 사람 이름이 없는지 확인해 주세요.)
Dataiku에서 레시피를 이미 돌렸다면 `birkman_parse_log`의 `json_keys` 컬럼에도 같은 정보가 있습니다.

---

## 6. PC에서 미리 테스트하기

```bash
pip install pandas dash plotly
python tools/make_sample_data.py sample                       # 가상 데이터 생성
python dss/01_recipe_parse_birkman.py sample/reports sample/out
BIRKMAN_LOCAL_DIR=sample/out python dss/02_webapp_dash.py     # http://127.0.0.1:8050
```

로컬에서는 메일 대신 화면에 인증번호가 표시됩니다. (예: `user001@dongwoo.example.com`은 팀장, `user002@...`는 파트장)
