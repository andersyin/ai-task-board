#!/usr/bin/env python3
"""原子认领公告牌任务：写 intent、锁定任务、重读并校验、一次性更新 status。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from board_integrity import get_task_integrity
from board_contract import (
    AGENT_CONTRACTS,
    CONTRACT_VERSION,
    NO_ELIGIBLE_INDEPENDENT_REVIEWER,
    atomic_write_text,
    canonical_agent,
    card_hash,
    effective_execute_level,
    eligible_reviewers,
    is_physical_task_dir,
    level_value,
    requires_independent_review,
    task_lock,
    validate_model_label,
    validate_task_id,
)
from board_transition import (
    TransitionError,
    commit_transition_locked,
    review_authorization_is_event_backed,
)

CN_TZ = timezone(timedelta(hours=8))


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def _role_constraints(card_text: str) -> tuple[bool, str | None]:
    locked = False
    preferred = None
    for raw in card_text.splitlines():
        line = raw.strip().lstrip("-").strip()
        if line.startswith("locked_executor:"):
            locked = line.split(":", 1)[1].strip().lower() == "true"
        elif line.startswith("preferred_executor:"):
            preferred = canonical_agent(line.split(":", 1)[1].strip())
    return locked, preferred


def _read_card_field(card_text: str, field: str) -> str | None:
    """从 card.md frontmatter 按行解析字段值（支持引号包裹的值）。"""
    in_frontmatter = False
    for raw in card_text.splitlines():
        line = raw.strip()
        if line == "---":
            in_frontmatter = not in_frontmatter
            continue
        if not in_frontmatter:
            continue
        if line.startswith(f"{field}:"):
            val = line.split(":", 1)[1].strip()
            # 去掉引号包裹
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            return val
    return None


def _check_family(board_root: Path, card_text: str) -> tuple[bool, str | None]:
    """检查点4/5：家族状态检查 + 根目标展示 + 负约束展示。
    返回 (allowed, reason)。frozen 家族拒绝认领。
    """
    family = _read_card_field(card_text, "family")
    if not family:
        return True, None  # 无家族，独立任务

    # 检查点4：读取家族状态
    family_status_path = board_root / "families" / family / "status.json"
    if family_status_path.exists():
        try:
            family_status = json.loads(family_status_path.read_text(encoding="utf-8"))
            fs = family_status.get("status", "active")
            if fs == "frozen":
                reason = family_status.get("rejections", [{}])[-1].get("reason", "未知原因") if family_status.get("rejections") else "未知原因"
                return False, f"家族 {family} 已冻结（被拒回溯中：{reason}），等待用户确认新方向后解冻"
            # 打印家族状态信息
            print(f"📌 家族: {family} (状态: {fs})")
        except (OSError, json.JSONDecodeError):
            pass
    else:
        print(f"📌 家族: {family} (无 family-status.json，视为 active)")

    # 打印根目标
    root_goal_card = board_root / "tasks" / family / "card.md"
    if root_goal_card.exists():
        try:
            root_text = root_goal_card.read_text(encoding="utf-8")
            # 提取根目标标题
            for line in root_text.splitlines():
                if line.startswith("# "):
                    print(f"   根目标: {line[2:].strip()}")
                    break
        except OSError:
            pass

    # 打印目标对齐声明
    goal_alignment = _read_card_field(card_text, "goal_alignment")
    if goal_alignment:
        print(f"   目标对齐: {goal_alignment}")

    # 检查点5：展示负约束
    neg = _read_card_field(card_text, "negative_constraints")
    if neg:
        print(f"🚫 负约束: {neg}")

    return True, None


def _dependencies_done(board_root: Path, depends_on: list[str]) -> tuple[bool, str | None]:
    tasks_root = board_root / "tasks"
    for dep_id in depends_on:
        if not validate_task_id(dep_id):
            return False, f"依赖 ID 非法: {dep_id!r}"
        dep_dir = tasks_root / dep_id
        if not is_physical_task_dir(dep_dir, tasks_root):
            return False, f"依赖不是 tasks/ 下的真实目录: {dep_id}"
        dep_path = dep_dir / "status.json"
        try:
            dep = json.loads(dep_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, f"依赖不存在或不可读: {dep_id}"
        if dep.get("status") != "done":
            return False, f"依赖未完成: {dep_id}={dep.get('status')}"
    return True, None


def _lifecycle_ready(board_root: Path, task_id: str, status: dict) -> tuple[bool, str | None]:
    if status.get("task_type") == "decision" and not status.get("decision_activated_at"):
        return False, "decision 尚未由用户 activate"
    replaced = status.get("supersedes")
    if replaced:
        parent_path = board_root / "tasks" / replaced / "status.json"
        try:
            parent = json.loads(parent_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, f"supersedes parent 不可读: {replaced}"
        if parent.get("status") != "superseded" or parent.get("superseded_by") != task_id:
            return False, f"旧任务 {replaced} 尚未完成正式 supersede transition"
    return True, None


def _warn_unreviewed_spec(board_root: Path, task_id: str, status: dict, card_text: str):
    """软警告：同 project 下有未验收的设计/规格任务且本任务是实施类。不阻止认领。"""
    impl_keywords = ["实施", "implementation", "implement", "实现"]
    spec_keywords = ["示意图", "规格", "spec", "design", "设计", "mockup"]
    lower_card = card_text.lower()
    if not any(kw in lower_card for kw in impl_keywords):
        return
    project = status.get("project", "")
    if not project:
        return
    tasks_root = board_root / "tasks"
    for task_dir in sorted(tasks_root.iterdir()):
        if task_dir.name == task_id or task_dir.name.startswith((".", "_")):
            continue
        if not is_physical_task_dir(task_dir, tasks_root):
            continue
        dep_status_path = task_dir / "status.json"
        dep_card_path = task_dir / "card.md"
        if not dep_status_path.exists() or not dep_card_path.exists():
            continue
        try:
            dep_status = json.loads(dep_status_path.read_text(encoding="utf-8"))
            dep_card = dep_card_path.read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            continue
        if dep_status.get("project") != project:
            continue
        if dep_status.get("status") != "awaiting_review":
            continue
        dep_lower = dep_card.lower()
        if any(kw in dep_lower for kw in spec_keywords):
            print(
                f"⚠️  警告: 同项目下有未验收的设计/规格任务 {task_dir.name}，"
                f"建议先完成验收再实施",
                file=sys.stderr,
            )


def claim(args) -> int:
    if not validate_task_id(args.task):
        print(f"❌ 非法任务 ID: {args.task!r}", file=sys.stderr)
        return 2
    agent = canonical_agent(args.agent)
    if not agent:
        print(f"❌ 未注册端名: {args.agent!r}", file=sys.stderr)
        return 2
    if not validate_model_label(args.model):
        print(f"❌ 模型名必须是真实档名，不能使用 {args.model!r}", file=sys.stderr)
        return 2

    board_root = Path(args.board_root).resolve()
    tasks_root = board_root / "tasks"
    task_dir = tasks_root / args.task
    status_path = task_dir / "status.json"
    card_path = task_dir / "card.md"
    if (
        not is_physical_task_dir(task_dir, tasks_root)
        or not status_path.exists()
        or not card_path.exists()
    ):
        print(f"❌ 任务不存在、不是安全真实目录或缺 card/status: {args.task}", file=sys.stderr)
        return 2

    claimed_at = now_cn()
    intent_path = board_root / "intents" / f"{agent}-{args.task}.md"
    intent = (
        "# 认领意图\n\n"
        f"- 端名: {agent}\n"
        f"- 模型: {args.model}\n"
        f"- 任务ID: {args.task}\n"
        f"- 意图时间: {claimed_at.isoformat(timespec='seconds')}\n"
        f"- 端能力: {', '.join(AGENT_CONTRACTS[agent]['caps'])}\n"
    )
    atomic_write_text(intent_path, intent)

    with task_lock(board_root, args.task):
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"❌ status.json 不可读: {exc}", file=sys.stderr)
            return 2

        if status.get("id", args.task) != args.task:
            print("❌ status.id 与任务目录不一致，拒绝认领", file=sys.stderr)
            return 2
        if status.get("status") != "open":
            current = status.get("status")
            if current == "awaiting_user_approval":
                reason = status.get("approval_reason", "未知原因")
                print(
                    f"🔒 认领失败：{args.task} 待用户审批（{reason}），"
                    f"不可认领",
                    file=sys.stderr,
                )
                return 3
            print(
                f"⏭️ 认领失败：{args.task} 当前状态={current}，"
                f"executor={status.get('executor')}",
                file=sys.stderr,
            )
            return 3

        integrity = get_task_integrity(task_dir, status)
        if not integrity.schedulable:
            print(
                f"❌ integrity gate 拒绝认领: {integrity.status}: {integrity.detail}",
                file=sys.stderr,
            )
            return 2

        stored_hash = status.get("card_hash")
        actual_hash = card_hash(card_path)
        if not stored_hash or stored_hash != actual_hash:
            print("❌ card_hash 缺失或不匹配，拒绝认领", file=sys.stderr)
            return 2

        required_caps = status.get("required_caps") or []
        if not isinstance(required_caps, list) or not required_caps:
            print("❌ required_caps 为空或格式错误", file=sys.stderr)
            return 2
        missing = [cap for cap in required_caps if cap not in AGENT_CONTRACTS[agent]["caps"]]
        if missing:
            print(f"❌ {agent} 缺少能力: {missing}", file=sys.stderr)
            return 2

        complexity = status.get("complexity", "L1")
        max_level = effective_execute_level(agent, args.model)
        if level_value(complexity) > level_value(max_level):
            print(
                f"❌ {agent}/{args.model} 最高执行 {max_level}，"
                f"不能认领 {complexity}",
                file=sys.stderr,
            )
            return 2

        deps_ok, dep_error = _dependencies_done(board_root, status.get("depends_on") or [])
        if not deps_ok:
            print(f"❌ {dep_error}", file=sys.stderr)
            return 4

        lifecycle_ok, lifecycle_error = _lifecycle_ready(board_root, args.task, status)
        if not lifecycle_ok:
            print(f"❌ lifecycle gate: {lifecycle_error}", file=sys.stderr)
            return 2

        card_text = card_path.read_text(encoding="utf-8")

        # 软警告：同 project 下有未验收的设计/规格任务且本任务是实施类
        _warn_unreviewed_spec(board_root, args.task, status, card_text)
        locked, preferred = _role_constraints(card_text)
        if locked and preferred != agent:
            print(f"❌ 角色锁定给 {preferred or '未知端'}，{agent} 不可认领", file=sys.stderr)
            return 2

        # v1.3 检查点4/5：家族状态检查 + 根目标展示 + 负约束展示
        family_ok, family_reason = _check_family(board_root, card_text)
        if not family_ok:
            print(f"❌ {family_reason}", file=sys.stderr)
            return 2

        if requires_independent_review(status):
            candidate_status = dict(status)
            candidate_status.update(
                {"executor": agent, "executor_model": args.model}
            )
            routes = eligible_reviewers(
                candidate_status,
                agent,
                executor_model=args.model,
                card_text=card_text,
                include_authorization=review_authorization_is_event_backed(
                    task_dir, status
                ),
            )
            if not routes:
                print(
                    f"❌ {NO_ELIGIBLE_INDEPENDENT_REVIEWER}: "
                    f"task={args.task} candidate_executor={agent}/{args.model}",
                    file=sys.stderr,
                )
                return 2

        lease_expires = claimed_at + timedelta(hours=args.lease_hours)
        updates = {
            # Legacy tasks keep their original contract. Claim is not a migration.
            "contract_version": int(status.get("contract_version") or 0),
            "status": "claimed",
            "executor": agent,
            "executor_model": args.model,
            "claimed_at": claimed_at.isoformat(timespec="seconds"),
            "lease_expires_at": lease_expires.isoformat(timespec="seconds"),
            "status_changed_at": claimed_at.isoformat(timespec="seconds"),
        }
        request_id = args.request_id or (
            f"claim:{args.task}:{agent}:{claimed_at.isoformat(timespec='microseconds')}"
        )
        try:
            commit_transition_locked(
                task_dir,
                action="claim",
                actor=agent,
                actor_model=args.model,
                request_id=request_id,
                updates=updates,
                request_payload={"task": args.task, "lease_hours": args.lease_hours},
                expected_status="open",
            )
        except TransitionError as exc:
            print(f"❌ 认领转换失败: {exc}", file=sys.stderr)
            return 2

    gen_script = board_root / "board-index-gen.py"
    if gen_script.exists():
        subprocess.run(
            [sys.executable, str(gen_script), "--board-root", str(board_root), "--mode", "apply"],
            check=False,
            capture_output=True,
            text=True,
        )
    print(
        f"✅ 已原子认领 {args.task}: executor={agent}/{args.model}, "
        f"lease={lease_expires.isoformat(timespec='seconds')}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="原子认领公告牌任务")
    parser.add_argument("--task", required=True, help="任务 ID")
    parser.add_argument("--agent", required=True, help="注册端名；显示名会归一为小写 key")
    parser.add_argument("--model", required=True, help="实际模型档名，禁止 Auto/unknown 等代指")
    parser.add_argument("--lease-hours", type=float, default=2.0, help="租约小时数，默认 2")
    parser.add_argument("--request-id", default="", help="可选幂等请求 ID")
    parser.add_argument("--board-root", default=str(Path(__file__).parent), help="board 根目录")
    args = parser.parse_args()
    if not 0 < args.lease_hours <= 24:
        parser.error("--lease-hours 必须在 (0, 24] 范围内")
    return claim(args)


if __name__ == "__main__":
    sys.exit(main())
