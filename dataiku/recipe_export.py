# Python 레시피 "hrli_export": 승인되어 반영된 데이터를 분석용 데이터셋으로 내보낸다.
#   입력: 관리 폴더 hrli_data
#   출력: 아래 OUTPUTS 의 데이터셋(필요 없는 것은 목록에서 지우고 출력에서도 뺀다)
# 내보낸 데이터셋은 Dataiku의 차트·대시보드·다른 레시피에서 자유롭게 쓸 수 있다.
# 개인정보(사번·성명·개인 이력)가 들어 있으므로 프로젝트 권한을 본인과 교육 담당자로 제한한다.
import dataiku

from hrli import config, flow

DATA_FOLDER = "hrli_data"
OUTPUTS = [
    "hrli_employee",      # 직원 최신 정보
    "hrli_snapshot",      # 월별 명부 스냅샷
    "hrli_training",      # 분석용 교육 이력(분류·분석용 시간·현재 조직 포함, 통계 반영 건만)
    "hrli_training_all",  # 전체 교육 이력(미매칭·통계 제외 포함, 반영 여부 표시)
    "hrli_course",        # 교육 과정 기준정보(표준 교육명·분류)
    "hrli_batches",       # 업로드 배치 이력
    "hrli_change_log",    # 데이터 수정 이력
]

config.configure(data_dir=dataiku.Folder(DATA_FOLDER).get_path())
tables = flow.export_tables()
for name in OUTPUTS:
    dataiku.Dataset(name).write_with_schema(tables[name])
