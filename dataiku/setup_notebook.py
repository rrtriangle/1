# Dataiku Python 노트북(웹앱과 같은 코드 환경)에서 한 번만 실행하는 초기 설정.
# DB를 만들고 첫 시스템 관리자 이메일을 등록한다. 이후 사용자는 화면의 데이터 관리 > 사용자에서 추가한다.
import dataiku

from hrli import config, seed

DATA_FOLDER = "hrli_data"
config.configure(data_dir=dataiku.Folder(DATA_FOLDER).get_path())

seed.init(admin_email="본인이메일@사내도메인")

# 화면을 먼저 살펴보고 싶으면 위 줄 대신 가상 데이터까지 넣는다(실제 데이터와 섞지 말 것).
# seed.init(demo=True, admin_email="본인이메일@사내도메인")
