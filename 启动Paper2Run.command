#!/bin/bash
# Paper2Run one-click launcher (macOS): double-click in Finder.
cd "$(dirname "$0")"
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "[!] 未找到 Python 3.11+，请先安装：https://www.python.org/downloads/"
  read -n 1 -s -r -p "按任意键关闭 / press any key to close"
  exit 1
fi
"$PY" scripts/launch.py
STATUS=$?
if [ $STATUS -ne 0 ] && [ -t 0 ]; then
  read -n 1 -s -r -p "按任意键关闭 / press any key to close"
fi
exit $STATUS
