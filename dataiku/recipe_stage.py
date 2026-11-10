# Python 레시피 "hrli_stage": 업로드한 엑셀 데이터셋을 검토 대기 배치로 올린다.
#   입력: 직원 명부 데이터셋, 교육 이력 데이터셋(둘 중 하나만 있어도 된다)
#   출력: 관리 폴더 hrli_data, 데이터셋 hrli_stage_log(이번 실행 결과 기록)
# 이 레시피는 통계에 바로 반영하지 않는다. 웹앱 > 데이터 관리에서 검토·승인해야 반영된다.
# 같은 내용을 다시 돌려도 중복으로 올라가지 않는다.
import dataiku
import pandas as pd

from hrli import config, db, flow

DATA_FOLDER = "hrli_data"
ROSTER_DATASET = "roster_upload"        # 직원 명부 데이터셋 이름. 없으면 None
TRAINING_DATASET = "training_upload"    # 교육 이력 데이터셋 이름. 없으면 None

# 프로젝트 변수(Settings > Variables)로 바꿀 수 있는 값
#   hrli_roster_month     : 명부 기준월(YYYY-MM). 데이터셋에 '기준월' 컬럼이 있으면 그 값을 쓴다
#   hrli_roster_file_type : full(전체 명부, 기본) / incremental / reprocess
#   hrli_training_file_type : full(과거 이력 포함, 기본) / incremental / reprocess
v = dataiku.get_custom_variables()

config.configure(data_dir=dataiku.Folder(DATA_FOLDER).get_path())
db.init_db()
try:
    user = dataiku.api_client().get_auth_info()["authIdentifier"]
except Exception:  # noqa: BLE001
    user = "recipe"

rows = []
if ROSTER_DATASET:
    df = dataiku.Dataset(ROSTER_DATASET).get_dataframe()
    rows += flow.stage_dataframe(df, ROSTER_DATASET, "roster", v.get("hrli_roster_file_type", "full"), user,
                                 month=v.get("hrli_roster_month"))
if TRAINING_DATASET:
    df = dataiku.Dataset(TRAINING_DATASET).get_dataframe()
    rows += flow.stage_dataframe(df, TRAINING_DATASET, "training", v.get("hrli_training_file_type", "full"), user)

log = pd.DataFrame(rows, columns=["source", "kind", "month", "rows", "batch_id", "status", "note"])
log.insert(0, "run_at", pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"))
dataiku.Dataset("hrli_stage_log").write_with_schema(log)
