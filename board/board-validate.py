#!/usr/bin/env python3
"""board-validate — 公告牌当前态不变量校验（v2.0）。

校验真实可从磁盘证明的事项：身份、能力等级、模型、状态字段、租约、
验收独立性/异构性、REVIEW/BLOCKED/card 完整性、ID 与路径边界。

重要边界：旧版没有 append-only 事件源，不能从单份 status.json 证明历史状态
跳转是否合法；本脚本不再把“当前态校验”冒充“历史状态机校验”。

退出码：0=无阻断问题（可含白色信息）；1=至少一个红/黄问题；2=运行错误。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from board_integrity import RECONCILED_HISTORY, get_task_integrity
from board_reconcile_contract import RECONCILE_PENDING_FILE
from board_task_contract import load_and_validate_contract, review_binds_contract
from board_verdicts import LEGACY_REVIEW_RESULTS, REVIEW_RESULTS, parse_legacy_verdict
from board_contract import (
    AGENT_CONTRACTS,
    ALLOWED_STATUSES,
    CONTRACT_VERSION,
    NO_ELIGIBLE_INDEPENDENT_REVIEWER,
    atomic_write_json,
    canonical_agent,
    card_hash,
    effective_execute_level,
    effective_review_level,
    eligible_reviewers,
    is_canonical_agent,
    is_physical_task_dir,
    is_safe_token,
    level_value,
    model_family,
    requires_independent_review,
    review_authorization_allows,
    review_override_allows,
    validate_model_label,
    validate_task_id,
)
from board_transition import (
    EVENTS_FILE,
    PENDING_FILE,
    TransitionError,
    event_hash,
    load_events,
    review_authorization_is_event_backed,
    signed_review_matches,
    status_hash,
    transition_allowed,
)

CN_TZ = timezone(timedelta(hours=8))
RESULTS = set(REVIEW_RESULTS)


def parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        normalized = value.strip()
        # Python 3.9 对 ISO 8601 基本时区偏移（+0800）兼容不稳定，归一为 +08:00。
        if re.search(r"[+-]\d{4}$", normalized):
            normalized = normalized[:-2] + ":" + normalized[-2:]
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=CN_TZ)
    except ValueError:
        return None


def add(issues, category, task_id, level, message):
    issues.append(
        {"category": category, "task_id": task_id, "level": level, "message": message}
    )


def is_fixture(status):
    return "fixture" in str(status.get("note", "")).lower()


def review_result_from_text(text: str):
    return parse_legacy_verdict(text)


def requires_hetero(card_text: str, complexity: str) -> bool:
    explicit = bool(
        re.search(r"require_hetero_model\s*:\s*true", card_text, re.IGNORECASE)
    )
    return explicit or level_value(complexity) >= 3


def validate_identity(
    issues,
    task_id,
    role,
    agent_value,
    model_value,
    *,
    strict,
):
    if not agent_value:
        add(issues, f"{role}_MISSING", task_id, "🔴" if strict else "🟡", f"{role.lower()} 为空")
        return None
    canonical = canonical_agent(agent_value)
    if not canonical:
        add(
            issues,
            f"{role}_UNREGISTERED",
            task_id,
            "🔴" if strict else "🟡",
            f"{role.lower()}={agent_value!r} 不在 board 注册端",
        )
        return None
    if strict and not is_canonical_agent(agent_value):
        add(
            issues,
            f"{role}_NONCANONICAL",
            task_id,
            "🔴",
            f"{role.lower()} 必须写注册 key {canonical!r}，不能写显示名 {agent_value!r}",
        )
    if not validate_model_label(model_value):
        add(
            issues,
            f"{role}_MODEL_INVALID",
            task_id,
            "🔴" if strict else "🟡",
            f"{role.lower()}_model={model_value!r} 不是可核验的真实模型档名",
        )
    return canonical


def validate_history(issues, task_dir: Path, status: dict):
    """回放可证的 committed event chain。

    full=从 create genesis 起可证；partial=旧任务从 bootstrap 锚点起可证；
    untraceable=尚未采用事件源，只做白色披露，不伪造历史。
    """
    task_id = task_dir.name
    pending_exists = (task_dir / PENDING_FILE).exists()
    if pending_exists:
        add(
            issues,
            "HISTORY_PENDING",
            task_id,
            "🔴",
            "存在未完成 pending journal，必须先执行 transition recover",
        )
    reconcile_pending = (task_dir / RECONCILE_PENDING_FILE).exists()
    if reconcile_pending:
        add(
            issues,
            "HISTORY_RECONCILE_PENDING",
            task_id,
            "🔴",
            "存在未完成 reconcile journal，必须先执行 board-reconcile-history --recover",
        )
    if not (task_dir / EVENTS_FILE).exists():
        add(
            issues,
            "HISTORY_UNTRACEABLE",
            task_id,
            "⚪",
            "无事件源；升级前历史不可追溯",
        )
        return
    try:
        events = load_events(task_dir)
    except TransitionError as exc:
        add(issues, "HISTORY_PARSE_ERROR", task_id, "🔴", str(exc))
        return
    if not events:
        add(issues, "HISTORY_EMPTY", task_id, "🔴", "events.jsonl 存在但无事件")
        return

    integrity = get_task_integrity(task_dir, status)
    if integrity.status == RECONCILED_HISTORY and not reconcile_pending:
        add(
            issues,
            "HISTORY_RECONCILED",
            task_id,
            "⚪",
            "旧前缀保持隔离；从经备份与决策证明的 reconcile anchor 起可严格回放",
        )
        return

    history_errors = False
    first = events[0]
    scope = first.get("history_scope")
    if first.get("action") == "create":
        first_ok = (
            scope == "full"
            and first.get("from_status") is None
            and first.get("to_status") in {"open", "awaiting_user_approval"}  # v1.3: CP2/CP3
            and first.get("before_hash") is None
        )
    elif first.get("action") == "bootstrap":
        first_ok = (
            scope == "partial"
            and first.get("from_status") == first.get("to_status")
            and first.get("before_hash") == first.get("after_hash")
        )
    elif first.get("action") == "archive":
        # 管理归档：无前置 create 事件（早期归档操作）
        first_ok = (
            scope == "full"
            and first.get("from_status") in {"open", None}
            and first.get("to_status") == "done"
            and first.get("prev_event_hash") is None
        )
    else:
        first_ok = False
    if not first_ok:
        add(issues, "HISTORY_BAD_ANCHOR", task_id, "🔴", "首事件不是合法 create/full 或 bootstrap/partial 锚点")
        history_errors = True

    previous = None
    seen_requests = set()
    has_archive = False
    for expected_seq, event in enumerate(events, 1):
        is_archive_event = event.get("action") == "archive"
        if is_archive_event:
            has_archive = True
        if event.get("schema") != "board-event/v1" or event.get("seq") != expected_seq:
            add(issues, "HISTORY_SEQUENCE", task_id, "🔴", f"第 {expected_seq} 事件 schema/seq 不合法")
            history_errors = True
        if not is_archive_event and event_hash(event) != event.get("event_hash"):
            add(issues, "HISTORY_EVENT_HASH", task_id, "🔴", f"第 {expected_seq} 事件 hash 不匹配")
            history_errors = True
        request_id = event.get("request_id")
        request_key = request_id if isinstance(request_id, str) else f"<invalid:{expected_seq}>"
        if not isinstance(request_id, str) or not request_id or request_key in seen_requests:
            add(issues, "HISTORY_REQUEST_ID", task_id, "🔴", f"第 {expected_seq} 事件 request_id 空或重复")
            history_errors = True
        seen_requests.add(request_key)
        if event.get("history_scope") != scope:
            add(issues, "HISTORY_SCOPE_DRIFT", task_id, "🔴", f"第 {expected_seq} 事件 scope 漂移")
            history_errors = True
        if previous is None:
            if event.get("prev_event_hash") is not None:
                add(issues, "HISTORY_PREV_HASH", task_id, "🔴", "首事件 prev_event_hash 必须为 null")
                history_errors = True
        else:
            if event.get("prev_event_hash") != previous.get("event_hash"):
                add(issues, "HISTORY_PREV_HASH", task_id, "🔴", f"第 {expected_seq} 事件 prev hash 断链")
                history_errors = True
            if not is_archive_event and event.get("before_hash") != previous.get("after_hash"):
                add(issues, "HISTORY_STATUS_HASH_CHAIN", task_id, "🔴", f"第 {expected_seq} 事件 before/after hash 不连续")
                history_errors = True
            if event.get("from_status") != previous.get("to_status"):
                add(issues, "HISTORY_STATUS_CHAIN", task_id, "🔴", f"第 {expected_seq} 事件 from/to 不连续")
                history_errors = True
            from_status = event.get("from_status")
            to_status = event.get("to_status")
            if not transition_allowed(event.get("action"), from_status, to_status):
                add(issues, "HISTORY_ILLEGAL_TRANSITION", task_id, "🔴", f"第 {expected_seq} 事件非法跳转 {from_status}->{to_status}")
                history_errors = True
        previous = event

    if not pending_exists and not has_archive and events[-1].get("after_hash") != status_hash(status):
        add(issues, "HISTORY_STATUS_DIVERGED", task_id, "🔴", "最后 committed event 与当前 status 不一致")
        history_errors = True
    if not history_errors and not pending_exists:
        category = "HISTORY_FULL" if scope == "full" else "HISTORY_PARTIAL"
        message = "从 create 起完整可回放" if scope == "full" else "从首次 bootstrap 锚点起可回放；更早历史不可追溯"
        add(issues, category, task_id, "⚪", message)


def validate_one(task_dir: Path, *, audit_legacy: bool):
    issues = []
    tasks_root = task_dir.parent
    if not is_physical_task_dir(task_dir, tasks_root):
        add(
            issues,
            "UNSAFE_TASK_DIR",
            task_dir.name,
            "🔴",
            "任务项不是 tasks/ 下的真实直接子目录（疑似符号链接或逃逸）",
        )
        return issues
    status_path = task_dir / "status.json"
    card_path = task_dir / "card.md"
    task_id = task_dir.name

    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        add(issues, "PARSE_ERROR", task_id, "🔴", f"status.json 解析失败: {exc}")
        return issues

    if not validate_task_id(task_id):
        add(issues, "UNSAFE_TASK_DIR", task_id, "🔴", "任务目录名不是安全单段 ID")
    reported_id = status.get("id")
    if reported_id != task_id:
        add(
            issues,
            "ID_PATH_MISMATCH",
            task_id,
            "🔴",
            f"status.id={reported_id!r} 与目录名不一致；写入脚本不得用 status.id 拼路径",
        )

    current = status.get("status")
    if current not in ALLOWED_STATUSES:
        add(issues, "UNKNOWN_STATUS", task_id, "🔴", f"未知状态: {current!r}")
        return issues

    contract_version = status.get("contract_version") or 0
    terminal_statuses = {"done", "cancelled", "superseded", "archived"}
    active = current not in terminal_statuses
    strict = contract_version >= CONTRACT_VERSION
    fixture = is_fixture(status)

    integrity = get_task_integrity(task_dir, status)
    if integrity.quarantined:
        add(
            issues,
            "INTEGRITY_QUARANTINED",
            task_id,
            "🔴",
            f"{integrity.status}: {integrity.detail}",
        )

    if active and contract_version < CONTRACT_VERSION:
        add(
            issues,
            "LEGACY_ACTIVE",
            task_id,
            "⚪",
            f"legacy compatibility：活跃任务保留旧契约（contract_version={contract_version or 'missing'}）",
        )

    card_text = ""
    if not card_path.exists():
        add(issues, "CARD_MISSING", task_id, "🔴", "card.md 缺失")
    else:
        try:
            card_text = card_path.read_text(encoding="utf-8")
        except OSError as exc:
            add(issues, "CARD_UNREADABLE", task_id, "🔴", str(exc))
        actual_hash = card_hash(card_path)
        stored_hash = status.get("card_hash")
        if not stored_hash:
            add(
                issues,
                "CARD_NO_HASH",
                task_id,
                "🔴" if strict and not fixture else "⚪",
                "card_hash 缺失",
            )
        elif actual_hash != stored_hash:
            add(
                issues,
                "CARD_TAMPERED",
                task_id,
                "🔴",
                f"card hash 不匹配（stored={stored_hash}, actual={actual_hash}）",
            )

    required_caps = status.get("required_caps")
    if (
        not isinstance(required_caps, list)
        or not required_caps
        or any(not is_safe_token(cap) for cap in required_caps)
    ):
        add(issues, "CAPS_INVALID", task_id, "🔴", f"required_caps 非法: {required_caps!r}")
        required_caps = []

    complexity = status.get("complexity", "L1")
    if level_value(complexity) not in {1, 2, 3, 4}:
        add(issues, "COMPLEXITY_INVALID", task_id, "🔴", f"complexity 非法: {complexity!r}")

    authorization = status.get("review_authorization")
    authorization_event_backed = False
    if status.get("review_override") is not None:
        add(
            issues,
            "LEGACY_REVIEW_OVERRIDE",
            task_id,
            "🔴" if contract_version >= 3 else "⚪",
            "review_override 仅作 v2 历史兼容；v3 必须使用 authorize-reviewer event",
        )
    if authorization is not None:
        authorization_event_backed = review_authorization_is_event_backed(
            task_dir, status
        )
        if contract_version < 2:
            add(
                issues,
                "REVIEW_AUTHORIZATION_CONTRACT_INVALID",
                task_id,
                "🔴",
                "review_authorization 只允许 event-managed v2+ task",
            )
        if not authorization_event_backed:
            add(
                issues,
                "REVIEW_AUTHORIZATION_UNTRACED",
                task_id,
                "🔴",
                "review_authorization 没有匹配的 authorize-reviewer governance event",
            )
        if not isinstance(authorization, dict) or not review_authorization_allows(
            status,
            authorization.get("reviewer") if isinstance(authorization, dict) else None,
            authorization.get("reviewer_model") if isinstance(authorization, dict) else None,
            card_text=card_text,
            allow_consumed=True,
        ):
            add(
                issues,
                "REVIEW_AUTHORIZATION_INVALID",
                task_id,
                "🔴",
                "review_authorization 结构、范围、独立性或 lifecycle 无效",
            )

    native_current = f"contract_version: {CONTRACT_VERSION}" in card_text
    creator = status.get("created_by")
    if native_current and not is_canonical_agent(creator):
        add(
            issues,
            "CREATOR_NONCANONICAL",
            task_id,
            "🔴",
            f"v{CONTRACT_VERSION} 任务 created_by 必须是小写注册 key，当前={creator!r}",
        )

    qualified_contract, qualified_error = load_and_validate_contract(task_dir, status)
    if qualified_error:
        add(issues, "QUALIFIED_CONTRACT_INVALID", task_id, "🔴", qualified_error)

    # 检测归档事件，供执行端/审查端豁免使用
    event_has_archive = False
    if (task_dir / EVENTS_FILE).exists():
        try:
            events_list = load_events(task_dir)
            event_has_archive = any(
                event.get("action") == "archive" for event in events_list
            )
        except TransitionError:
            pass

    executor = status.get("executor")
    executor_model = status.get("executor_model")
    executor_key = None
    pre_execution_approval = (
        current == "awaiting_user_approval"
        and bool(status.get("approval_reason"))
        and not status.get("reviewer")
    )
    execution_states = {"claimed", "awaiting_review", "awaiting_user_approval", "done", "blocked", "dlq"}
    has_execution_identity = bool(status.get("executor") or status.get("claimed_at"))
    if (
        current in execution_states
        and not pre_execution_approval
        and not (fixture and current == "done")
        and not event_has_archive
    ) or (current in {"cancelled", "superseded", "archived"} and has_execution_identity):
        executor_key = validate_identity(
            issues,
            task_id,
            "EXECUTOR",
            executor,
            executor_model,
            strict=strict,
        )
        if executor_key:
            profile = AGENT_CONTRACTS[executor_key]
            missing_caps = [cap for cap in required_caps if cap not in profile["caps"]]
            if missing_caps:
                add(
                    issues,
                    "CAP_MISMATCH",
                    task_id,
                    "🔴" if strict else "🟡",
                    f"{executor_key} 缺 required_caps={missing_caps}",
                )
            max_execute = effective_execute_level(executor_key, executor_model)
            if level_value(complexity) > level_value(max_execute):
                add(
                    issues,
                    "EXECUTOR_LEVEL_EXCEEDED",
                    task_id,
                    "🔴" if strict else "🟡",
                    f"{executor_key}/{executor_model} 最高执行 {max_execute}，任务为 {complexity}",
                )

    if (
        current in {"claimed", "awaiting_review"}
        and executor_key
        and requires_independent_review(status)
        and not eligible_reviewers(
            status,
            executor_key,
            executor_model=executor_model,
            card_text=card_text,
            include_authorization=authorization_event_backed,
        )
    ):
        add(
            issues,
            NO_ELIGIBLE_INDEPENDENT_REVIEWER,
            task_id,
            "🔴",
            f"{current} task 没有合法 independent reviewer path",
        )

    lease = status.get("lease_expires_at")
    claimed_at = status.get("claimed_at")
    lease_dt = parse_iso(lease)
    claimed_dt = parse_iso(claimed_at)
    if current == "claimed":
        if not claimed_dt:
            add(issues, "CLAIMED_AT_INVALID", task_id, "🔴", f"claimed_at 非法: {claimed_at!r}")
        if not lease_dt:
            add(issues, "CLAIMED_NO_LEASE", task_id, "🔴", f"lease_expires_at 非法: {lease!r}")
        elif claimed_dt and lease_dt <= claimed_dt:
            add(issues, "LEASE_ORDER_INVALID", task_id, "🔴", "lease 必须晚于 claimed_at")
        elif lease_dt < datetime.now(CN_TZ):
            add(issues, "LEASE_EXPIRED", task_id, "🟡", f"租约已过期: {lease}")
    elif current in {"awaiting_review", "awaiting_user_approval", "done", "open"} and lease:
        level = "🟡" if current == "done" and not audit_legacy and contract_version < CONTRACT_VERSION else "🔴"
        add(issues, "STALE_LEASE", task_id, level, f"{current} 不应保留 lease_expires_at={lease!r}")

    if current == "open" and any(status.get(key) for key in ("executor", "executor_model", "claimed_at")):
        add(issues, "OPEN_HAS_CLAIM", task_id, "🔴", "open 任务仍带认领身份/时间")

    if current == "awaiting_review" and not claimed_dt:
        add(issues, "AWAITING_REVIEW_NO_CLAIM", task_id, "🔴", "awaiting_review 缺 claimed_at")

    if current == "blocked":
        if not (task_dir / "BLOCKED.md").exists():
            add(issues, "BLOCKED_FILE_MISSING", task_id, "🔴", "blocked 任务缺 BLOCKED.md")
        if not parse_iso(status.get("blocked_at")):
            add(
                issues,
                "BLOCKED_AT_MISSING",
                task_id,
                "🟡",
                "blocked_at 缺失；lease checker 不能从 claimed_at 推断阻塞起点",
            )

    review_states = {"done", "awaiting_user_approval"}
    event_managed_review = False
    if (task_dir / EVENTS_FILE).exists():
        try:
            events_list = load_events(task_dir)
            event_managed_review = any(
                event.get("action") == "review" for event in events_list
            )
        except TransitionError:
            # 历史解析错误由 validate_history 统一报；这里不重复制造次生噪音。
            pass
    if current in review_states and not fixture and not event_has_archive:
        reviewer = status.get("reviewer")
        reviewer_model = status.get("reviewer_model")
        reviewer_key = validate_identity(
            issues,
            task_id,
            "REVIEWER",
            reviewer,
            reviewer_model,
            strict=strict,
        )
        result = str(status.get("review_result") or "").lower()
        allowed_results = RESULTS | (set(LEGACY_REVIEW_RESULTS) if contract_version < 3 else set())
        if result not in allowed_results:
            add(issues, "REVIEW_RESULT_INVALID", task_id, "🔴" if strict else "🟡", f"review_result={result!r}")
        if not isinstance(status.get("review_round"), int) or status.get("review_round", 0) < 1:
            add(issues, "REVIEW_ROUND_INVALID", task_id, "🔴" if strict else "🟡", "review_round 必须 >=1")
        if not isinstance(status.get("review_issues_count"), int) or status.get("review_issues_count", -1) < 0:
            add(issues, "REVIEW_ISSUES_INVALID", task_id, "🔴" if strict else "🟡", "review_issues_count 必须是非负整数")

        review_path = task_dir / "REVIEW.md"
        if not review_path.exists():
            add(issues, "REVIEW_MISSING", task_id, "🔴" if strict else "🟡", "终态任务缺 REVIEW.md")
        else:
            try:
                review_text = review_path.read_text(encoding="utf-8")
                text_result = review_result_from_text(review_text)
                if text_result and result in allowed_results and text_result != result:
                    add(
                        issues,
                        "REVIEW_RESULT_MISMATCH",
                        task_id,
                        "🔴",
                        f"REVIEW.md={text_result}, status.json={result}",
                    )
                if (
                    event_managed_review
                    and
                    reviewer_key
                    and validate_model_label(reviewer_model)
                    and result in RESULTS
                    and not signed_review_matches(task_dir, reviewer_key, reviewer_model, result)
                ):
                    add(
                        issues,
                        "REVIEW_EVIDENCE_MISMATCH",
                        task_id,
                        "🔴" if strict else "🟡",
                        "REVIEW.md 未绑定 status 中的验收端/模型/结果",
                    )
            except OSError as exc:
                add(issues, "REVIEW_UNREADABLE", task_id, "🔴", str(exc))

        if contract_version >= 3:
            fingerprint = status.get("contract_fingerprint")
            if (
                status.get("submission_contract_version") != contract_version
                or status.get("submission_contract_fingerprint") != fingerprint
                or not status.get("submission_outputs")
                or not status.get("submission_evidence")
            ):
                add(
                    issues,
                    "SUBMISSION_CONTRACT_INVALID",
                    task_id,
                    "🔴",
                    "submission 未绑定 v3 合同或缺 output/evidence",
                )
            if reviewer and not review_binds_contract(task_dir, fingerprint):
                add(
                    issues,
                    "REVIEW_CONTRACT_MISMATCH",
                    task_id,
                    "🔴",
                    "REVIEW.md 未绑定冻结合同指纹",
                )

        if reviewer_key:
            max_review = effective_review_level(reviewer_key, reviewer_model)
            if level_value(complexity) > level_value(max_review):
                if (
                    authorization_event_backed
                    and review_authorization_allows(
                        status,
                        reviewer_key,
                        reviewer_model,
                        card_text=card_text,
                        allow_consumed=True,
                    )
                ):
                    add(
                        issues,
                        "REVIEW_LEVEL_AUTHORIZED",
                        task_id,
                        "⚪",
                        f"用户 event-backed task-scoped one-shot 授权: {reviewer_key}/{reviewer_model} 验收 {complexity}",
                    )
                elif contract_version < 3 and review_override_allows(
                    status, reviewer_key, reviewer_model
                ):
                    add(
                        issues,
                        "REVIEW_LEVEL_OVERRIDE",
                        task_id,
                        "⚪",
                        f"v2 历史一次性例外: {reviewer_key}/{reviewer_model} 验收 {complexity}",
                    )
                else:
                    add(
                        issues,
                        "REVIEWER_LEVEL_EXCEEDED",
                        task_id,
                        "🔴" if strict else "🟡",
                        f"{reviewer_key}/{reviewer_model} 最高验收 {max_review}，任务为 {complexity}",
                    )
            if reviewer_key == executor_key:
                add(
                    issues,
                    "SELF_CLOSE",
                    task_id,
                    "🔴",
                    "executor 与 reviewer 必须是不同注册端；授权也不可 self-review",
                )

            if requires_hetero(card_text, complexity):
                exec_family = model_family(executor_model)
                review_family = model_family(reviewer_model)
                if not exec_family or not review_family:
                    add(
                        issues,
                        "HETERO_UNPROVABLE",
                        task_id,
                        "🔴" if strict else "🟡",
                        f"无法从模型名证明异构: {executor_model!r} vs {reviewer_model!r}",
                    )
                elif exec_family == review_family:
                    add(
                        issues,
                        "HETERO_REQUIRED",
                        task_id,
                        "🔴",
                        f"{complexity} 要求异模型家族，当前均为 {exec_family}",
                    )

    if current == "done" and not status.get("completed_at"):
        add(issues, "DONE_NO_COMPLETED_AT", task_id, "🔴" if strict else "🟡", "done 缺 completed_at")
    if (
        current == "awaiting_user_approval"
        and not status.get("requires_governance_approval")
        and not status.get("approval_reason")
        and status.get("task_type") != "decision"
    ):
        add(issues, "APPROVAL_WITHOUT_REASON", task_id, "🔴", "awaiting_user_approval 缺治理或方向/决策原因")
    if current == "cancelled" and not parse_iso(status.get("cancelled_at")):
        add(issues, "CANCELLED_AT_MISSING", task_id, "🔴", "cancelled 缺 cancelled_at")
    if current == "superseded":
        if not parse_iso(status.get("superseded_at")) or not status.get("superseded_by"):
            add(issues, "SUPERSEDE_LINK_MISSING", task_id, "🔴", "superseded 缺时间或 superseded_by")
    if current == "archived" and (
        not parse_iso(status.get("archived_at"))
        or status.get("archived_from") not in {"done", "cancelled", "superseded"}
    ):
        add(issues, "ARCHIVE_METADATA_INVALID", task_id, "🔴", "archived 缺合法 archived_at/archived_from")

    if not active and contract_version < CONTRACT_VERSION and not audit_legacy:
        # 对历史 done 数据仍报告硬解析/篡改问题；其余语义问题降级由 --audit-legacy 展开。
        for issue in issues:
            if issue["level"] == "🟡" and issue["category"] not in {"PARSE_ERROR", "CARD_TAMPERED", "ID_PATH_MISMATCH"}:
                issue["level"] = "⚪"

    validate_history(issues, task_dir, status)

    return issues


def fix_missing_hashes(board_root: Path):
    fixed = 0
    tasks_root = board_root / "tasks"
    for task_dir in sorted(tasks_root.iterdir()):
        status_path = task_dir / "status.json"
        card_path = task_dir / "card.md"
        if not is_physical_task_dir(task_dir, tasks_root) or not status_path.exists() or not card_path.exists():
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if status.get("card_hash"):
            continue
        digest = card_hash(card_path)
        if digest:
            status["card_hash"] = digest
            atomic_write_json(status_path, status)
            fixed += 1
    return fixed


def _parse_version_tuple(iteration: str):
    """从 iteration 字符串中提取数值化版本元组，用于可靠的新旧比较。

    "v16.1" → (16, 1)；"v16.3-qwen-polish" → (16, 3)；
    "board-v1.1" → (1, 1)；无法解析 → None。
    """
    import re
    m = re.search(r"v?(\d+)(?:\.(\d+))?", iteration)
    if not m:
        return None
    major = int(m.group(1))
    minor = int(m.group(2)) if m.group(2) else 0
    return (major, minor)


def _is_version_chain_related(impl: dict, spec: dict) -> bool:
    """判断实施任务与规格任务是否存在版本链关系（P1-2 修复）。

    满足以下任一条件才视为有版本链关系：
    - 同 iteration
    - impl 的 depends_on 直接包含 spec 的 ID
    - spec 的 created_at 早于 impl 的 created_at（规格先于实施创建）
    """
    # 同 iteration
    if impl.get("iteration") and impl["iteration"] == spec.get("iteration"):
        return True
    # depends_on 直接关系
    if spec["id"] in (impl.get("depends_on") or []):
        return True
    # created_at 时间先后（spec 先创建）
    impl_created = impl.get("created_at") or ""
    spec_created = spec.get("created_at") or ""
    if impl_created and spec_created and impl_created > spec_created:
        return True
    return False


def validate_version_coherence(board_root: Path):
    """跨任务版本链一致性检查（方案 A，P1-1/P1-2/P1-3/P2-1 修复版）。"""
    from collections import defaultdict

    issues = []
    tasks_root = board_root / "tasks"

    impl_keywords = ["实施", "implementation", "implement", "实现"]
    spec_keywords = ["示意图", "规格", "spec", "design", "设计", "mockup"]
    review_keywords = ["验收", "review", "审查", "audit", "对抗性", "adversarial"]

    def has_kw(text, kws):
        lower = text.lower()
        return any(kw in lower for kw in kws)

    def classify_task(card_text):
        """分类任务类型，收紧判定避免误报（P1-3 修复）。

        只检查 card 标题行（首个 H1），不检查全文——因为几乎所有 card 的
        "验收标准"段落都含"验收"关键词，全文匹配会导致所有任务被误判为 review。

        - 标题含 review 关键词 → review 任务，不归入 impl 或 spec
        - 标题同时含 impl + spec → 元任务，不归入任一类
        - 标题仅含 impl → is_impl=True
        - 标题仅含 spec → is_spec=True
        """
        title = ""
        for line in card_text.splitlines():
            line = line.strip()
            if line.startswith("# "):
                title = line
                break
        is_review = has_kw(title, review_keywords)
        is_impl_kw = has_kw(title, impl_keywords)
        is_spec_kw = has_kw(title, spec_keywords)
        if is_review:
            return False, False
        if is_impl_kw and is_spec_kw:
            return False, False
        return is_impl_kw, is_spec_kw

    # 收集所有任务信息
    tasks_info = []
    for task_dir in sorted(tasks_root.iterdir()):
        if task_dir.name.startswith((".", "_")):
            continue
        if not is_physical_task_dir(task_dir, tasks_root):
            continue
        status_path = task_dir / "status.json"
        card_path = task_dir / "card.md"
        if not status_path.exists() or not card_path.exists():
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            card_text = card_path.read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            continue
        is_impl, is_spec = classify_task(card_text)
        tasks_info.append({
            "id": task_dir.name,
            "project": status.get("project", ""),
            "iteration": status.get("iteration", ""),
            "status": status.get("status", ""),
            "depends_on": status.get("depends_on") or [],
            "created_at": status.get("created_at", ""),
            "is_impl": is_impl,
            "is_spec": is_spec,
        })

    by_project = defaultdict(list)
    for t in tasks_info:
        if t["project"]:
            by_project[t["project"]].append(t)

    for project, tasks in by_project.items():
        # 检查 1: 实施任务已 claimed+ 但规格/设计任务仍 awaiting_review → 🟡
        # P1-1 修复：排除同任务自指
        # P1-2 修复：仅当存在版本链关系时才告警
        impl_active = [t for t in tasks if t["is_impl"] and t["status"] in
                       {"claimed", "awaiting_review", "awaiting_user_approval", "done"}]
        spec_pending = [t for t in tasks if t["is_spec"] and t["status"] == "awaiting_review"]
        for impl in impl_active:
            for spec in spec_pending:
                if impl["id"] == spec["id"]:
                    continue
                if not _is_version_chain_related(impl, spec):
                    continue
                add(issues, "VERSION_COHERENCE", impl["id"], "🟡",
                    f"实施任务已进入 {impl['status']}，但同项目规格/设计任务 {spec['id']} 仍 awaiting_review")

        # 检查 2: 同 project + 同 iteration 多个活跃任务无 depends_on → ⚪
        by_iter = defaultdict(list)
        for t in tasks:
            if t["iteration"]:
                by_iter[t["iteration"]].append(t)
        for iteration, iter_tasks in by_iter.items():
            active_no_dep = [t for t in iter_tasks
                             if t["status"] in {"open", "claimed"} and not t["depends_on"]]
            if len(active_no_dep) > 1:
                ids = ", ".join(t["id"] for t in active_no_dep)
                for t in active_no_dep:
                    add(issues, "VERSION_COHERENCE", t["id"], "⚪",
                        f"同项目同轮次有 {len(active_no_dep)} 个活跃任务无依赖关系（可能重叠: {ids}）")

        # 检查 3: 旧 iteration 仍 open 但新 iteration 已 done → ⚪
        # P2-1 修复：用数值化版本比较替代字符串字典序，无法解析时跳过
        iter_map = defaultdict(lambda: {"open": [], "done": []})
        for t in tasks:
            if t["iteration"]:
                if t["status"] == "open":
                    iter_map[t["iteration"]]["open"].append(t["id"])
                elif t["status"] == "done":
                    iter_map[t["iteration"]]["done"].append(t["id"])
        # 按数值化版本排序；无法解析的 iteration 不参与检查 3
        parsed_iters = []
        for iter_name in iter_map:
            vt = _parse_version_tuple(iter_name)
            if vt is not None:
                parsed_iters.append((vt, iter_name))
        parsed_iters.sort(key=lambda x: x[0])
        for i, (_, older) in enumerate(parsed_iters):
            for _, newer in parsed_iters[i + 1:]:
                if iter_map[older]["open"] and iter_map[newer]["done"]:
                    for open_id in iter_map[older]["open"]:
                        add(issues, "VERSION_COHERENCE", open_id, "⚪",
                            f"旧轮次 {older} 仍 open，但新轮次 {newer} 已有 done 任务（可能已被取代）")

    return issues


def validate_family_coherence(board_root: Path):
    """检查点7：家族一致性检查（v1.3 新增）。
    检查有 family 字段的任务：goal_alignment 完整性、规模阈值、方向分裂、冻结家族活跃任务。
    """
    issues = []
    tasks_root = board_root / "tasks"
    if not tasks_root.exists():
        return issues

    # 收集家族
    families = {}
    for task_dir in sorted(tasks_root.iterdir()):
        if not task_dir.is_dir() or task_dir.name.startswith((".", "_")):
            continue
        card_path = task_dir / "card.md"
        status_path = task_dir / "status.json"
        if not card_path.exists() or not status_path.exists():
            continue
        try:
            card_text = card_path.read_text(encoding="utf-8")
            # 按行解析 frontmatter 找 family 字段
            family = None
            in_fm = False
            for line in card_text.splitlines():
                if line.strip() == "---":
                    in_fm = not in_fm
                    continue
                if not in_fm:
                    continue
                if line.strip().startswith("family:"):
                    family = line.split(":", 1)[1].strip()
                    break
            if not family:
                continue
            status = json.loads(status_path.read_text(encoding="utf-8"))
            # 提取 goal_alignment
            ga = None
            in_fm = False
            for line in card_text.splitlines():
                if line.strip() == "---":
                    in_fm = not in_fm
                    continue
                if not in_fm:
                    continue
                if line.strip().startswith("goal_alignment:"):
                    ga = line.split(":", 1)[1].strip()
                    if ga.startswith('"') and ga.endswith('"'):
                        ga = ga[1:-1]
                    break
            status["_goal_alignment"] = ga
            families.setdefault(family, []).append(status)
        except (OSError, json.JSONDecodeError):
            continue

    for family_id, tasks in families.items():
        # 1. 检查 goal_alignment 完整性
        for t in tasks:
            if not t.get("_goal_alignment"):
                add(issues, "FAMILY_COHERENCE", t.get("id", "?"), "🟡",
                    f"家族 {family_id} 的任务缺少 goal_alignment")

        # 2. 检查家族规模阈值
        if len(tasks) > 5:
            add(issues, "FAMILY_COHERENCE", family_id, "⚪",
                f"家族任务数 {len(tasks)} > 5，建议健康检查")

        # 3. 检查方向分裂
        DIRECTION_PREFIXES = ["服务于", "实现", "完成", "按", "根据", "基于"]
        directions = []
        for t in tasks:
            ga = t.get("_goal_alignment", "")
            if not ga:
                continue
            cleaned = ga
            for prefix in DIRECTION_PREFIXES:
                if cleaned.startswith(prefix):
                    cleaned = cleaned[len(prefix):]
                    break
            cleaned = cleaned.strip("，。、,. ")
            directions.append(cleaned[:20])
        unique_directions = list(set(directions))
        if len(unique_directions) > 1:
            add(issues, "FAMILY_COHERENCE", family_id, "🔴",
                f"检测到方向分裂: {unique_directions}")

        # 4. 检查冻结家族下有非 decision 任务仍 open/claimed
        family_status_path = board_root / "families" / family_id / "status.json"
        if family_status_path.exists():
            try:
                family_status = json.loads(family_status_path.read_text(encoding="utf-8"))
                if family_status.get("status") == "frozen":
                    for t in tasks:
                        if t.get("status") in ("open", "claimed") and t.get("task_type") != "decision":
                            add(issues, "FAMILY_COHERENCE", t.get("id", "?"), "🔴",
                                f"家族 {family_id} 已冻结但任务仍 {t.get('status')}")
            except (OSError, json.JSONDecodeError):
                pass

    return issues


def validate_reviewer_redundancy(board_root: Path):
    """验收通道冗余检查（2026-08-12 治理新增，P0-1）。

    每个复杂度等级（L1-L4）至少需 2 个异模型家族验收端，避免单点依赖
    （实证：2026-08-12 前 workbuddy L2 + codex 额度断档 + qwenwork 未登录
    → 5 个 L3 任务积压 60h+）。
    仅对非终态任务所在的复杂度等级检查；纯配置快照，不涉及任务卡。
    """
    issues = []
    try:
        from board_contract import AGENT_CONTRACTS, level_value
    except Exception:
        return issues
    # 收集非终态任务的复杂度集合
    tasks_root = board_root / "tasks"
    active_levels = set()
    if tasks_root.exists():
        terminal = {"done", "cancelled", "superseded", "archived"}
        for task_dir in tasks_root.iterdir():
            status_path = task_dir / "status.json"
            if not status_path.exists():
                continue
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if status.get("status") in terminal:
                continue
            complexity = status.get("complexity", "L1")
            if isinstance(complexity, str) and complexity.upper().startswith("L"):
                active_levels.add(complexity.upper())
    if not active_levels:
        return issues
    max_level = max(active_levels, key=level_value)
    # 每个等级：能验收它的端（max_review_level >= 等级）按模型家族去重计数
    for level in sorted(active_levels, key=level_value):
        eligible = {}
        for agent, profile in AGENT_CONTRACTS.items():
            if level_value(profile.get("max_review_level", "L0")) < level_value(level):
                continue
            for fam in profile.get("model_families", []):
                eligible.setdefault(fam, []).append(agent)
        distinct_families = len(eligible)
        if distinct_families < 2:
            msg = (
                f"复杂度 {level} 验收端仅 {distinct_families} 个模型家族"
                f"（{ {f: a for f, a in eligible.items()} }），存在单点验收风险；"
                "建议至少 2 个异家族验收端"
            )
            # 仅对当前活跃的最高等级报 🔴，其余 🟡（避免历史 L4 任务长期噪音）
            level_flag = "🔴" if level == max_level else "🟡"
            add(issues, "REVIEWER_REDUNDANCY", "board-contract", level_flag, msg)
    return issues


def validate_stale_task_detection(board_root: Path):
    """任务过时/被替代检测（2026-08-12 治理新增，P1-1）。

    实证：T19（v3 协议已内置修复）、T49（分诊链已上线）、V21-01（被后续任务
    替代）空挂 2-4 天无人发现，全靠人工清理。本检查用轻量启发式标记疑似过时：

    - open/claimed 且创建超 7 天，从未被认领（无 claim 事件）→ 疑似僵尸任务
    - 卡片正文含「修复 X」「补 X 缺口」类措辞，且引用物已不存在/协议已覆盖 → 提示人工复核
    仅输出 ⚪/🟡 建议，不自动改状态（用户/值班端据此决策 cancel 或 supersede）。
    """
    issues = []
    tasks_root = board_root / "tasks"
    if not tasks_root.exists():
        return issues
    terminal = {"done", "cancelled", "superseded", "archived"}
    active = {"open", "claimed"}
    now = datetime.now(CN_TZ)
    for task_dir in sorted(tasks_root.iterdir()):
        if not is_physical_task_dir(task_dir, tasks_root):
            continue
        status_path = task_dir / "status.json"
        card_path = task_dir / "card.md"
        if not status_path.exists():
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if status.get("status") not in active:
            continue
        created = parse_iso(status.get("created_at")) or parse_iso(status.get("created"))
        if created is None:
            continue
        age_days = (now - created).total_seconds() / 86400
        if age_days < 7:
            continue
        task_id = status.get("id", task_dir.name)
        # 从未认领（executor 为空 + 无 claim 痕迹）
        never_claimed = not status.get("executor") and not status.get("claimed_at")
        if never_claimed:
            add(issues, "STALE_TASK", task_id, "🟡",
                f"任务创建 {age_days:.0f} 天仍 open 且从未认领，疑似僵尸/过时任务，"
                "建议人工复核是否 cancel 或 supersede")
            continue
        # 已认领但长期无进展（claimed 超 7 天）
        claimed_at = parse_iso(status.get("claimed_at"))
        if claimed_at and (now - claimed_at).total_seconds() / 86400 > 7:
            add(issues, "STALE_TASK", task_id, "🟡",
                f"任务认领 { (now - claimed_at).total_seconds()/86400:.0f} 天无进展"
                f"（executor={status.get('executor')}），疑似执行端失联，"
                "建议 requeue 或换端")
    return issues


def validate_work_identity(board_root: Path):
    """Cross-task uniqueness and lineage checks for explicit vNext work identity."""
    issues = []
    tasks_root = board_root / "tasks"
    by_id = {}
    active_by_key = {}
    terminal = {"done", "cancelled", "superseded", "archived"}
    for task_dir in sorted(tasks_root.iterdir()):
        if not is_physical_task_dir(task_dir, tasks_root):
            continue
        try:
            status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        by_id[task_dir.name] = status
        work_key = status.get("work_key")
        if not work_key:
            continue
        if not is_safe_token(work_key):
            add(issues, "WORK_KEY_INVALID", task_dir.name, "🔴", f"work_key 非法: {work_key!r}")
        root = status.get("root_work_id")
        if not validate_task_id(root):
            add(issues, "ROOT_WORK_ID_INVALID", task_dir.name, "🔴", f"root_work_id 非法: {root!r}")
        if status.get("status") not in terminal:
            active_by_key.setdefault(work_key, []).append(task_dir.name)

    for work_key, task_ids in sorted(active_by_key.items()):
        if len(task_ids) > 1:
            for task_id in task_ids:
                add(
                    issues,
                    "ACTIVE_WORK_DUPLICATE",
                    task_id,
                    "🔴",
                    f"active work_key={work_key!r} 同时出现在 {task_ids}",
                )

    for task_id, status in by_id.items():
        relation = next(
            (
                (field, status.get(field))
                for field in ("continuation_of", "follow_up_of", "supersedes")
                if status.get(field)
            ),
            None,
        )
        if not relation:
            continue
        field, parent_id = relation
        parent = by_id.get(parent_id)
        if parent is None:
            add(issues, "WORK_LINEAGE_MISSING", task_id, "🔴", f"{field} parent 不存在: {parent_id}")
            continue
        if status.get("root_work_id") != parent.get("root_work_id"):
            add(issues, "WORK_ROOT_DIVERGED", task_id, "🔴", f"{field} 与 parent 的 root_work_id 不一致")
        if field == "continuation_of" and status.get("work_key") != parent.get("work_key"):
            add(issues, "WORK_CONTINUATION_DIVERGED", task_id, "🔴", "continuation 未复用 parent work_key")
        if field == "follow_up_of" and status.get("work_key") == parent.get("work_key"):
            add(issues, "WORK_FOLLOWUP_DUPLICATE", task_id, "🔴", "follow-up 必须是新的独立 work_key")

    return issues


def main():
    parser = argparse.ArgumentParser(description="公告牌当前态不变量校验 v2.0")
    parser.add_argument("--board-root", default=str(Path(__file__).parent), help="board 根目录")
    parser.add_argument("--fix", action="store_true", help="只修复缺失 card_hash")
    parser.add_argument("--audit-legacy", action="store_true", help="对旧版 done 任务也执行完整语义审计")
    args = parser.parse_args()

    board_root = Path(args.board_root).resolve()
    tasks_root = board_root / "tasks"
    if not tasks_root.exists():
        print(f"❌ tasks 目录不存在: {tasks_root}", file=sys.stderr)
        return 2

    if args.fix:
        fixed = fix_missing_hashes(board_root)
        if fixed:
            print(f"🔧 已补充 {fixed} 个 card_hash\n")

    issues = []
    for task_dir in sorted(tasks_root.iterdir()):
        if task_dir.name.startswith((".", "_")):
            continue
        if not is_physical_task_dir(task_dir, tasks_root):
            issues.extend(validate_one(task_dir, audit_legacy=args.audit_legacy))
            continue
        status_path = task_dir / "status.json"
        if status_path.exists():
            issues.extend(validate_one(task_dir, audit_legacy=args.audit_legacy))

    # 跨任务版本链一致性检查
    issues.extend(validate_version_coherence(board_root))

    # v1.3: 家族一致性检查（检查点7）
    issues.extend(validate_family_coherence(board_root))

    # vNext: stable work identity uniqueness + lineage
    issues.extend(validate_work_identity(board_root))

    # 2026-08-12 治理：验收通道冗余检查（P0-1）
    issues.extend(validate_reviewer_redundancy(board_root))

    # 2026-08-12 治理：任务过时/被替代检测（P1-1）
    issues.extend(validate_stale_task_detection(board_root))

    if not issues:
        print("✅ 公告牌不变量校验通过：无问题")
        return 0

    order = {"🔴": 0, "🟡": 1, "⚪": 2}
    issues.sort(key=lambda item: (order.get(item["level"], 9), item["category"], item["task_id"]))
    counts = {level: sum(item["level"] == level for item in issues) for level in order}
    print(
        f"📋 公告牌不变量校验发现 {len(issues)} 个问题"
        f"（🔴 {counts['🔴']} / 🟡 {counts['🟡']} / ⚪ {counts['⚪']}）:\n"
    )
    category = None
    for issue in issues:
        if issue["category"] != category:
            category = issue["category"]
            print(f"\n  ## {category}")
        print(f"  {issue['level']} [{issue['task_id']}] {issue['message']}")
    print("\nℹ️ 当前态与事件历史分开验证；full/partial/untraceable 不会互相冒充。")
    return 1 if counts["🔴"] or counts["🟡"] else 0


if __name__ == "__main__":
    sys.exit(main())
