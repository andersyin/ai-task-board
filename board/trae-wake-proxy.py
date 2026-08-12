#!/usr/bin/env python3
"""[已归档·文档用途] Trae wake proxy — 已被本地 shell 脚本替代。

实际使用的代理脚本：~/Library/Application Support/trae-wake/trae-wake-proxy.sh
触发目录：~/Library/Application Support/trae-wake/triggers/
launchd plist：~/Library/LaunchAgents/com.user.trae-wake-proxy.plist

v3 变更（2026-08-12）：
1. AppleScript keystroke return 被 TCC 拒绝 (-10004)，改用 cliclick kp:return (CGEvent API)
2. 触发目录从外接卷迁移到本地卷（~/Library/Application Support/trae-wake/triggers/），
   避免 launchd 代理访问外接卷时的 TCC "Operation not permitted"
3. launchd plist 改为直接运行 shell 脚本（/bin/bash），不经过 Python subprocess

本文件保留用于文档和手动测试参考，不再被 launchd 使用。

board-wake.py 的 wake_trae() 三层策略：
1. 直接：osascript activate + cliclick kp:return（从 TraeWork/有 CGEvent 权限的进程）
2. launchd 代理：触发文件 → WatchPaths → shell 脚本 → cliclick kp:return
3. 兜底：触发文件留待 TraeWork 轮询拾取（30min 内）

手动测试：
  # 写触发文件
  echo '{"task":"TEST","ts":"..."}' > ~/Library/Application\ Support/trae-wake/triggers/TEST.json
  # 手动运行 shell 代理
  bash ~/Library/Application\ Support/trae-wake/trae-wake-proxy.sh
"""
