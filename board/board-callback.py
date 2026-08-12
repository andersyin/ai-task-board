#!/usr/bin/env python3
"""board-callback — CP9 完成回调管理工具。

用法:
  python3 board-callback.py list                          # 列出所有回调
  python3 board-callback.py list --agent trae              # 按创建方过滤
  python3 board-callback.py list --pending-only            # 仅未确认
  python3 board-callback.py count --agent trae             # 待处理计数（轮询用）
  python3 board-callback.py ack cb-T52-1786287330          # 确认回调
  python3 board-callback.py purge --days 7                 # 清理7天前已确认

退出码: 0=成功; 1=错误; 2=无匹配
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CN_TZ = timezone(timedelta(hours=8))
CALLBACKS_FILE = "callbacks.jsonl"


def load_callbacks(board_root: Path) -> list[dict]:
    """加载全部回调记录。"""
    cb_path = board_root / CALLBACKS_FILE
    if not cb_path.exists():
        return []
    callbacks = []
    for line in cb_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            callbacks.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return callbacks


def save_callbacks(board_root: Path, callbacks: list[dict]) -> None:
    """回写全部回调记录。"""
    cb_path = board_root / CALLBACKS_FILE
    with open(cb_path, "w", encoding="utf-8") as f:
        for cb in callbacks:
            f.write(json.dumps(cb, ensure_ascii=False) + "\n")


def cmd_list(args, board_root: Path) -> int:
    callbacks = load_callbacks(board_root)
    if args.agent:
        callbacks = [cb for cb in callbacks if cb.get("created_by") == args.agent]
    if args.pending_only:
        callbacks = [cb for cb in callbacks if not cb.get("acknowledged")]

    if not callbacks:
        print("无回调记录" if not args.agent else f"无 {args.agent} 的回调记录")
        return 2

    print(f"\n回调列表 ({len(callbacks)} 条{'，待确认 ' + str(sum(1 for c in callbacks if not c.get('acknowledged'))) + ' 条' if args.pending_only or args.agent else ''})")
    print(f"{'='*70}\n")
    for cb in callbacks:
        ack = "✅" if cb.get("acknowledged") else "⏳"
        task_id = cb.get("task_id", "?")
        created_by = cb.get("created_by", "?")
        executor = cb.get("executor", "?")
        trigger = cb.get("trigger", "?")
        triggered_at = cb.get("triggered_at", "?")
        task_status = cb.get("task_status", "?")
        print(f"  {ack} {cb['callback_id']}")
        print(f"     任务: {task_id} (状态: {task_status})")
        print(f"     创建方: {created_by} | 执行方: {executor}")
        delivery = cb.get("delivery", "?")
        print(f"     交付: {delivery} | 触发: {trigger} @ {triggered_at}")
        if cb.get("acknowledged"):
            print(f"     确认: {cb.get('acknowledged_by', '?')} @ {cb.get('acknowledged_at', '?')}")
        print()
    return 0


def cmd_count(args, board_root: Path) -> int:
    """快速计数，适合轮询脚本使用。"""
    callbacks = load_callbacks(board_root)
    if args.agent:
        callbacks = [cb for cb in callbacks if cb.get("created_by") == args.agent]
    pending = [cb for cb in callbacks if not cb.get("acknowledged")]
    print(f"{len(pending)}")
    return 0 if pending else 2


def cmd_ack(args, board_root: Path) -> int:
    """确认回调。acknowledge ALL matching callbacks（防 legacy 重复 ID）。"""
    from board_transition import now_cn
    callbacks = load_callbacks(board_root)
    found_count = 0
    already_acked = 0
    for cb in callbacks:
        if cb["callback_id"] == args.callback_id:
            if cb.get("acknowledged"):
                already_acked += 1
                continue
            cb["acknowledged"] = True
            cb["acknowledged_by"] = args.agent or "user"
            cb["acknowledged_at"] = now_cn().isoformat(timespec="seconds")
            found_count += 1
    if found_count == 0 and already_acked > 0:
        print(f"⚠️ {args.callback_id} 已被确认 ({already_acked} 条)")
        return 0
    if found_count == 0:
        print(f"❌ 未找到回调: {args.callback_id}", file=sys.stderr)
        return 1
    save_callbacks(board_root, callbacks)
    task_ids = set(cb.get("task_id", "?") for cb in callbacks if cb["callback_id"] == args.callback_id)
    print(f"✅ 已确认 {args.callback_id} ({found_count} 条, 任务: {', '.join(task_ids)})")
    return 0


def cmd_purge(args, board_root: Path) -> int:
    """清理已确认的旧回调。"""
    callbacks = load_callbacks(board_root)
    cutoff = datetime.now(CN_TZ) - timedelta(days=args.days)
    kept = []
    purged = 0
    for cb in callbacks:
        if cb.get("acknowledged"):
            ack_at = cb.get("acknowledged_at", "")
            try:
                ack_dt = datetime.fromisoformat(ack_at)
                if ack_dt.tzinfo is None:
                    ack_dt = ack_dt.replace(tzinfo=CN_TZ)
                if ack_dt < cutoff:
                    purged += 1
                    continue
            except (ValueError, TypeError):
                pass  # 解析失败则保留
        kept.append(cb)
    save_callbacks(board_root, kept)
    print(f"✅ 清理 {purged} 条已确认回调（>{args.days}天），保留 {len(kept)} 条")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CP9 完成回调管理工具")
    parser.add_argument("--board-root", default=str(Path(__file__).parent), help="board 根目录")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出回调")
    p_list.add_argument("--agent", default="", help="按创建方过滤")
    p_list.add_argument("--pending-only", action="store_true", help="仅未确认")

    p_count = sub.add_parser("count", help="待处理计数")
    p_count.add_argument("--agent", default="", help="按创建方过滤")

    p_ack = sub.add_parser("ack", help="确认回调")
    p_ack.add_argument("callback_id", help="回调 ID")
    p_ack.add_argument("--agent", default="", help="确认方")

    p_purge = sub.add_parser("purge", help="清理旧回调")
    p_purge.add_argument("--days", type=int, default=7, help="清理 N 天前已确认的回调")

    args = parser.parse_args()
    board_root = Path(args.board_root).resolve()

    if args.cmd == "list":
        return cmd_list(args, board_root)
    elif args.cmd == "count":
        return cmd_count(args, board_root)
    elif args.cmd == "ack":
        return cmd_ack(args, board_root)
    elif args.cmd == "purge":
        return cmd_purge(args, board_root)
    return 1


if __name__ == "__main__":
    sys.exit(main())
