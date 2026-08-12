#!/usr/bin/env python3
"""board-task-create — 标准化任务创建脚本（v1.1：支持项目/轮次/依赖/优先级/治理标记）。

用法:
  python3 board-task-create.py \
    --id V11-03-review-template \
    --title "创建 REVIEW.md 模板" \
    --complexity L1 \
    --required-caps scheduled-task \
    --created-by Trae \
    --body "任务描述" \
    --project board-v1.1 \
    --iteration iter-1 \
    --priority high

退出码: 0=成功; 1=错误

v1.1 新增:
  - --project: 项目标识（如 board-v1.1）
  - --iteration: 轮次标识（如 iter-1）
  - --depends-on: 依赖任务ID，逗号分隔
  - --priority: urgent/high/normal/low（默认 normal）
  - --governance: 强制标记为治理文件修改类任务
  - 自动检测 card body 中的治理文件路径并标记
  - 依赖循环检测
  - --type: 任务类型（decision=决策类/tracking=跟踪类，默认空=执行类）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from board_contract import (
    AGENT_CONTRACTS,
    CONTRACT_VERSION,
    board_lock,
    canonical_agent,
    is_physical_task_dir,
    is_safe_token,
    validate_task_id,
)
from board_transition import write_genesis_ledger
from board_task_contract import build_qualified_contract, contract_fingerprint

CN_TZ = timezone(timedelta(hours=8))

# 治理文件路径模式
GOVERNANCE_PATTERNS = [
    "CLAUDE.md", "AGENTS.md", "DECISIONS.md", "AI_ENTRY.md",
    ".kb/schema/", ".kb/hooks/", ".kb/规约.md",
    "CAPABILITIES.yaml", "_FACTS.md",
]


def detect_governance(body: str) -> bool:
    """检测任务正文是否涉及治理文件。"""
    for pattern in GOVERNANCE_PATTERNS:
        if pattern in body:
            return True
    return False


def check_dependency_cycle(task_id: str, depends_on: list, board_root: Path, visited: set = None) -> bool:
    """检查依赖是否形成环。返回 True=有环。"""
    if visited is None:
        visited = set()
    if task_id in visited:
        return True
    visited.add(task_id)
    for dep_id in depends_on:
        dep_status_path = board_root / "tasks" / dep_id / "status.json"
        if dep_status_path.exists():
            try:
                dep_status = json.loads(dep_status_path.read_text(encoding="utf-8"))
                dep_deps = dep_status.get("depends_on", [])
                if dep_deps and check_dependency_cycle(dep_id, dep_deps, board_root, visited.copy()):
                    return True
            except (json.JSONDecodeError, OSError):
                pass
    return False


# ===== 任务家族与注意力机制（v1.3 新增）=====

DIRECTION_CHANGE_SIGNALS = ["替代", "替换", "换方向", "重新定义", "冻结"]
UNCERTAINTY_PATTERNS = [
    r"根因是.{1,20}而非",
    r"本质上应该",
    r"真正的方向是",
    r"应该用.{1,20}替代",
]


def detect_direction_change(body: str) -> bool:
    """检查点3：检测方向变更信号词。"""
    return any(s in body for s in DIRECTION_CHANGE_SIGNALS)


def detect_uncertainty_judgment(body: str) -> bool:
    """检查点2：检测 AI 方向性判断（不确定性门控）。
    使用 re.DOTALL 使 . 跨行匹配，防止 body 换行后漏检。
    """
    return any(re.search(p, body, re.DOTALL) for p in UNCERTAINTY_PATTERNS)


def check_root_goal_confirmed(family_id: str, board_root: Path) -> bool:
    """检查根目标是否经用户确认（Issue-3 修复）。
    读取根目标 card.md 的 confirmed_by 字段。
    """
    goal_card = board_root / "tasks" / family_id / "card.md"
    if not goal_card.exists():
        return False
    text = goal_card.read_text(encoding="utf-8")
    return "confirmed_by: user" in text


def check_family_frozen(family_id: str, board_root: Path) -> bool:
    """检查家族是否处于冻结状态。"""
    status_path = board_root / "families" / family_id / "status.json"
    if not status_path.exists():
        return False
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        return status.get("status") == "frozen"
    except (json.JSONDecodeError, OSError):
        return False


TERMINAL_WORK_STATUSES = {"done", "cancelled", "superseded", "archived"}


def _read_existing_status(tasks_root: Path, task_id: str) -> dict | None:
    task_dir = tasks_root / task_id
    if not is_physical_task_dir(task_dir, tasks_root):
        return None
    try:
        value = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _identity_records(tasks_root: Path, work_key: str) -> list[tuple[str, dict]]:
    records = []
    for task_dir in sorted(tasks_root.iterdir()):
        if not is_physical_task_dir(task_dir, tasks_root):
            continue
        status = _read_existing_status(tasks_root, task_dir.name)
        if status and status.get("work_key") == work_key:
            records.append((task_dir.name, status))
    return records


def _resolve_lineage(tasks_root: Path, args) -> tuple[str | None, tuple[str, dict] | None, str | None]:
    """Resolve root identity and optional parent while holding work-identity lock."""
    relation_ids = [value for value in (args.continuation_of, args.follow_up_of, args.supersedes) if value]
    if len(relation_ids) > 1:
        return None, None, "continuation/follow-up/supersedes 只能选择一种关系"
    parent = None
    relation_id = relation_ids[0] if relation_ids else None
    if relation_id:
        parent_status = _read_existing_status(tasks_root, relation_id)
        if parent_status is None:
            return None, None, f"lineage parent 不存在或不可读: {relation_id}"
        parent = (relation_id, parent_status)

    if args.continuation_of and parent:
        parent_status = parent[1]
        if parent_status.get("status") in TERMINAL_WORK_STATUSES:
            return None, None, "continuation_of 必须指向 active task"
        if parent_status.get("work_key") != args.work_key:
            return None, None, "continuation 必须复用 parent 的 work_key"
    if args.follow_up_of and parent:
        parent_status = parent[1]
        if parent_status.get("status") not in TERMINAL_WORK_STATUSES:
            return None, None, "follow_up_of 必须指向 terminal task"
        if parent_status.get("work_key") == args.work_key:
            return None, None, "follow-up 是独立交付物，必须使用新的 work_key"
    if args.supersedes and parent and parent[1].get("work_key") == args.work_key:
        return None, None, "superseding work 必须使用新的 work_key"

    inherited_root = parent[1].get("root_work_id") if parent else None
    root_work_id = args.root_work_id or inherited_root or args.id
    if parent and inherited_root and args.root_work_id and args.root_work_id != inherited_root:
        return None, None, "root_work_id 与 lineage parent 不一致"
    return root_work_id, parent, None


def _existing_identity_result(tasks_root: Path, args) -> tuple[int | None, str | None, str | None]:
    """Return (exit_code, existing_task_id, error) under the identity lock."""
    existing_same_id = _read_existing_status(tasks_root, args.id)
    if existing_same_id is not None:
        if args.work_key and existing_same_id.get("work_key") == args.work_key:
            if (
                getattr(args, "contract_fingerprint", "")
                and existing_same_id.get("contract_fingerprint") != args.contract_fingerprint
            ):
                return 1, None, "同 task_id/work_key 的 qualified contract 不一致"
            return 0, args.id, None
        return 1, None, f"任务已存在且 work identity 不匹配: {args.id}"
    if not args.work_key:
        return None, None, None  # v2 compatibility; v3 makes this mandatory

    records = _identity_records(tasks_root, args.work_key)
    active = [(task_id, st) for task_id, st in records if st.get("status") not in TERMINAL_WORK_STATUSES]
    if args.continuation_of:
        if active and active[0][0] == args.continuation_of:
            return 0, args.continuation_of, None
        return 1, None, "continuation target is not the active work instance"
    if active:
        return 3, active[0][0], f"active work_key 已存在: {args.work_key} -> {active[0][0]}"
    if records and not args.follow_up_of:
        return 1, records[-1][0], "work_key 已完成；新需求必须使用新 work_key + --follow-up-of"
    return None, None, None


def main():
    parser = argparse.ArgumentParser(description="标准化任务创建（v1.1）")
    parser.add_argument("--id", required=True, help="任务 ID（如 V11-03-review-template）")
    parser.add_argument("--title", required=True, help="任务标题")
    parser.add_argument("--complexity", default="L1", choices=["L1", "L2", "L3", "L4"], help="复杂度")
    parser.add_argument("--required-caps", required=True, help="需求能力，逗号分隔")
    parser.add_argument("--created-by", required=True, help="创建端名")
    parser.add_argument("--body", required=True, help="任务描述（卡正文）")
    parser.add_argument("--note", default="", help="备注（可选）")
    # v1.1 新增参数
    parser.add_argument("--project", default="", help="项目标识（如 board-v1.1）")
    parser.add_argument("--iteration", default="", help="轮次标识（如 iter-1）")
    parser.add_argument("--depends-on", default="", help="依赖任务ID，逗号分隔")
    parser.add_argument("--priority", default="normal", choices=["urgent", "high", "normal", "low"], help="优先级")
    parser.add_argument("--governance", action="store_true", help="强制标记为治理文件修改类任务")
    parser.add_argument("--type", default="", choices=["", "decision", "tracking"], help="任务类型：decision=决策类（等用户拍板后执行），tracking=跟踪类（长期项目，不认领不验收）")
    # v1.3 任务家族与注意力机制参数
    parser.add_argument("--family", default="", help="家族归属（根目标 ID，如 G-workbench-upgrade）")
    parser.add_argument("--goal-alignment", default="", help="目标对齐声明（--family 存在时必填）")
    parser.add_argument("--negative-constraints", default="", help="负约束（该角色不该做什么）")
    # v1.4 CP9 简化：交付模式声明
    parser.add_argument("--delivery", default="",
                        choices=["", "declare_done", "return_result", "pass_to_next"],
                        help="交付模式：declare_done=自包含完成 / return_result=结果回传创建方审阅 / pass_to_next=产出传给下一端。跨端委托（created_by 不在 required_caps 中）必填")
    # v1.0 收敛改造：项目交付里程碑
    parser.add_argument("--delivery-milestone", default="", help="项目交付里程碑（验收标准，如'bash .kb/workbench-regress.sh FAIL=0'）")
    parser.add_argument("--work-key", default="", help="稳定真实工作身份；v3 起必填")
    parser.add_argument("--root-work-id", default="", help="工作家族根 task ID；默认当前 task 或继承 parent")
    parser.add_argument("--continuation-of", default="", help="继续同一 active work；返回原 task，不新建")
    parser.add_argument("--follow-up-of", default="", help="已完成工作的独立后续交付")
    parser.add_argument("--supersedes", default="", help="新工作取代的旧 task ID")
    parser.add_argument("--acceptance", action="append", default=[], help="结构化验收标准；可重复")
    parser.add_argument("--output", dest="contract_outputs", action="append", default=[], help="结构化输出合同；可重复")
    parser.add_argument("--evidence", action="append", default=[], help="结构化验证/证据合同；可重复")
    parser.add_argument("--board-root", default=str(Path(__file__).parent), help="board 根目录")
    args = parser.parse_args()

    board_root = Path(args.board_root).resolve()

    # 收敛改造：元任务预算检查（禁止创建过多的元任务）
    meta_keywords = ["board", "validator", "orchestration", "wake", "governance", "review-framework", "system", "optimization", "audit", "重构", "优化", "系统", "架构", "机制"]
    is_meta = any(kw in args.id.lower() for kw in meta_keywords)
    if is_meta:
        tracker_script = board_root / "project-delivery-tracker.py"
        if tracker_script.exists():
            result = subprocess.run(
                [sys.executable, str(tracker_script), "--board-root", str(board_root), "--check-meta-create", args.id],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"❌ {result.stdout or result.stderr}", file=sys.stderr)
                return 1
    tasks_root = board_root / "tasks"
    tasks_root.mkdir(parents=True, exist_ok=True)

    if not validate_task_id(args.id):
        print(f"❌ 非法任务 ID: {args.id!r}（只允许单段字母数字/._-，且禁止 '..'）", file=sys.stderr)
        return 1
    task_dir = tasks_root / args.id

    if args.work_key and not is_safe_token(args.work_key):
        print(f"❌ work_key 非法: {args.work_key!r}", file=sys.stderr)
        return 1
    for field_name, value in (
        ("root_work_id", args.root_work_id),
        ("continuation_of", args.continuation_of),
        ("follow_up_of", args.follow_up_of),
        ("supersedes", args.supersedes),
    ):
        if value and not validate_task_id(value):
            print(f"❌ {field_name} 非法: {value!r}", file=sys.stderr)
            return 1
    try:
        qualified_contract = build_qualified_contract(
            task_id=args.id,
            work_key=args.work_key,
            acceptance=args.acceptance,
            outputs=args.contract_outputs,
            evidence=args.evidence,
        )
    except ValueError as exc:
        print(f"❌ qualified contract invalid: {exc}", file=sys.stderr)
        return 1
    args.contract_fingerprint = contract_fingerprint(qualified_contract)

    # 解析 required_caps（v1.4.2: 端名逐项 canonical 归一化，能力名保留原样）
    required_caps = []
    for cap_raw in args.required_caps.split(","):
        cap = cap_raw.strip()
        if not cap:
            continue
        canonical = canonical_agent(cap) or cap
        if canonical not in required_caps:
            required_caps.append(canonical)
    if not required_caps or any(not is_safe_token(cap) for cap in required_caps):
        print("❌ required_caps 不能为空，且每项只允许字母数字/._-", file=sys.stderr)
        return 1

    # v1.5 caps 校验：每项 cap 必须匹配至少一个注册端的能力列表
    all_known_caps = set()
    for profile in AGENT_CONTRACTS.values():
        all_known_caps.update(profile.get("caps", []))
    unknown_caps = [c for c in required_caps if c not in all_known_caps]
    if unknown_caps:
        registered = {cap: [name for name, p in AGENT_CONTRACTS.items() if cap in p.get("caps", [])]
                      for cap in sorted(all_known_caps)}
        print(
            f"❌ required_caps 含未注册能力: {unknown_caps}\n"
            f"   已注册能力及对应端：",
            file=sys.stderr,
        )
        for cap, agents in sorted(registered.items()):
            print(f"     {cap} → {', '.join(agents)}", file=sys.stderr)
        return 1

    created_by = canonical_agent(args.created_by)
    if not created_by:
        print(f"❌ created_by 不是公告牌已注册端: {args.created_by!r}", file=sys.stderr)
        return 1
    if args.created_by != created_by:
        print(f"ℹ️ created_by 已归一化: {args.created_by!r} → {created_by!r}")

    for field_name, field_value in (("project", args.project), ("iteration", args.iteration)):
        if field_value and not is_safe_token(field_value):
            print(f"❌ {field_name} 非法: {field_value!r}", file=sys.stderr)
            return 1

    # 解析 depends_on
    depends_on = [d.strip() for d in args.depends_on.split(",") if d.strip()] if args.depends_on else []
    if any(not validate_task_id(dep_id) for dep_id in depends_on):
        print("❌ depends_on 含非法任务 ID", file=sys.stderr)
        return 1
    if args.id in depends_on:
        print(f"❌ 任务不能依赖自身: {args.id}", file=sys.stderr)
        return 1
    missing_deps = [
        dep_id for dep_id in depends_on
        if not is_physical_task_dir(tasks_root / dep_id, tasks_root)
        or not (tasks_root / dep_id / "status.json").exists()
    ]
    if missing_deps:
        print(f"❌ 依赖任务不存在: {missing_deps}", file=sys.stderr)
        return 1

    # 依赖循环检测
    if depends_on and check_dependency_cycle(args.id, depends_on, board_root):
        print(f"❌ 依赖形成环，拒绝创建: {args.id}", file=sys.stderr)
        return 1

    # 治理文件检测
    requires_governance_approval = args.governance or detect_governance(args.body)

    # v1.4 CP9: 跨端委托必须显式声明交付模式（防止 AI 偷懒跳过回调）
    is_cross_agent = created_by not in required_caps
    delivery = args.delivery or ("declare_done" if not is_cross_agent else "")
    if is_cross_agent and not delivery:
        print(
            f"❌ 跨端委托（created_by={created_by} 不在 required_caps={required_caps}）必须显式声明 --delivery：\n"
            f"   return_result=结果回传创建方审阅 / pass_to_next=产出传给下一端",
            file=sys.stderr,
        )
        return 1
    # v1.4.1 P1 修复（对抗验收 T54）：跨端委托禁止 declare_done（设计文档：仅同端任务可用）
    if is_cross_agent and delivery == "declare_done":
        print(
            f"❌ 跨端委托（created_by={created_by} 不在 required_caps={required_caps}）不允许 --delivery declare_done：\n"
            f"   declare_done 仅同端任务可用；跨端委托必须声明 return_result 或 pass_to_next（防止静默跳过回调）",
            file=sys.stderr,
        )
        return 1

    # v1.3 任务家族与注意力机制
    # Issue-5: --family 存在时 --goal-alignment 必填
    if args.family and not args.goal_alignment:
        print("❌ --family 存在时 --goal-alignment 必填（检查点1：目标对齐声明）", file=sys.stderr)
        return 1

    # 检查点2/3：不确定性门控 + 方向变更拦截
    is_decision = args.type == "decision"
    uncertainty_hit = detect_uncertainty_judgment(args.body) if not is_decision else False
    direction_hit = detect_direction_change(args.body) if not is_decision else False
    needs_user_approval = uncertainty_hit or direction_hit or is_decision

    # 检查家族冻结状态
    family_frozen = False
    root_goal_confirmed = False
    if args.family:
        if check_family_frozen(args.family, board_root):
            print(f"❌ 家族 {args.family} 已冻结（被拒回溯中），不可创建新任务", file=sys.stderr)
            return 1
        root_goal_confirmed = check_root_goal_confirmed(args.family, board_root)
        if not root_goal_confirmed:
            print(f"⚠️ 根目标 {args.family} 未确认或不存在，任务可创建但 root_goal_confirmed=false", file=sys.stderr)

    approval_reason = ""
    if is_decision:
        approval_reason = "decision_pending"
    elif uncertainty_hit and direction_hit:
        approval_reason = "CP2+CP3: 不确定性判断+方向变更信号"
    elif uncertainty_hit:
        approval_reason = "CP2: 不确定性判断"
    elif direction_hit:
        approval_reason = "CP3: 方向变更信号"

    # 创建 status.json
    created_dt = datetime.now(CN_TZ)
    now = created_dt.strftime("%Y-%m-%d")
    # v1.3: CP2/CP3 命中时 status 自动设为 awaiting_user_approval
    initial_status = "awaiting_user_approval" if needs_user_approval else "open"
    status = {
        "contract_version": CONTRACT_VERSION,
        "id": args.id,
        "status": initial_status,
        "complexity": args.complexity,
        "required_caps": required_caps,
        "executor": None,
        "executor_model": None,
        "created_by": created_by,
        "created": now,
        "created_at": created_dt.isoformat(timespec="seconds"),
        "status_changed_at": created_dt.isoformat(timespec="seconds"),
        "claimed_at": None,
        "lease_expires_at": None,
        "completed_at": None,
        "requeue_count": 0,
        "contract_fingerprint": args.contract_fingerprint,
    }

    # v1.3: CP2/CP3 触发原因
    if needs_user_approval:
        status["approval_reason"] = approval_reason

    # v1.1 新增字段（只在有值时写入，保持向后兼容）
    if args.project:
        status["project"] = args.project
        # 收敛改造：delivery_milestone 用于项目级交付判定
        if args.delivery_milestone:
            status["delivery_milestone"] = args.delivery_milestone
    if args.iteration:
        status["iteration"] = args.iteration
    if depends_on:
        status["depends_on"] = depends_on
    if args.priority != "normal":
        status["priority"] = args.priority
    if requires_governance_approval:
        status["requires_governance_approval"] = True
    if args.type:
        status["task_type"] = args.type

    # v1.1 验收字段（初始为空）
    status["review_result"] = None
    status["review_round"] = 0
    status["review_issues_count"] = None

    if args.note:
        status["note"] = args.note

    # v1.3 任务家族字段（只在有值时写入，保持向后兼容）
    if args.family:
        status["family"] = args.family
    if args.goal_alignment:
        status["goal_alignment"] = args.goal_alignment
    if args.negative_constraints:
        status["negative_constraints"] = args.negative_constraints
    if args.family:
        status["root_goal_confirmed"] = root_goal_confirmed
    if direction_hit:
        status["direction_change"] = True
    # v1.4 CP9: 交付模式（非 declare_done 时写入，保持向后兼容）
    if delivery != "declare_done":
        status["delivery"] = delivery
    # vNext stable work identity（v2 兼容期可为空；v3 起由 qualified create 强制）
    if args.work_key:
        status["work_key"] = args.work_key
    if args.continuation_of:
        status["continuation_of"] = args.continuation_of
    if args.follow_up_of:
        status["follow_up_of"] = args.follow_up_of
    if args.supersedes:
        status["supersedes"] = args.supersedes

    # v1.2: 先写 card.md，再计算 hash 写入 status.json

    # 创建 card.md
    caps_yaml = ", ".join(required_caps)
    frontmatter_lines = [
        "---",
        f"id: {args.id}",
        f"required_caps: [{caps_yaml}]",
        f"complexity: {args.complexity}",
        f"created_by: {created_by}",
        f"created: {now}",
        f"contract_version: {CONTRACT_VERSION}",
        f"contract_fingerprint: {args.contract_fingerprint}",
    ]
    if args.project:
        frontmatter_lines.append(f"project: {args.project}")
        if args.delivery_milestone:
            frontmatter_lines.append(f'delivery_milestone: "{args.delivery_milestone}"')
    if args.iteration:
        frontmatter_lines.append(f"iteration: {args.iteration}")
    if args.priority != "normal":
        frontmatter_lines.append(f"priority: {args.priority}")
    if depends_on:
        frontmatter_lines.append(f"depends_on: [{', '.join(depends_on)}]")
    if requires_governance_approval:
        frontmatter_lines.append("requires_governance_approval: true")
    if args.type:
        frontmatter_lines.append(f"task_type: {args.type}")
    # v1.3 任务家族字段
    if args.family:
        frontmatter_lines.append(f"family: {args.family}")
    if args.goal_alignment:
        frontmatter_lines.append(f'goal_alignment: "{args.goal_alignment}"')
    if args.negative_constraints:
        frontmatter_lines.append(f'negative_constraints: "{args.negative_constraints}"')
    if args.family:
        frontmatter_lines.append(f"root_goal_confirmed: {str(root_goal_confirmed).lower()}")
    if direction_hit:
        frontmatter_lines.append("direction_change: true")
    # v1.4 CP9: 交付模式
    if delivery != "declare_done":
        frontmatter_lines.append(f"delivery: {delivery}")
    if args.work_key:
        frontmatter_lines.append(f"work_key: {args.work_key}")
    if args.continuation_of:
        frontmatter_lines.append(f"continuation_of: {args.continuation_of}")
    if args.follow_up_of:
        frontmatter_lines.append(f"follow_up_of: {args.follow_up_of}")
    if args.supersedes:
        frontmatter_lines.append(f"supersedes: {args.supersedes}")
    frontmatter_lines.append("---")
    frontmatter = "\n".join(frontmatter_lines)

    card = f"""{frontmatter}

