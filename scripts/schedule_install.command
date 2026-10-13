#!/bin/bash
# launchd 자동 갱신 설치 (기본 07:30, 19:30 — config/settings.yaml 의 schedule.times 로 변경)
cd "$(dirname "$0")/.." && .venv/bin/marketlab schedule install && .venv/bin/marketlab schedule status
read -n 1 -s -r -p "완료. 아무 키나 누르면 닫힙니다."
