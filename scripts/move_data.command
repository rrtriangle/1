#!/bin/bash
# 데이터 디렉터리를 외장 SSD 등 새 위치로 옮긴다 (소스코드 수정 불필요).
# 사용: ./scripts/move_data.command /Volumes/MarketSSD/market-research-lab-data
set -euo pipefail
cd "$(dirname "$0")/.."
DEST="${1:-}"
if [ -z "$DEST" ]; then echo "사용: $0 <새 데이터 경로>"; exit 1; fi
SRC=$(.venv/bin/python -c 'from marketlab.config import get_settings; print(get_settings().paths.data)')
if pgrep -f "marketlab.cli.main (ingest|update)" >/dev/null; then echo "수집 작업이 실행 중입니다. 먼저 'marketlab stop' 후 종료를 기다리세요."; exit 1; fi
mkdir -p "$DEST"
echo "복사: $SRC -> $DEST"
rsync -a "$SRC"/ "$DEST"/
# 원본은 사용자가 확인 후 직접 삭제 (자동 삭제하지 않음)
if grep -q '^MARKETLAB_DATA_DIR=' .env 2>/dev/null; then
  sed -i '' "s#^MARKETLAB_DATA_DIR=.*#MARKETLAB_DATA_DIR=$DEST#" .env
else
  echo "MARKETLAB_DATA_DIR=$DEST" >> .env
fi
.venv/bin/marketlab status >/dev/null && echo "완료. .env 가 새 경로를 가리킵니다. 확인 후 기존 폴더($SRC)는 직접 삭제하세요."
echo "launchd 를 쓰는 경우 다시 설치: ./scripts/schedule_install.command"
