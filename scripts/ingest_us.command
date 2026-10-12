#!/bin/bash
# 미국 전체 수집 (수 시간 소요. 중단 후 다시 실행하면 받은 종목은 건너뛰고 이어서 진행)
cd "$(dirname "$0")/.." && caffeinate -i .venv/bin/marketlab ingest --market US --mode full
.venv/bin/marketlab ingest --market IND
read -n 1 -s -r -p "완료. 아무 키나 누르면 닫힙니다."