# {args.title}

{args.body}
"""
    # 全局 work identity 锁覆盖“查重→lineage 校验→原子 rename”，防止不同 task_id 竞态。
    with board_lock(board_root, "work-identity"):
        identity_code, existing_id, identity_error = _existing_identity_result(tasks_root, args)
        if identity_code is not None:
            if identity_code == 0:
                print(f"✅ EXISTING_WORK: {existing_id} (work_key={args.work_key})")
            else:
                print(f"❌ {identity_error}", file=sys.stderr)
                if existing_id:
                    print(f"   existing_task={existing_id}", file=sys.stderr)
            return identity_code

        root_work_id, _, lineage_error = _resolve_lineage(tasks_root, args)
        if lineage_error:
            print(f"❌ {lineage_error}", file=sys.stderr)
            return 1
        if args.work_key:
            status["root_work_id"] = root_work_id
            insert_at = frontmatter_lines.index("---", 1)
            frontmatter_lines.insert(insert_at, f"root_work_id: {root_work_id}")
            frontmatter = "\n".join(frontmatter_lines)
            card = f"""{frontmatter}

# {args.title}

{args.body}
"""

        # 在 tasks/ 下的隐藏临时目录完整构建，再原子 rename；崩溃不会留下半任务。
        with tempfile.TemporaryDirectory(prefix=".creating-", dir=tasks_root) as tmp_name:
            staging_dir = Path(tmp_name)
            card_path = staging_dir / "card.md"
            card_path.write_text(card, encoding="utf-8")

            card_digest = hashlib.sha256(card_path.read_bytes()).hexdigest()[:16]
            status["card_hash"] = card_digest
            status_path = staging_dir / "status.json"
            status_path.write_text(
                json.dumps(status, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (staging_dir / "contract.json").write_text(
                json.dumps(qualified_contract, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            write_genesis_ledger(staging_dir, status, actor=created_by)
            (staging_dir / "output").mkdir()
            if os.environ.get("BOARD_CREATE_FAILPOINT") == "before_commit":
                print("❌ fault injection: before create commit", file=sys.stderr)
                return 2
            staging_dir.rename(task_dir)

    print(f"✅ 任务已创建: {args.id}")
    print(f"   目录: {task_dir}")
    print(f"   复杂度: {args.complexity}")
    print(f"   需求能力: {required_caps}")
    if args.project:
        print(f"   项目: {args.project}")
    if args.iteration:
        print(f"   轮次: {args.iteration}")
    if depends_on:
        print(f"   依赖: {depends_on}")
    if args.priority != "normal":
        print(f"   优先级: {args.priority}")
    if requires_governance_approval:
        print(f"   ⚠️ 治理文件标记: 验收通过后需 awaiting_user_approval")
    if args.type:
        print(f"   任务类型: {args.type}")
    # v1.3 家族信息输出
    if args.family:
        print(f"   家族: {args.family}")
    if args.goal_alignment:
        print(f"   目标对齐: {args.goal_alignment}")
    if args.negative_constraints:
        print(f"   🚫 负约束: {args.negative_constraints}")
    if needs_user_approval:
        print(f"   🔒 待用户审批: {approval_reason}")
    if args.family:
        print(f"   根目标确认: {root_goal_confirmed}")
    if args.work_key:
        print(f"   工作身份: {args.work_key} (root={status.get('root_work_id')})")

    # 刷新索引
    gen_script = board_root / "board-index-gen.py"
    if gen_script.exists():
        result = subprocess.run(
            [sys.executable, str(gen_script), "--board-root", str(board_root), "--mode", "apply"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"   索引已刷新")
        else:
            print(f"   ⚠️ 索引刷新失败: {result.stderr}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
