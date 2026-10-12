#!/bin/bash
# 관리 화면 실행 → 브라우저에서 http://localhost:8501
cd "$(dirname "$0")/.." && .venv/bin/marketlab dashboard
