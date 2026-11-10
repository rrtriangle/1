# Dataiku Python 노트북(웹앱과 같은 코드 환경)에서 한 번만 실행하는 초기 설정.
# DB를 만들고, 지금 노트북을 실행하는 Dataiku 사용자(본인)를 시스템 관리자로 등록한다.
# 이후 사용자는 웹앱의 데이터 관리 > 사용자 화면에서 추가한다.
import dataiku

from hrli import config, seed

DATA_FOLDER = "hrli_data"                 # 관리 폴더 이름
ADMIN_EMAIL = "본인이메일@사내도메인"        # 본인 이메일(나중에 이메일 인증을 켤 때도 쓰인다)

config.configure(data_dir=dataiku.Folder(DATA_FOLDER).get_path())
my_login = dataiku.api_client().get_auth_info()["authIdentifier"]
print("Dataiku 로그인 ID:", my_login)

seed.init(admin_email=ADMIN_EMAIL, admin_dss_login=my_login)

# 화면을 먼저 살펴보고 싶으면 위 줄 대신 가상 데이터까지 넣는다(실제 데이터와 섞지 말 것. 나중에 폴더를 비우고 다시 시작).
# seed.init(demo=True, admin_email=ADMIN_EMAIL, admin_dss_login=my_login)
