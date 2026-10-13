import psutil
import streamlit as st

from marketlab.config import get_settings
from marketlab.dashboard.common import cat_df, research_df
from marketlab.research import runner
from marketlab.storage.diskguard import GB, assess, dir_size, is_external_volume

S = get_settings()
st.title("시스템 관리")
vm = psutil.virtual_memory()
a = assess()
c = st.columns(5)
c[0].metric("RAM 사용", f"{vm.percent:.0f}%", help=f"{vm.used / GB:.1f}/{vm.total / GB:.1f}GB, 가용 {vm.available / GB:.1f}GB")
c[1].metric("CPU", f"{psutil.cpu_percent(interval=0.3):.0f}%")
c[2].metric("디스크 여유", f"{a.disk_free_gb:.1f}GB")
c[3].metric("프로젝트 사용량", f"{a.project_used_gb:.2f}GB", help=f"유효 예산 {a.budget_gb:.1f}GB (수집+연구 합산)")
c[4].metric("저장 위치", "외장" if is_external_volume(S.paths.data) else "내장")
if a.risk:
    st.error("저장공간 위험: " + "; ".join(a.reasons))

st.subheader("용량 내역")
parts = {"원본(raw)": S.paths.raw, "표준화": S.paths.standardized, "특징(features)": S.paths.data / "features",
         "모델": S.paths.data / "research_models", "백테스트 결과": S.paths.data / "research_backtests",
         "메타데이터·연구DB": S.paths.metadata, "임시(tmp)": S.paths.tmp, "로그": S.paths.logs}
st.dataframe([{"항목": k, "GB": round(dir_size(v) / GB, 3)} for k, v in parts.items()], hide_index=True)
st.caption("외장 SSD 는 저장공간만 늘려 줍니다. 학습 메모리(RAM 8GB) 부족은 특징 축소·기간 분할·표본 간격 확대로 해결하며, "
           "그래도 부족하면 고용량 RAM 컴퓨터나 클라우드 CPU 가 대안입니다(자동으로 유료 자원을 쓰지 않음).")

st.subheader("수집·학습 상태")
st.dataframe(cat_df("SELECT source, status, started_at, items_total, items_ok, items_failed, message FROM ingestion_runs WHERE status='running'"), hide_index=True)
for m in ("KR", "US"):
    stt = runner.load_state(m)
    if stt.get("steps"):
        done = sum(1 for v in stt["steps"].values() if v.get("done"))
        st.write(f"{m} 연구 단계 완료 {done}개 · 최대 메모리 {max((v.get('rss_gb') or 0) for v in stt['steps'].values()):.2f}GB")
st.dataframe(research_df("SELECT exp_id, train_seconds, peak_rss_gb, status FROM experiments ORDER BY created_at DESC LIMIT 20"), hide_index=True)

st.subheader("경고·오류")
st.dataframe(cat_df("SELECT created_at, level, kind, message FROM pending_alerts WHERE NOT acknowledged ORDER BY created_at DESC LIMIT 50"), hide_index=True)
logf = S.paths.logs / "marketlab.jsonl"
if logf.exists():
    errs = [ln for ln in logf.read_text(errors="replace").splitlines()[-2000:] if '"ERROR"' in ln or "failed" in ln]
    st.code("\n".join(errs[-40:]) or "(최근 오류 없음)")
adv = S.paths.reports / "storage_advice.md"
if adv.exists():
    with st.expander("외장 SSD 권고"):
        st.markdown(adv.read_text(encoding="utf-8"))
st.subheader("접속 범위")
st.write("기본은 이 컴퓨터에서만 접속(127.0.0.1)입니다. 같은 와이파이의 갤럭시·아이패드에서 보려면 `marketlab dashboard --lan` 으로 실행하세요. "
         "그 경우 같은 네트워크의 누구나 이 화면에 접속할 수 있으므로, 집 등 신뢰할 수 있는 네트워크에서만 사용하세요.")
