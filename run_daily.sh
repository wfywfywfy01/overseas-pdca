#!/bin/bash
# 每日质检快捷入口
# 用法: ./run_daily.sh            → 质检昨天
#        ./run_daily.sh 2026-05-04 → 质检指定日期

cd "$(dirname "$0")"
DATE=${1:-}
if [ -n "$DATE" ]; then
    venv/bin/python3 orchestrator/run.py "$DATE"
else
    venv/bin/python3 orchestrator/run.py
fi
