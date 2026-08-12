#!/usr/bin/env python3
"""board-indirect-verify — 标记间接验证通过的上游任务。

场景：A 委托 B 出题（delivery=pass_to_next），B submit 后 A 答题并通过验收。
此时 A 的原任务（B 的出题任务）处于 awaiting_review，但已被下游 PASS 间接验证。

逻辑：对每个 awaiting_review + delivery=pass_to_next 的任务，
检查 executor 是否创建了其他任务且该任务已 review PASS。
若是，标记 indirectly_verified=true。

用法：
  python3 board-indirect-verify.py --board-root <board> [--apply]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="间接验证标记")
    parser.add_argument("--board-root", default=str(Path(__file__).parent))
    parser.add_argument("--apply", action="store_true", help="实际写入标记（默认 dry-run）")
    args = parser.parse_args()
    board_root = Path(args.board_root).resolve()
    tasks_root = board_root / "tasks"

    if not tasks_root.exists():
        return 0

    # 构建索引：created_by → list of (task_id, status, review_result)
    executor_tasks: dict[str, list[tuple[str, str, str | None]]] = {}
    for task_dir in sorted(tasks_root.iterdir()):
        if not task_dir.is_dir() or task_dir.name.startswith("."):
            continue
        sj = task_dir / "status.json"
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        creator = st.get("created_by", "")
        if creator:
            executor_tasks.setdefault(creator, []).append(
                (st.get("id", task_dir.name), st.get("status", ""), st.get("review_result"))
            )

    marked = 0
    for task_dir in sorted(tasks_root.iterdir()):
        if not task_dir.is_dir() or task_dir.name.startswith("."):
            continue
        sj_path = task_dir / "status.json"
        try:
            st = json.loads(sj_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        # 只检查 awaiting_review + pass_to_next 的任务
        if st.get("status") != "awaiting_review":
            continue
        if st.get("delivery") != "pass_to_next":
            continue
        if st.get("indirectly_verified"):
            continue  # 已标记

        executor = st.get("executor", "")
        if not executor:
            continue

        # 检查 executor 是否创建了已 review PASS 的任务
        downstream = executor_tasks.get(executor, [])
        has_pass = any(
            result == "pass" and status == "done"
            for _, status, result in downstream
        )
        if not has_pass:
            continue

        # 标记
        st["indirectly_verified"] = True
        st["indirectly_verified_by"] = f"downstream PASS by {executor}"
        if args.apply:
            sj_path.write_text(
                json.dumps(st, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        marked += 1
        print(f"✅ {st['id']}: indirectly_verified (executor={executor} 有下游 PASS)")

    if not args.apply and marked > 0:
        print(f"(dry-run, {marked} 个任务待标记，加 --apply 写入)")
    elif marked == 0:
        print("无间接验证可标记")

    return 0


if __name__ == "__main__":
    sys.exit(main())
