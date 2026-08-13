#!/bin/bash
# 清除可能泄漏的环境变量
unset PYTHONHOME
unset PYTHONPATH
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [ -z "${KB_ROOT:-}" ]; then
  echo "board-lease-check-runner.sh: KB_ROOT is not set" >&2
  exit 1
fi
cd "$KB_ROOT" || exit 1
/usr/bin/python3 ".kb/board/board-lease-check.py"
