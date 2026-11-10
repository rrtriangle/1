#!/bin/bash
# 1분봉 수집 (전 종목). 처음에는 최근 약 30일치를 받느라 몇 시간 걸립니다.
# 중단 후 다시 실행하면 받은 종목은 건너뛰고 이어서 진행합니다. (--max-minutes 0 = 시간 제한 없이 끝까지)
cd "$(dirname "$0")/.." && caffeinate -i .venv/bin/marketlab minute --max-minutes 0
read -n 1 -s -r -p "완료. 아무 키나 누르면 닫힙니다."
