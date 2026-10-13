#!/bin/bash
# 모든 데이터 증분 갱신 + 품질 검증
cd "$(dirname "$0")/.." && .venv/bin/marketlab update --all
read -n 1 -s -r -p "완료. 아무 키나 누르면 닫힙니다."
