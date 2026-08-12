#!/bin/bash
# 清除可能泄漏的环境变量
unset PYTHONHOME
unset PYTHONPATH
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$KB_ROOT"
/usr/bin/python3 ".kb/board/board-lease-check.py"
