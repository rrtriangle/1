# Dataiku Dash 웹앱의 "Python" 탭에 이 파일 내용을 그대로 붙여 넣는다.
# 전제: 프로젝트 라이브러리(Libraries > python/)에 hrli 폴더가 올라가 있고,
#       웹앱 코드 환경에 dash, plotly, pandas, numpy, openpyxl, scikit-learn, networkx 가 설치되어 있다.
# Dataiku가 `app`(Dash 객체)을 미리 만들어 주므로 여기서 새로 만들지 않는다.
import dataiku

from hrli import auth
from hrli.web.app import attach

# 1) 저장 위치: DSS 서버 로컬 파일시스템에 있는 관리 폴더(DB 파일과 업로드 원본이 여기에 쌓인다)
DATA_FOLDER = "hrli_data"            # 관리 폴더 이름 또는 ID
data_dir = dataiku.Folder(DATA_FOLDER).get_path()

# 2) 인증 메일 발송 방법: 아래 둘 중 하나만 남긴다.
#    (가) DSS 관리자가 만들어 둔 메일 채널 사용
mailer = auth.dataiku_mailer(channel_id="사내메일채널ID", project_key=dataiku.default_project_key())
#    (나) 사내 SMTP 서버 직접 사용. 비밀번호가 필요하면 코드에 적지 말고 프로젝트 비밀 변수나 환경변수로 넣는다.
# mailer = auth.smtp_mailer(host="smtp.사내도메인", sender="hr-learning@사내도메인", port=25)

# 3) 화면·로그인·접근 통제 연결. DSS가 HTTPS로 서비스되면 https=True 로 둔다.
attach(app, data_dir=data_dir, mailer=mailer, https=True)
