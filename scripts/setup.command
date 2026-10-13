#!/bin/bash
# market-research-lab 최초 설치 (더블클릭 또는 터미널에서 ./scripts/setup.command)
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== market-research-lab 설치 =="
ARCH=$(uname -m)
if [ "$(uname)" = "Darwin" ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = "1" ]; then
  echo "경고: Rosetta(x86_64) 터미널입니다. Apple Silicon 네이티브 터미널에서 실행하세요."
fi
echo "CPU 아키텍처: $ARCH"

PY=""
if command -v uv >/dev/null 2>&1; then
  echo "uv 로 Python 3.12 가상환경 생성"
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -r requirements.lock
  uv pip install --python .venv/bin/python --no-deps -e .
else
  for c in python3.13 python3.12 python3.11 /opt/homebrew/bin/python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then PY="$c"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "Python 3.11 이상이 필요합니다. 다음 중 하나를 설치한 뒤 다시 실행하세요:"
    echo "  1) uv (권장):  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "  2) Homebrew:   brew install python@3.12"
    exit 1
  fi
  echo "사용 Python: $($PY --version) ($PY)"
  "$PY" -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.lock
  .venv/bin/pip install --no-deps -e .
fi

[ -f .env ] || cp .env.example .env
# SEC EDGAR 정책: 연락 가능한 User-Agent 필요 (공개 저장소에 올리지 않도록 .env 에만 저장)
if ! grep -q '^MARKETLAB_SEC_USER_AGENT=.*@' .env; then
  echo
  echo "SEC EDGAR 수집에는 이름과 이메일이 필요합니다 (SEC 정책, .env 에만 저장되며 Git 에 올라가지 않음)."
  read -r -p "영문 이름 (예: SEOKKI HONG): " SEC_NAME
  read -r -p "이메일: " SEC_EMAIL
  if [ -n "$SEC_NAME" ] && [ -n "$SEC_EMAIL" ]; then
    grep -v '^MARKETLAB_SEC_USER_AGENT=' .env > .env.tmp || true
    echo "MARKETLAB_SEC_USER_AGENT=\"$SEC_NAME $SEC_EMAIL\"" >> .env.tmp
    mv .env.tmp .env
  fi
fi
echo
echo "== 환경 점검 =="
.venv/bin/marketlab doctor | head -80
echo
echo "== 데이터 원천 접근 점검 =="
.venv/bin/marketlab sources || true
echo
echo "== 저장공간 계획 =="
.venv/bin/marketlab disk-plan || true
echo
echo "설치 완료. 다음 단계: ./scripts/ingest_kr.command (한국 전체 수집)"
