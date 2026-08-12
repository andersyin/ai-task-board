#!/usr/bin/env python3
"""公告牌统一状态转换 CLI：含实施激活、执行、验收与治理终批。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from board_integrity import get_task_integrity
from board_task_contract import (
    agent_heartbeat_freshness,
    load_and_validate_contract,
    review_binds_contract,
)
from board_verdicts import REVIEW_RESULTS
from board_contract import (
    AGENT_CONTRACTS,
    NO_ELIGIBLE_INDEPENDENT_REVIEWER,
    REVIEW_AUTHORIZATION_SCHEMA,
    REVIEW_AUTHORIZATION_SCOPE,
    canonical_agent,
    eligible_reviewers,
    level_value,
    model_family,
    requires_independent_review,
    review_authorization_allows,
    validate_model_label,
)
from board_transition import (
    SYSTEM_REQUEUE_ACTOR,
    TransitionError,
    canonical_registered_actor,
    committed_request,
    digest,
    now_cn,
    recover_task,
    review_authorization_is_event_backed,
    signed_blocked_exists,
    signed_progress_exists,
    signed_review_matches,
    transition_task,
    validate_reviewer,
)


# 收敛改造：取消 conditional_pass，验收只保留 PASS / FAIL
# 存在影响验收标准的问题就是 FAIL，非阻塞改进写入 REVIEW.md 的 recommendations 段
RESULTS = set(REVIEW_RESULTS)

CALLBACKS_FILE = "callbacks.jsonl"


def _log_callback(
    board_root: Path,
    task_id: str,
    status: dict,
    trigger: str,
    executor: str,
    executor_model: str,
) -> None:
    """CP9: 完成回调日志 — submit/block 时写 callbacks.jsonl。

    简化设计（v1.4/v1.4.2）：
    - 只写日志，不自动唤醒（唤醒靠巡查端轮询或手动 board-wake.py）
    - 触发条件：实际执行方 != 创建方（created_by != executor）即写回调
      —— 跨端委托必写；多端 required_caps 下实际由他端执行也写（P2-1 修复，
      不再依赖 delivery 声明，因为多端 caps 任务创建时可能默认 declare_done）
    - 同端自创建自执行（created_by == executor）跳过
    - 失败不阻断 transition
    """
    created_by = status.get("created_by")
    if not created_by or created_by == executor:
        return  # 自己创建的任务，无需回调

    delivery = status.get("delivery", "declare_done")
    import time
    from board_transition import now_cn

    callback = {
        "callback_id": f"cb-{task_id}-{trigger}-{int(time.time())}",
        "task_id": task_id,
        "created_by": created_by,
        "executor": executor,
        "executor_model": executor_model,
        "trigger": trigger,  # submit / block
        "delivery": delivery,
        "triggered_at": now_cn().isoformat(timespec="seconds"),
        "task_status": status.get("status"),
        "acknowledged": False,
    }

    try:
        cb_path = board_root / CALLBACKS_FILE
        with open(cb_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(callback, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 不阻断 transition

    # v1.5: pass_to_next / return_result 时写通知到创建方的 inbox，方便在线端轮询发现
    if delivery in ("pass_to_next", "return_result") and trigger == "submit":
        try:
            inbox = board_root / "experimental" / f"{created_by}-inbox.md"
            inbox.parent.mkdir(parents=True, exist_ok=True)
            line = (
                f"- [{now_cn().isoformat(timespec='seconds')}] "
                f"任务 {task_id} 已由 {executor}/{executor_model} submit "
                f"(delivery={delivery}) → 请认领后续任务或验收产出\n"
            )
            with open(inbox, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass  # best-effort


def _attempt_wake_notify(
    board_root: Path,
    task_id: str,
    callback_id: str,
    target_agent: str,
    review_result: str,
    reviewer: str,
    reviewer_model: str,
) -> None:
    """CP9+ : best-effort 唤醒通知目标端验收结果。失败不阻断。

    通过 board-wake.py --mode notify 唤醒目标端，告知验收完成。
    唤醒是 fire-and-forget，不等待结果。
    """
    import time as _time

    result_text = {
        "pass": "PASS（通过）",
        "fail": "FAIL（不通过，需修复后重新 submit）",
    }.get(review_result, review_result)

    prompt = (
        f"公告牌验收完成通知：\n"
        f"  任务: {task_id}\n"
        f"  验收方: {reviewer}/{reviewer_model}\n"
        f"  结果: {result_text}\n"
        f"  回调ID: {callback_id}\n\n"
        f"请执行以下步骤：\n"
        f"1. 确认收到此通知：python3 .kb/board/board-callback.py ack {callback_id} --agent {target_agent}\n"
        f"2. 获取下一动作：python3 .kb/board/board-next-action.py --agent {target_agent} --model <你的模型档名>\n"
        f"3. 按 next-action 返回的 action 执行\n"
    )

    prompt_dir = board_root / "_trigger-archive"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = prompt_dir / f"_wake-notify-{task_id}-{int(_time.time())}.md"
    try:
        prompt_file.write_text(prompt, encoding="utf-8")
    except OSError:
        return

    wake_script = board_root / "board-wake.py"
    if not wake_script.exists():
        return

    try:
        subprocess.run(
            [
                sys.executable,
                str(wake_script),
                "wake",
                "--channel",
                target_agent,
                "--task",
                task_id,
                "--prompt-file",
                str(prompt_file),
                "--mode",
                "notify",
                "--confirm-seconds",
                "30",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(board_root.parent),
        )
    except Exception:
        pass  # Best-effort, 不阻断 transition


def _log_review_callback(
    board_root: Path,
    task_id: str,
    status: dict,
    reviewer: str,
    reviewer_model: str,
    review_result: str,
) -> None:
    """CP9+: Review 回调 — review 完成后写 callbacks.jsonl 并 best-effort 唤醒相关方。

    通知对象：
    - review pass → 通知 created_by（创建方可能需要推进依赖任务）
    - review fail → 通知 executor（执行方需要修复后重新 submit）
    - 同端（notify_target == reviewer）跳过

    失败不阻断 transition。
    """
    import time
    from board_transition import now_cn as _now_cn

    created_by = status.get("created_by")
    executor = status.get("executor")

    # 决定通知谁
    if review_result == "fail":
        notify_target = executor if executor and executor != reviewer else None
    else:
        notify_target = created_by if created_by and created_by != reviewer else None

    if not notify_target:
        return  # 同端或无目标，无需回调

    callback_id = f"cb-{task_id}-review-{int(time.time())}"
    callback = {
        "callback_id": callback_id,
        "task_id": task_id,
        "created_by": created_by,
        "executor": executor,
        "reviewer": reviewer,
        "reviewer_model": reviewer_model,
        "trigger": "review",
        "review_result": review_result,
        "triggered_at": _now_cn().isoformat(timespec="seconds"),
        "task_status": status.get("status"),
        "acknowledged": False,
        "notify_target": notify_target,
    }

    try:
        cb_path = board_root / CALLBACKS_FILE
        with open(cb_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(callback, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 不阻断 transition

    # Best-effort 唤醒通知（传入 callback_id 以便目标端直接 ack）
    _attempt_wake_notify(
        board_root, task_id, callback_id, notify_target, review_result, reviewer, reviewer_model
    )


def load_status(board_root: Path, task_id: str) -> dict:
    try:
        return json.loads(
            (board_root / "tasks" / task_id / "status.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise TransitionError(f"status.json 不可读: {exc}") from exc


def refresh_index(board_root: Path) -> None:
    script = board_root / "board-index-gen.py"
    if script.exists():
        subprocess.run(
            [sys.executable, str(script), "--board-root", str(board_root), "--mode", "apply"],
            check=False,
            capture_output=True,
            text=True,
        )


def execute(args) -> int:
    board_root = Path(args.board_root).resolve()
    if args.action == "recover":
        event = recover_task(board_root, args.task)
        print("✅ 恢复完成" if event else "✅ 无 pending 需恢复")
        return 0

    # 常规请求也先前滚上一次崩溃断点，再基于新鲜 status 做权限门禁。
    recover_task(board_root, args.task)
    status = load_status(board_root, args.task)
    task_dir = board_root / "tasks" / args.task
    try:
        card_text = (task_dir / "card.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise TransitionError(f"card.md 不可读: {exc}") from exc
    current = status.get("status")
    when = now_cn().isoformat(timespec="seconds")
    updates: dict = {"status_changed_at": when}
    payload: dict = {"task": args.task, "reason": args.reason or ""}
    expected_fields: dict = {}

    submission_payload = None
    if args.action == "submit" and int(status.get("contract_version") or 0) >= 3:
        _, contract_error = load_and_validate_contract(task_dir, status)
        if contract_error:
            raise TransitionError(f"qualified contract invalid: {contract_error}")
        outputs = [value.strip() for value in args.output_ref if value.strip()]
        evidence = [value.strip() for value in args.evidence_ref if value.strip()]
        if not outputs:
            raise TransitionError("v3 submit 必须提供至少一个 --output-ref")
        if not evidence:
            raise TransitionError("v3 submit 必须提供至少一个 --evidence-ref")
        submission_payload = {
            "contract_version": status.get("contract_version"),
            "contract_fingerprint": status.get("contract_fingerprint"),
            "outputs": outputs,
            "evidence": evidence,
        }

    # 在状态门禁前先识别已提交的同一请求：重试返回成功，
    # 但复用 request_id 且参数不同会硬拒绝。
    user_actions = {
        "activate",
        "approve",
        "cancel",
        "archive",
        "supersede",
        "authorize-reviewer",
    }
    # v1.5: 创建方取消未认领任务不走 user 审批路径
    created_by_name = status.get("created_by", "")
    is_creator_cancel = (
        args.action == "cancel"
        and created_by_name
        and args.actor.strip().lower() == created_by_name
        and args.actor.strip().lower() != "user"
    )
    if is_creator_cancel:
        if not args.model:
            args.model = args.actor.strip().lower()
        idem_actor = canonical_registered_actor(args.actor, args.model)
    elif args.action in user_actions and not args.model:
        args.model = "human"
    if args.action in user_actions and not is_creator_cancel:
        idem_actor = args.actor.strip().lower()
        payload["approval_evidence"] = args.approval_evidence
        if args.action == "supersede":
            payload["replacement_task"] = args.replacement_task
    elif args.action == "requeue" and args.actor == SYSTEM_REQUEUE_ACTOR:
        idem_actor = SYSTEM_REQUEUE_ACTOR
    else:
        idem_actor = canonical_registered_actor(args.actor, args.model)
    if args.action == "submit":
        payload["target"] = "awaiting_review"
        if submission_payload:
            payload["submission"] = submission_payload
    elif args.action == "block":
        payload["target"] = "blocked"
    elif args.action == "review":
        payload.update({"result": args.result, "issues": args.issues})
        authorization = status.get("review_authorization")
        if isinstance(authorization, dict) and authorization.get("active") is True:
            payload["review_authorization_id"] = authorization.get("authorization_id")
    elif args.action == "authorize-reviewer":
        authorization_id = "review-auth-" + digest(
            {"task_id": args.task, "request_id": args.request_id}
        )[:16]
        payload["authorization"] = {
            "authorization_id": authorization_id,
            "task_id": args.task,
            "reviewer": canonical_agent(args.reviewer),
            "reviewer_model": args.reviewer_model,
            "reviewer_family": args.reviewer_family,
            "requested_max_level": args.requested_max_level,
            "reason": args.reason,
            "scope": args.scope,
            "one_time": args.one_time,
            "approval_evidence": args.approval_evidence,
        }
    elif args.action == "activate":
        payload["target"] = (
            "claimed"
            if str(status.get("approval_reason") or "").startswith("review_fail_threshold")
            and status.get("executor")
            else "open"
        )
    elif args.action == "cancel":
        payload["target"] = "cancelled"
    elif args.action == "archive":
        payload["target"] = "archived"
    elif args.action == "supersede":
        payload["target"] = "superseded"
    elif args.action == "requeue":
        payload["target"] = "dlq" if args.to_dlq else "open"
    existing = committed_request(
        task_dir,
        request_id=args.request_id,
        action=args.action,
        actor=idem_actor,
        actor_model=args.model,
        request_payload=payload,
    )
    if existing:
        print(
            f"✅ 幂等命中 {args.task}: {existing.get('from_status')} -> "
            f"{existing.get('to_status')} seq={existing.get('seq')} request_id={args.request_id}"
        )
        return 0

    if args.action in {"submit", "block"}:
        actor = canonical_registered_actor(args.actor, args.model)
        if actor != canonical_agent(status.get("executor")) or args.model != status.get("executor_model"):
            raise TransitionError("只有原 executor/模型可以 submit 或 block")
        if current != "claimed":
            raise TransitionError(f"{args.action} 只允许 claimed，当前={current}")
        if args.action == "submit":
            if not signed_progress_exists(task_dir, actor, args.model):
                raise TransitionError(
                    f"submit 前 PROGRESS.md 必须包含执行端/模型署名\n"
                    f"   期望格式: [端名/模型名]，如 [{actor}/{args.model}]\n"
                    f"   或写: 执行端：{actor}/{args.model}"
                )
            if requires_independent_review(status):
                routes = eligible_reviewers(
                    status,
                    actor,
                    executor_model=args.model,
                    card_text=card_text,
                    include_authorization=review_authorization_is_event_backed(
                        task_dir, status
                    ),
                )
                if not routes:
                    raise TransitionError(
                        f"{NO_ELIGIBLE_INDEPENDENT_REVIEWER}: submit 拒绝；"
                        f"task={args.task} executor={actor}/{args.model}"
                    )
                # P1-2（2026-08-12 治理）：验收通道 fail-fast —— routes 存在但全部
                # eligible reviewer 心跳过期时挂 review_risk 标记（不阻断 submit，
                # 端可能稍后恢复，P0-2 escalate 会兜底唤醒）。
                reviewers = [r.get("reviewer") for r in routes if r.get("reviewer")]
                stale_reviewers = [
                    rv for rv in reviewers
                    if (age := agent_heartbeat_freshness(board_root, rv)) is None
                    or age > 24.0
                ]
                if reviewers and len(stale_reviewers) == len(reviewers):
                    updates["review_risk"] = (
                        "stale_reviewers"
                    )
                    updates["review_risk_detail"] = (
                        f"eligible reviewers 全部心跳过期: {sorted(stale_reviewers)}; "
                        "P0-2 escalate 将自动唤醒, 或需用户 authorize-reviewer"
                    )
                    print(
                        f"⚠️ 验收通道风险: {args.task} 的 eligible reviewers "
                        f"{sorted(reviewers)} 心跳均过期（>24h），已标记 review_risk；"
                        "P0-2 自动升级处置将兜底", file=sys.stderr
                    )
            updates.update({"status": "awaiting_review", "lease_expires_at": None})
            if submission_payload:
                updates.update(
                    {
                        "submission_contract_version": submission_payload["contract_version"],
                        "submission_contract_fingerprint": submission_payload["contract_fingerprint"],
                        "submission_outputs": submission_payload["outputs"],
                        "submission_evidence": submission_payload["evidence"],
                        "submitted_at": when,
                    }
                )
        else:
            if not signed_blocked_exists(task_dir, actor, args.model):
                raise TransitionError(
                    f"block 前 BLOCKED.md 前 3 行必须包含执行端/模型署名\n"
                    f"   期望格式: 执行端：{actor}/{args.model}"
                )
            updates.update(
                {"status": "blocked", "blocked_at": when, "lease_expires_at": None}
            )
        payload["target"] = updates["status"]

    elif args.action == "review":
        actor = canonical_registered_actor(args.actor, args.model)
        if current != "awaiting_review":
            raise TransitionError(f"review 只允许 awaiting_review，当前={current}")
        if args.result not in RESULTS:
            raise TransitionError("review 必须提供 --result pass|fail（conditional_pass 已取消）")
        if args.issues is None or args.issues < 0:
            raise TransitionError("review 必须提供非负 --issues")
        review_path = task_dir / "REVIEW.md"
        if not review_path.exists() or review_path.stat().st_size < 32:
            raise TransitionError("review 前必须有非空 REVIEW.md")
        review_route = validate_reviewer(
            status,
            actor,
            args.model,
            task_dir=task_dir,
            card_text=card_text,
        )
        if not signed_review_matches(task_dir, actor, args.model, args.result):
            raise TransitionError(
                f"REVIEW.md 必须绑定本次验收端/模型并写明一致的验收结果\n"
                f"   期望格式:\n"
                f"     验收端: {actor}/{args.model}\n"
                f"     验收结果: {'PASS' if args.result == 'pass' else 'FAIL'}\n"
                f"   注意: 字段行不能有列表前缀(- )，须顶格写"
            )
        if int(status.get("contract_version") or 0) >= 3:
            _, contract_error = load_and_validate_contract(task_dir, status)
            if contract_error:
                raise TransitionError(f"qualified contract invalid: {contract_error}")
            fingerprint = status.get("contract_fingerprint")
            if (
                status.get("submission_contract_version") != status.get("contract_version")
                or status.get("submission_contract_fingerprint") != fingerprint
                or not status.get("submission_outputs")
                or not status.get("submission_evidence")
            ):
                raise TransitionError("review 拒绝：submission 未绑定冻结合同或缺 output/evidence")
            if not review_binds_contract(task_dir, fingerprint):
                raise TransitionError("REVIEW.md 必须声明与 submission 一致的冻结合同指纹")
        round_value = int(status.get("review_round") or 0) + 1
        updates.update(
            {
                "reviewer": actor,
                "reviewer_model": args.model,
                "review_result": args.result,
                "review_round": round_value,
                "review_issues_count": args.issues,
            }
        )
        authorization = status.get("review_authorization")
        if isinstance(authorization, dict) and authorization.get("active") is True:
            consumed = dict(authorization)
            consumed.update(
                {
                    "active": False,
                    "consumed_at": when,
                    "consumed_by": actor,
                    "consumed_by_model": args.model,
                    "consumed_via": review_route,
                }
            )
            updates["review_authorization"] = consumed
        if args.result == "fail":
            # 收敛改造：强制停止条件 — review fail ≥2 次时提交用户裁决
            if round_value >= 2:
                updates.update(
                    {
                        "status": "awaiting_user_approval",
                        "lease_expires_at": None,
                        "completed_at": None,
                        "approval_reason": f"review_fail_threshold: {round_value} 次连续验收失败",
                    }
                )
            else:
                updates.update(
                    {
                        "status": "claimed",
                        "lease_expires_at": (now_cn() + timedelta(hours=args.lease_hours)).isoformat(
                            timespec="seconds"
                        ),
                        "completed_at": None,
                    }
                )
        elif status.get("requires_governance_approval"):
            updates.update(
                {"status": "awaiting_user_approval", "lease_expires_at": None, "completed_at": None}
            )
        else:
            updates.update(
                {"status": "done", "lease_expires_at": None, "completed_at": when}
            )
        payload.update({"result": args.result, "issues": args.issues})

    elif args.action == "authorize-reviewer":
        actor = args.actor.strip().lower()
        reviewer = canonical_agent(args.reviewer)
        if actor != "user" or args.model != "human":
            raise TransitionError("authorize-reviewer 只能由 actor=user/model=human 执行")
        if current not in {"open", "claimed", "awaiting_review"}:
            raise TransitionError(
                "authorize-reviewer 只允许 open/claimed/awaiting_review"
            )
        if int(status.get("contract_version") or 0) < 2 or not (
            task_dir / "events.jsonl"
        ).exists():
            raise TransitionError("authorize-reviewer 只用于 event-managed v2+ 任务")
        integrity = get_task_integrity(task_dir, status)
        if not integrity.schedulable:
            raise TransitionError(
                f"authorize-reviewer integrity failure: {integrity.status}: {integrity.detail}"
            )
        if not requires_independent_review(status):
            raise TransitionError("该任务不需要独立技术验收")
        if not reviewer or not validate_model_label(args.reviewer_model):
            raise TransitionError("必须提供注册 reviewer 与真实 reviewer model")
        if args.reviewer_family not in AGENT_CONTRACTS[reviewer]["model_families"]:
            raise TransitionError("reviewer family 与注册端 capability 不匹配")
        if model_family(args.reviewer_model) != args.reviewer_family:
            raise TransitionError("reviewer model 与 reviewer family 不匹配")
        if args.scope != REVIEW_AUTHORIZATION_SCOPE or args.one_time is not True:
            raise TransitionError(
                "reviewer authorization 必须是 independent-technical-review one-shot"
            )
        if level_value(args.requested_max_level) not in {1, 2, 3, 4}:
            raise TransitionError("--requested-max-level 必须是 L1-L4")
        if not args.reason.strip() or not args.approval_evidence.strip():
            raise TransitionError("授权必须提供 reason 与 approval evidence")
        if canonical_agent(status.get("executor")) == reviewer:
            raise TransitionError("授权不得允许 executor self-review")
        if status.get("executor") and eligible_reviewers(
            status,
            status.get("executor"),
            executor_model=status.get("executor_model"),
            card_text=card_text,
            include_authorization=False,
        ):
            raise TransitionError("正常 independent reviewer pool 非空，不需要 escalation")
        existing_authorization = status.get("review_authorization")
        if (
            isinstance(existing_authorization, dict)
            and existing_authorization.get("active") is True
        ):
            raise TransitionError("任务已有未消费 reviewer authorization")
        authorization_request = payload["authorization"]
        authorization = {
            "schema": REVIEW_AUTHORIZATION_SCHEMA,
            **authorization_request,
            "approved_by": "user",
            "approved_at": when,
            "active": True,
            "consumed_at": None,
        }
        candidate = dict(status)
        candidate["review_authorization"] = authorization
        if not review_authorization_allows(
            candidate,
            reviewer,
            args.reviewer_model,
            card_text=card_text,
        ):
            raise TransitionError("reviewer authorization 不满足 task-scoped independence policy")
        updates.update({"status": current, "review_authorization": authorization})

    elif args.action == "activate":
        actor = args.actor.strip().lower()
        fail_threshold_retry = str(status.get("approval_reason") or "").startswith(
            "review_fail_threshold"
        )
        if actor != "user" or not args.approval_evidence:
            raise TransitionError("activate 只能 actor=user 且必须提供 --approval-evidence")
        if current != "awaiting_user_approval":
            raise TransitionError("activate 只允许 awaiting_user_approval 状态")
        if not status.get("approval_reason"):
            raise TransitionError("activate 只允许 CP2/CP3 实施前方向审批")
        if (status.get("reviewer") or status.get("review_result")) and not fail_threshold_retry:
            raise TransitionError("已有技术验收的治理终批必须使用 approve，不得 activate")
        if fail_threshold_retry and status.get("executor"):
            updates.update(
                {
                    "status": "claimed",
                    "lease_expires_at": (now_cn() + timedelta(hours=args.lease_hours)).isoformat(timespec="seconds"),
                    "reviewer": None,
                    "reviewer_model": None,
                    "review_result": None,
                    "review_round": 0,
                    "review_issues_count": None,
                }
            )
        else:
            updates.update({"status": "open", "lease_expires_at": None})
        updates.update(
            {
                "approved_by": "user",
                "approved_at": when,
                "approval_reason": None,
                "activation_evidence": args.approval_evidence,
            }
        )
        if status.get("task_type") == "decision":
            updates["decision_activated_at"] = when
        payload["approval_evidence"] = args.approval_evidence

    elif args.action == "cancel":
        actor = args.actor.strip().lower()
        created_by = status.get("created_by", "")
        is_creator = actor == created_by and created_by
        is_user = actor == "user"
        # v1.5: 创建方可取消自己的未认领（open）任务，不需要 user 审批
        if is_creator and not is_user and current == "open" and not status.get("executor"):
            pass  # 创建方取消未认领任务，允许
        elif not is_user or not args.approval_evidence:
            raise TransitionError(
                "cancel 只能由 actor=user（需 --approval-evidence）或 "
                "created_by 方取消自己的 open 任务（未认领）；"
                f"当前 actor={actor}, status={current}, created_by={created_by}"
            )
        if current in {"done", "cancelled", "superseded", "archived"}:
            raise TransitionError(f"cancel 不允许 terminal 状态 {current}")
        updates.update(
            {
                "status": "cancelled",
                "disposition": "cancelled",
                "cancelled_at": when,
                "cancellation_reason": args.reason or args.approval_evidence,
                "terminal_at": when,
                "lease_expires_at": None,
                "completed_at": None,
            }
        )

    elif args.action == "supersede":
        actor = args.actor.strip().lower()
        if actor != "user" or not args.approval_evidence:
            raise TransitionError("supersede 只能 actor=user 且必须提供 --approval-evidence")
        if current in {"done", "cancelled", "superseded", "archived"}:
            raise TransitionError(f"supersede 不允许 terminal 状态 {current}")
        replacement_id = args.replacement_task
        if not replacement_id:
            raise TransitionError("supersede 必须提供 --replacement-task")
        replacement_dir = board_root / "tasks" / replacement_id
        try:
            replacement = json.loads((replacement_dir / "status.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TransitionError(f"replacement task 不可读: {exc}") from exc
        replacement_integrity = get_task_integrity(replacement_dir, replacement)
        if not replacement_integrity.schedulable:
            raise TransitionError(
                f"replacement integrity failure: {replacement_integrity.status}: {replacement_integrity.detail}"
            )
        if replacement.get("supersedes") != args.task:
            raise TransitionError("replacement.supersedes 未指向当前旧任务")
        old_root = status.get("root_work_id") or args.task
        if replacement.get("root_work_id") != old_root:
            raise TransitionError("replacement 与旧任务 root_work_id 不一致")
        if status.get("work_key") and replacement.get("work_key") == status.get("work_key"):
            raise TransitionError("replacement 必须使用新的 work_key")
        updates.update(
            {
                "status": "superseded",
                "disposition": "superseded",
                "superseded_by": replacement_id,
                "superseded_at": when,
                "terminal_at": when,
                "lease_expires_at": None,
                "completed_at": None,
            }
        )

    elif args.action == "archive":
        actor = args.actor.strip().lower()
        if actor != "user" or not args.approval_evidence:
            raise TransitionError("archive 只能 actor=user 且必须提供 --approval-evidence")
        if current not in {"done", "cancelled", "superseded"}:
            raise TransitionError("archive 只允许 done/cancelled/superseded 终态")
        updates.update(
            {
                "status": "archived",
                "archived_from": current,
                "archived_at": when,
                "terminal_at": status.get("terminal_at") or when,
                "lease_expires_at": None,
            }
        )

    elif args.action == "approve":
        actor = args.actor.strip().lower()
        if actor != "user" or not args.approval_evidence:
            raise TransitionError("approve 只能 actor=user 且必须提供 --approval-evidence")
        if current != "awaiting_user_approval":
            raise TransitionError("approve 只允许 awaiting_user_approval 状态")
        if status.get("requires_governance_approval"):
            # 治理任务审批：需要技术验收通过 → done
            if not status.get("reviewer") or status.get("review_result") != "pass":
                raise TransitionError("approve 前必须已有独立技术验收 pass")
            updates.update(
                {"status": "done", "approved_by": "user", "approved_at": when, "completed_at": when}
            )
        else:
            raise TransitionError("approve 只允许已通过技术验收的治理任务；实施前方向审批使用 activate")
        payload["approval_evidence"] = args.approval_evidence

    elif args.action == "requeue":
        if args.actor == SYSTEM_REQUEUE_ACTOR:
            actor = SYSTEM_REQUEUE_ACTOR
            if args.model != "deterministic-v1":
                raise TransitionError("系统 requeue 模型必须是 deterministic-v1")
        else:
            actor = canonical_registered_actor(args.actor, args.model)
        if current not in {"claimed", "blocked", "dlq"}:
            raise TransitionError(f"requeue 不允许当前状态 {current}")
        new_count = int(status.get("requeue_count") or 0) + 1
        target = "dlq" if args.to_dlq else "open"
        updates.update(
            {
                "status": target,
                "requeue_count": new_count,
                "executor": None if target == "open" else status.get("executor"),
                "executor_model": None if target == "open" else status.get("executor_model"),
                "claimed_at": None if target == "open" else status.get("claimed_at"),
                "lease_expires_at": None,
                "blocked_at": None if target == "open" else status.get("blocked_at"),
            }
        )
        payload.update({"target": target})
        if args.expected_lease is not None:
            expected_fields["lease_expires_at"] = args.expected_lease
        if args.expected_blocked_at is not None:
            expected_fields["blocked_at"] = args.expected_blocked_at
    else:
        raise TransitionError(f"未知 action: {args.action}")

    after, event, idempotent = transition_task(
        board_root,
        args.task,
        action=args.action,
        actor=actor,
        actor_model=args.model,
        request_id=args.request_id,
        updates=updates,
        request_payload=payload,
        expected_status=current,
        expected_fields=expected_fields,
    )
    refresh_index(board_root)
    state = "幂等命中" if idempotent else "已提交"
    print(
        f"✅ {state} {args.task}: {event.get('from_status')} -> {after.get('status')} "
        f"seq={event.get('seq')} request_id={args.request_id}"
    )
    # CP9: 完成回调日志 — submit/block 时写 callbacks.jsonl
    if not idempotent and args.action in {"submit", "block"}:
        _log_callback(
            board_root, args.task, after, args.action, actor, args.model
        )
    # CP9+: Review 回调 — review 完成后写 callbacks.jsonl 并 best-effort 唤醒相关方
    if not idempotent and args.action == "review":
        _log_review_callback(
            board_root, args.task, after, actor, args.model, args.result
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="公告牌统一状态转换 CLI")
    parser.add_argument("--task", required=True)
    parser.add_argument(
        "--action", required=True,
        choices=["activate", "submit", "block", "review", "authorize-reviewer", "approve", "requeue", "cancel", "supersede", "archive", "recover"],
    )
    parser.add_argument("--actor", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--request-id", default="")
    parser.add_argument("--result", choices=sorted(RESULTS))
    parser.add_argument("--issues", type=int)
    parser.add_argument("--reason", default="")
    parser.add_argument("--approval-evidence", default="")
    parser.add_argument("--reviewer", default="")
    parser.add_argument("--reviewer-model", default="")
    parser.add_argument("--reviewer-family", default="")
    parser.add_argument("--requested-max-level", default="")
    parser.add_argument("--scope", default=REVIEW_AUTHORIZATION_SCOPE)
    parser.add_argument("--one-time", action="store_true")
    parser.add_argument("--output-ref", action="append", default=[])
    parser.add_argument("--evidence-ref", action="append", default=[])
    parser.add_argument("--replacement-task", default="")
    parser.add_argument("--lease-hours", type=float, default=2.0)
    parser.add_argument("--to-dlq", action="store_true")
    parser.add_argument("--expected-lease")
    parser.add_argument("--expected-blocked-at")
    parser.add_argument("--board-root", default=str(Path(__file__).parent))
    args = parser.parse_args()
    if args.action != "recover" and not args.request_id:
        parser.error("非 recover 动作必须提供唯一 --request-id")
    if not 0 < args.lease_hours <= 24:
        parser.error("--lease-hours 必须在 (0,24] 范围")
    try:
        return execute(args)
    except TransitionError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
