#!/usr/bin/env python3
"""board-review-escalate.py — 验收超时自动升级处置（2026-08-12 治理新增，P0-2）。

背景（实证）：2026-08-12 前 awaiting_review 超时仅产生告警（REVIEW_OVERDUE /
REVIEW_BACKLOG），无自动处置。codex 额度断档 + qwenwork 未登录时，5 个 L3 任务
积压 60h+ 无人验收，靠人工逐批清理。

本脚本为确定性处置器，由哨兵/值班端每轮调用：
- 扫描 awaiting_review 超阈值（默认 4h）的任务
- 对每个任务：计算 eligible reviewer（复用 eligible_reviewers 与 WAKE_REVIEW_ORDER）
- 有 reviewer → 自动执行 board-wake.py wake --urgent --mode review 唤醒验收端
  （re-route；wake ledger 自带 25min 去重，幂等安全）
- 无 reviewer（验收通道堵死）→ 记录台账 + 输出需要用户 authorize-reviewer 的
  处置建议（不自动 cancel，尊重用户终态权）
- 桌面通知：仅当本轮出现「堵死」或「唤醒失败」时触发

用法：
  python3 board-review-escalate.py [--board-root DIR] [--threshold-hours 4]
                                   [--dry-run] [--max-wakes N]
默认 dry-run=false，但 max-wakes 限制单轮唤醒数（默认 2），避免惊群。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import importlib.util  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


_na = _load("board_next_action_esc", "board-next-action.py")
WAKE_REVIEW_ORDER = _na.WAKE_REVIEW_ORDER
WAKE_TARGET_MODELS = _na.WAKE_TARGET_MODELS

_tr = _load("board_transition_esc", "board_transition.py")
eligible_reviewers = _tr.eligible_reviewers

CN_TZ = timezone(timedelta(hours=8))

LEDGER = None  # set in main
WAKE_LEDGER = None  # set in main


def now() -> datetime:
    return datetime.now(CN_TZ)


def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=CN_TZ)


def append_ledger(entry: dict) -> None:
    global LEDGER
    if LEDGER is None:
        return
    try:
        with open(LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_status(task_dir: Path) -> dict | None:
    p = task_dir / "status.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def recent_wake(task: str, channel: str, minutes: int = 25) -> bool:
    """复用 wake ledger 的 25min 去重窗口：避免同一任务同端被反复唤醒。"""
    global WAKE_LEDGER
    if WAKE_LEDGER is None or not WAKE_LEDGER.exists():
        return False
    cutoff = now() - timedelta(minutes=minutes)
    try:
        for line in WAKE_LEDGER.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (e.get("task") == task and e.get("channel") == channel
                    and e.get("kind") == "wake" and e.get("ok") is True):
                ts = parse_iso(e.get("ts"))
                if ts is not None and ts > cutoff:
                    return True
    except OSError:
        return False
    return False


def pick_reviewer(status: dict, card_text: str):
    """按 WAKE_REVIEW_ORDER 选第一个 eligible 的验收端。返回 (channel, model) 或 (None, None)。"""
    try:
        routes = eligible_reviewers(
            status,
            status.get("executor"),
            executor_model=status.get("executor_model"),
            card_text=card_text,
            include_authorization=False,
        )
    except Exception:
        return None, None
    by_reviewer = {r.get("reviewer"): r for r in routes if r.get("reviewer")}
    for reviewer in WAKE_REVIEW_ORDER:
        route = by_reviewer.get(reviewer)
        if not route:
            continue
        model = route.get("reviewer_model") or WAKE_TARGET_MODELS.get(reviewer)
        if model:
            return reviewer, model
    return None, None


def run_wake(board_root: Path, channel: str, task: str, model: str, dry_run: bool) -> tuple[int, str]:
    """调用 board-wake.py wake --urgent --mode review 唤醒验收端。"""
    if dry_run:
        return 0, "DRY_RUN"
    prompt_file = board_root / "tasks" / task / "card.md"
    cmd = [
        sys.executable, str(board_root / "board-wake.py"),
        "wake", "--channel", channel, "--task", task,
        "--prompt-file", str(prompt_file),
        "--urgent", "--mode", "review",
        "--target-model", model,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        tail = (proc.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else (proc.stderr or "").strip().splitlines()[-1:] or "?"
        return proc.returncode, str(detail)[:200]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"EXC:{type(exc).__name__}"


def notify_desktop(title: str, body: str) -> None:
    """macOS 桌面通知（失败静默）。"""
    try:
        script = (
            f'display notification "{body}" with title "{title}"'
        )
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass


def scan(board_root: Path, threshold_hours: float, max_wakes: int, dry_run: bool) -> int:
    tasks_root = board_root / "tasks"
    if not tasks_root.exists():
        print("NOGO: tasks 目录不存在")
        return 2

    escalate_rows = []
    stalled = []  # 无 reviewer 的堵死任务
    wake_failures = []
    wakes_issued = 0

    for task_dir in sorted(tasks_root.iterdir()):
        if not task_dir.is_dir() or task_dir.name.startswith((".", "_")):
            continue
        status = load_status(task_dir)
        if not status or status.get("status") != "awaiting_review":
            continue
        # 超时起点：claimed_at 优先，其次 status_changed_at
        start = parse_iso(status.get("claimed_at")) or parse_iso(status.get("status_changed_at"))
        if start is None:
            continue
        age_h = (now() - start).total_seconds() / 3600
        if age_h < threshold_hours:
            continue
        card_text = ""
        card_path = task_dir / "card.md"
        if card_path.exists():
            try:
                card_text = card_path.read_text(encoding="utf-8")
            except OSError:
                card_text = ""
        task_id = status.get("id", task_dir.name)
        reviewer, model = pick_reviewer(status, card_text)
        if reviewer is None:
            stalled.append((task_id, age_h))
            continue
        if recent_wake(task_id, reviewer):
            escalate_rows.append({"task": task_id, "age_h": round(age_h, 1),
                                  "reviewer": reviewer, "action": "skip_recent_wake"})
            continue
        if wakes_issued >= max_wakes:
            escalate_rows.append({"task": task_id, "age_h": round(age_h, 1),
                                  "reviewer": reviewer, "action": "deferred_max_wakes"})
            continue
        rc, detail = run_wake(board_root, reviewer, task_id, model, dry_run)
        action = "wake_ok" if rc == 0 else "wake_failed"
        escalate_rows.append({"task": task_id, "age_h": round(age_h, 1),
                              "reviewer": reviewer, "action": action, "rc": rc, "detail": detail})
        if rc == 0:
            wakes_issued += 1
        else:
            wake_failures.append((task_id, reviewer, rc, detail))

    # 台账
    if escalate_rows or stalled:
        append_ledger({
            "ts": now().isoformat(timespec="seconds"),
            "kind": "review_escalate",
            "threshold_hours": threshold_hours,
            "dry_run": dry_run,
            "escalated": escalate_rows,
            "stalled": [{"task": t, "age_h": round(a, 1)} for t, a in stalled],
        })

    # 输出
    if not escalate_rows and not stalled:
        print("OK: 无超时验收任务")
        return 0
    for row in escalate_rows:
        print(f"ESCALATE [{row['task']}] {row['age_h']}h → {row['reviewer']}: {row['action']}"
              + (f" (rc={row['rc']} {row.get('detail','')})" if row["action"] == "wake_failed" else ""))
    for task_id, age_h in stalled:
        print(f"STALLED [{task_id}] {age_h:.1f}h 无 eligible reviewer → 需用户 authorize-reviewer 或 cancel")

    if stalled:
        notify_desktop("公告牌验收通道堵死",
                       f"{len(stalled)} 个任务无验收端可唤醒（如 {stalled[0][0]} {stalled[0][1]:.0f}h）"
                       "，需人工授权或终止")
    for task_id, reviewer, rc, detail in wake_failures:
        notify_desktop("公告牌验收唤醒失败",
                       f"{task_id} → {reviewer} 唤醒 rc={rc}: {detail}")
    return 1 if (stalled or wake_failures) else 0


def main() -> int:
    global LEDGER, WAKE_LEDGER
    parser = argparse.ArgumentParser(description="验收超时自动升级处置（P0-2）")
    parser.add_argument("--board-root", default=str(Path(__file__).parent))
    parser.add_argument("--threshold-hours", type=float, default=4.0,
                        help="awaiting_review 超时阈值（小时），默认 4")
    parser.add_argument("--max-wakes", type=int, default=2,
                        help="单轮最多唤醒数（默认 2，防惊群）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只扫描输出，不实际唤醒")
    args = parser.parse_args()

    board_root = Path(args.board_root).resolve()
    LEDGER = board_root / "_reports" / "review-escalate-ledger.jsonl"
    WAKE_LEDGER = board_root / ".." / "_reports" / "board-wake-ledger.jsonl"
    WAKE_LEDGER = WAKE_LEDGER.resolve()
    return scan(board_root, args.threshold_hours, args.max_wakes, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
