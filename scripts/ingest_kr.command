#!/bin/bash
# 한국 전체 수집 (중단 후 다시 실행하면 이어서 진행)
cd "$(dirname "$0")/.." && .venv/bin/marketlab ingest --market KR --mode full
read -n 1 -s -r -p "완료. 아무 키나 누르면 닫힙니다."
