#!/usr/bin/env python3
"""board-alert — 公告牌告警检测脚本（v1.4）。

用法: python3 board-alert.py [--board-root <path>] [--heartbeat-stale-hours 2]
退出码: 0=成功（无论是否有告警）; 1=错误

基础检测项:
  1. DLQ 任务 → 需人工裁决
  2. open 超 7 天 → 无人认领
  3. blocked 超 24h → 即将自动解除
  4. claimed 租约即将过期（1h 内）→ 执行端可能卡住
  5. 心跳过期 → 某端可能离线
  6. awaiting_review 超 2h → 🔴 REVIEW_OVERDUE（巡查端应主动验收）
     超 24h 额外追加 🟡 REVIEW_BACKLOG（长期积压统计）

v1.1 新增检测项:
  7. awaiting_user_approval → 治理审批待办（🔴 GOVERNANCE_APPROVAL）
     含 task_id、card.md 中提及的输出文件路径、reviewer、等待时长
  8. awaiting_review 数量连续 3 代 board-index.json 递增 → 验收积压趋势（🟡 REVIEW_BACKLOG_TREND）
     通过 .kb/board/_meta/review-trend.json 滚动追踪（按 generation 去重）
  9. 同执行端同复杂度连续 2 次 FAIL（review_result=fail）→ 执行端重复失败（🟡 EXECUTOR_REPEATED_FAIL）
  10. 扫描 done 任务 REVIEW.md → 构建端置信度表
      规则: 连续 2 PASS=🟢 high / 1 FAIL=🟡 medium / 连续 2 FAIL=🔴 low
      输出到 ALERTS.md「## 端置信度（自动维护）」段

v1.4 新增检测项:
  11. done 任务缺 REVIEW.md → 🟡 MISSING_REVIEW
      排除 fixture 类任务（note 含 "fixture"）
      原因: 28 done 中仅 2 个有 REVIEW.md（7.1%），置信度追踪形同虚设

v1.5 新增（T49 预警分诊链）:
  12. ALERTS.md 头部输出「红告警指纹」行 = md5(排序后的 红告警 category|task_id 集合)；
      无红告警时为 none。供哨兵 alert_triage 触发做确定性去重。

输出:
  - stdout: 告警摘要
  - .kb/board/ALERTS.md: 完整告警报告 + 端置信度表（每次覆盖写入）
  - .kb/board/_meta/review-trend.json: 验收积压趋势状态文件（滚动窗口）
  - 无告警时写 "✅ 所有正常" 到 ALERTS.md（仍附端置信度表）

依赖: 仅 Python3 标准库（json/re/argparse/sys/datetime/pathlib）。
REVIEW.md 解析使用简单文本匹配（"验收结果:" 行 + "## 验收方" 段落），不依赖 yaml。
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from board_contract import (
    AGENT_CONTRACTS,
    NO_ELIGIBLE_INDEPENDENT_REVIEWER,
    canonical_agent,
    eligible_reviewers,
)
from board_transition import review_authorization_is_event_backed
from board_verdicts import (
    LEGACY_REVIEW_RESULTS,
    REVIEW_RESULTS,
    parse_legacy_verdict,
)

CN_TZ = timezone(timedelta(hours=8))
TREND_WINDOW = 20  # review-trend.json 滚动保留最近 N 代

RESULT_PASS, RESULT_FAIL = REVIEW_RESULTS
LEGACY_RESULT_CONDITIONAL = LEGACY_REVIEW_RESULTS[0]


# ----------------------------------------------------------------------------
# 时间工具
# ----------------------------------------------------------------------------

def parse_iso(ts_str):
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CN_TZ)
        return dt
    except (ValueError, TypeError):
        return None


def now_cn():
    return datetime.now(CN_TZ)


def _sort_time_key(value):
    """返回可比较的 tz-aware datetime（None 时用极小值，保证排在最前）。"""
    if value is None:
        return datetime.min.replace(tzinfo=CN_TZ)
    return value


def _norm_result(val):
    """归一化已落盘结果；旧结果仅供历史统计兼容。"""
    if not val:
        return None
    v = str(val).strip().lower()
    if v in REVIEW_RESULTS or v in LEGACY_REVIEW_RESULTS:
        return v
    return None


# ----------------------------------------------------------------------------
# REVIEW.md 解析（简单文本匹配，不依赖 yaml）
# ----------------------------------------------------------------------------

def parse_review_md(path):
    """解析 REVIEW.md，返回 dict(result/reviewer/review_time/review_round) 或 None。

    匹配规则:
    - 当前契约只识别 PASS/FAIL；历史记录的旧结果只读兼容
    - "## 验收方" 段落内的 "- 端:"/"- 时间:"/"- 验收轮次:" 行
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    result = parse_legacy_verdict(text)
    reviewer = None
    review_time = None
    review_round = None
    in_reviewer_section = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("##"):
            in_reviewer_section = line.startswith("## 验收方")
        if in_reviewer_section:
            if line.startswith("- 端:") or (line.startswith("端:") and not line.startswith("端: ")):
                reviewer = line.split(":", 1)[1].strip()
            elif line.startswith("- 时间:") or line.startswith("时间:"):
                ts_raw = line.split(":", 1)[1].strip().strip("<>").strip()
                review_time = parse_iso(ts_raw)
            elif line.startswith("- 验收轮次:") or line.startswith("验收轮次:"):
                nums = re.findall(r"\d+", line.split(":", 1)[1])
                if nums:
                    review_round = int(nums[0])

    if result is None:
        return None
    return {
        "result": result,
        "reviewer": reviewer,
        "review_time": review_time,
        "review_round": review_round,
    }


# ----------------------------------------------------------------------------
# card.md 输出路径提取
# ----------------------------------------------------------------------------

def extract_output_paths(card_text):
    """从 card.md 提取输出文件路径。

    策略（任一命中即收录，去重保序）:
    1. 反引号包裹且像路径的串（含 "output" 或以代码/数据扩展名结尾）
    2. 裸 output/... 或 tasks/<id>/output/... 引用
    """
    paths = []
    seen = set()
    ext_re = re.compile(r"\.(py|json|txt|sh|ya?ml|html?|csv|ts|js)$", re.IGNORECASE)

    # 1. 反引号包裹
    for m in re.finditer(r"`([^`]+)`", card_text):
        p = m.group(1).strip()
        if "/" not in p:
            continue
        if ("output" in p.lower()) or ext_re.search(p):
            if p not in seen:
                seen.add(p)
                paths.append(p)

    # 2. 裸 output/ 引用
    for m in re.finditer(r"(?<![\w/])(?:tasks/[^/\s`]+/)?output/[\w./\-]+", card_text):
        p = m.group(0)
        if p not in seen:
            seen.add(p)
            paths.append(p)

    return paths


# ----------------------------------------------------------------------------
# 端置信度表
# ----------------------------------------------------------------------------

def confidence_from_results(results):
    """根据有序验收结果序列（旧→新）判定置信度。

    返回 (emoji, label, reason) 或 None。
    规则:
      - 连续 2 PASS → 🟢 high
      - 连续 2 FAIL → 🔴 low
      - 最近 1 FAIL → 🟡 medium
      - 最近为历史旧结果 → 🟡 medium
      - PASS 但前序未通过（恢复中）→ 🟡 medium
      - 仅 1 次 PASS / 无法判定 → ⚪ insufficient
    """
    if not results:
        return None
    last = results[-1]
    if len(results) >= 2:
        prev = results[-2]
        if prev == RESULT_PASS and last == RESULT_PASS:
            return ("🟢", "high", "连续 2 次 PASS")
        if prev == RESULT_FAIL and last == RESULT_FAIL:
            return ("🔴", "low", "连续 2 次 FAIL")
    if last == RESULT_FAIL:
        return ("🟡", "medium", "最近 1 次 FAIL")
    if last == LEGACY_RESULT_CONDITIONAL:
        return ("🟡", "medium", "最近为历史旧结果")
    if last == RESULT_PASS:
        if len(results) == 1:
            return ("⚪", "insufficient", "仅 1 次 PASS，样本不足")
        return ("🟡", "medium", "PASS（前序未通过，恢复中）")
    return ("⚪", "insufficient", "样本不足")


def build_confidence_table(review_records):
    """review_records: done 任务的 REVIEW.md 记录列表。
    返回按 (executor, complexity) 分组、按时间排序后的置信度行。
    """
    groups = {}
    for r in review_records:
        executor = canonical_agent(str(r.get("executor") or "").split("/", 1)[0])
        complexity = r.get("complexity") or "L1"
        if not executor:
            continue
        groups.setdefault((executor, complexity), []).append(r)

    rows = []
    for (executor, complexity), recs in sorted(groups.items()):
        recs.sort(key=lambda r: (_sort_time_key(r.get("review_time")), r.get("task_id", "")))
        seq = [r["result"] for r in recs if r.get("result")]
        conf = confidence_from_results(seq)
        if not conf:
            continue
        emoji, label, reason = conf
        seq_str = "→".join(s.upper() for s in seq) if seq else "-"
        rows.append({
            "executor": executor,
            "complexity": complexity,
            "emoji": emoji,
            "label": label,
            "sequence": seq_str,
            "sample_count": len(seq),
            "reason": reason,
            "task_ids": [r.get("task_id") for r in recs],
        })
    return rows


def render_confidence_section(conf_rows):
    """渲染「## 端置信度（自动维护）」段。"""
    if not conf_rows:
        return (
            "## 端置信度（自动维护）\n\n"
            "暂无验收数据（未发现 done 任务的 REVIEW.md）。\n"
        )
    lines = [
        "## 端置信度（自动维护）",
        "",
        "| 执行端 | 复杂度 | 置信度 | 最近结果序列 | 样本数 | 判定依据 |",
        "|--------|--------|--------|--------------|--------|----------|",
    ]
    for row in conf_rows:
        lines.append(
            f"| {AGENT_CONTRACTS[row['executor']]['display_name']} | {row['complexity']} | "
            f"{row['emoji']} {row['label']} | {row['sequence']} | "
            f"{row['sample_count']} | {row['reason']} |"
        )
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# 执行端重复失败检测
# ----------------------------------------------------------------------------

def detect_executor_repeated_fail(task_records):
    """检测同执行端同复杂度连续 2 次 FAIL。

    task_records: 全量任务记录（含 review_result/review_round/review_time）。
    触发条件（满足其一）:
      A. 单任务 review_result=fail 且 review_round >= 2（同任务连续多轮 FAIL）
      B. 同 (executor, complexity) 下按时间排序最近 2 个任务均 FAIL
    返回告警列表。
    """
    groups = {}
    for t in task_records:
        executor = canonical_agent(str(t.get("executor") or "").split("/", 1)[0])
        complexity = t.get("complexity") or "L1"
        rr = t.get("review_result")
        if not executor or rr != RESULT_FAIL:
            continue
        groups.setdefault((executor, complexity), []).append(t)

    alerts = []
    for (executor, complexity), recs in sorted(groups.items()):
        trigger = False
        failing_ids = []

        # 条件 A：单任务连续多轮 FAIL
        for r in recs:
            rnd = r.get("review_round") or 1
            if rnd >= 2:
                trigger = True
                if r["task_id"] not in failing_ids:
                    failing_ids.append(r["task_id"])

        # 条件 B：最近 2 个不同任务均 FAIL
        if len(recs) >= 2:
            ordered = sorted(
                recs,
                key=lambda r: (_sort_time_key(r.get("review_time")), r.get("task_id", "")),
            )
            last_two = ordered[-2:]
            if all(x.get("review_result") == RESULT_FAIL for x in last_two):
                trigger = True
                for x in last_two:
                    if x["task_id"] not in failing_ids:
                        failing_ids.append(x["task_id"])

        if trigger:
            ids_str = ", ".join(failing_ids) if failing_ids else "N/A"
            alerts.append({
                "level": "🟡",
                "category": "EXECUTOR_REPEATED_FAIL",
                "task_id": ",".join(failing_ids) if failing_ids else executor,
                "message": (
                    f"执行端 {executor} 在 {complexity} 复杂度连续 FAIL"
                    f"（任务: {ids_str}），建议关注执行质量或任务拆分"
                ),
            })
    return alerts


# ----------------------------------------------------------------------------
# 验收积压趋势检测
# ----------------------------------------------------------------------------

def read_index_awaiting(board_root):
    """读取 board-index.json，返回 (generated, awaiting_review_count) 或 (None, None)。"""
    idx_path = board_root / "board-index.json"
    if not idx_path.exists():
        return None, None
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, None
    generated = data.get("generated")
    tasks = data.get("tasks", [])
    count = sum(1 for t in tasks if t.get("status") == "awaiting_review")
    return generated, count


def detect_review_backlog_trend(trend_path, generated, awaiting_count, *, persist=False):
    """更新 review-trend.json 并检测连续 3 代递增。

    按 generation（board-index.json 的 generated 时间戳）去重，滚动保留最近 TREND_WINDOW 代。
    返回 (alert_dict_or_None, entries)。
    """
    entries = []
    if trend_path.exists():
        try:
            data = json.loads(trend_path.read_text(encoding="utf-8"))
            entries = data.get("entries", [])
        except (json.JSONDecodeError, OSError):
            entries = []

    # 同一 generation 去重（重复运行不产生新代）
    entries = [e for e in entries if e.get("generated") != generated]
    entries.append({
        "generated": generated,
        "awaiting_review_count": awaiting_count,
        "recorded_at": now_cn().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    })
    entries = entries[-TREND_WINDOW:]

    # 持久化（_meta 目录自动创建）
    if persist:
        try:
            trend_path.parent.mkdir(parents=True, exist_ok=True)
            trend_path.write_text(
                json.dumps(
                    {"entries": entries, "updated": now_cn().strftime("%Y-%m-%dT%H:%M:%S+08:00")},
                    ensure_ascii=False, indent=2,
                ) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    alert = None
    if len(entries) >= 3:
        last3 = entries[-3:]
        c = [e.get("awaiting_review_count", 0) for e in last3]
        if c[0] < c[1] < c[2]:
            alert = {
                "level": "🟡",
                "category": "REVIEW_BACKLOG_TREND",
                "task_id": "board:trend",
                "message": (
                    f"awaiting_review 数量连续 3 代递增（{c[0]}→{c[1]}→{c[2]}），"
                    f"验收积压趋势，建议增派验收端"
                ),
            }
    return alert, entries


# ----------------------------------------------------------------------------
# 主检测逻辑
# ----------------------------------------------------------------------------

def check_alerts(board_root, heartbeat_stale_hours=2, *, apply=False):
    now = now_cn()
    alerts = []
    task_records = []        # 全量任务记录（重复失败分析用）
    review_records_done = []  # done 任务的 REVIEW.md 记录（置信度表用）

    # 1. 扫描任务
    tasks_root = board_root / "tasks"
    if tasks_root.exists():
        for task_dir in sorted(tasks_root.iterdir()):
            if not task_dir.is_dir() or task_dir.name.startswith((".", "_")):
                continue
            status_path = task_dir / "status.json"
            if not status_path.exists():
                continue
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            task_id = status.get("id", task_dir.name)
            task_status = status.get("status", "open")
            complexity = status.get("complexity", "L1")
            executor = status.get("executor")
            reviewer = status.get("reviewer")
            created = parse_iso(status.get("created"))
            claimed_at = parse_iso(status.get("claimed_at"))
            lease_expires = parse_iso(status.get("lease_expires_at"))
            completed_at = parse_iso(status.get("completed_at"))
            requeue_count = status.get("requeue_count", 0)
            review_result = _norm_result(status.get("review_result"))
            review_round = status.get("review_round")

            # 解析 REVIEW.md（若存在），补充 review_result/review_round/review_time
            review_md = task_dir / "REVIEW.md"
            parsed_review = parse_review_md(review_md) if review_md.exists() else None
            if parsed_review:
                if review_result is None:
                    review_result = parsed_review["result"]
                if review_round is None:
                    review_round = parsed_review["review_round"]
                review_time = parsed_review["review_time"] or completed_at or claimed_at
            else:
                review_time = completed_at or claimed_at

            # 收集任务记录（执行端重复失败分析用）
            task_records.append({
                "task_id": task_id,
                "executor": executor,
                "complexity": complexity,
                "review_result": review_result,
                "review_round": review_round,
                "review_time": review_time,
                "status": task_status,
            })

            # done 任务且有 REVIEW.md → 置信度表数据源
            if task_status == "done" and parsed_review:
                review_records_done.append({
                    "executor": executor,
                    "complexity": complexity,
                    "result": parsed_review["result"],
                    "review_time": parsed_review["review_time"] or completed_at or claimed_at,
                    "task_id": task_id,
                })

            # --- v1.4 新增告警 ---

            # done 任务缺 REVIEW.md（MISSING_REVIEW）
            if task_status == "done" and not parsed_review:
                # 排除 fixture 类任务（note 含 "fixture"）
                note = status.get("note", "")
                if "fixture" not in note.lower():
                    alerts.append({
                        "level": "🟡",
                        "category": "MISSING_REVIEW",
                        "task_id": task_id,
                        "message": (
                            f"任务已完成但缺 REVIEW.md（executor={executor or 'N/A'}），"
                            f"验收过程无记录可查——v1.4 起 REVIEW.md 为必填"
                        )
                    })

            # --- 基础告警 ---

            # DLQ 告警
            if task_status == "dlq":
                alerts.append({
                    "level": "🔴",
                    "category": "DLQ",
                    "task_id": task_id,
                    "message": f"任务在死信队列（requeue_count={requeue_count}），需人工裁决"
                })

            # open 超 7 天
            if task_status == "open" and created:
                age = now - created
                if age > timedelta(days=7):
                    alerts.append({
                        "level": "🟡",
                        "category": "STALE_OPEN",
                        "task_id": task_id,
                        "message": f"任务 open 已 {age.days} 天无人认领"
                    })

            # blocked 超 24h
            if task_status == "blocked" and claimed_at:
                age = now - claimed_at
                if age > timedelta(hours=24):
                    alerts.append({
                        "level": "🟡",
                        "category": "BLOCKED_TIMEOUT",
                        "task_id": task_id,
                        "message": f"任务 blocked 已 {age.total_seconds()/3600:.1f}h，即将自动解除"
                    })

            # claimed 租约即将过期（1h 内）
            if task_status == "claimed" and lease_expires:
                remaining = lease_expires - now
                if remaining < timedelta(hours=1) and remaining > timedelta(0):
                    alerts.append({
                        "level": "🟡",
                        "category": "LEASE_EXPIRING",
                        "task_id": task_id,
                        "message": f"租约将在 {remaining.total_seconds()/60:.0f} 分钟后过期，执行端可能卡住"
                    })

            # awaiting_review 超 2h → 🔴 REVIEW_OVERDUE（主动验收机制兜底）
            # 超 24h 额外追加 🟡 REVIEW_BACKLOG（长期积压统计）
            if task_status == "awaiting_review" and claimed_at:
                age = now - claimed_at
                age_h = age.total_seconds() / 3600
                if age_h > 2:
                    try:
                        card_text = (task_dir / "card.md").read_text(encoding="utf-8")
                    except OSError:
                        card_text = ""
                    routes = eligible_reviewers(
                        status,
                        executor,
                        executor_model=status.get("executor_model"),
                        card_text=card_text,
                        include_authorization=review_authorization_is_event_backed(
                            task_dir, status
                        ),
                    )
                    alerts.append({
                        "level": "🔴",
                        "category": (
                            NO_ELIGIBLE_INDEPENDENT_REVIEWER
                            if not routes
                            else "REVIEW_OVERDUE"
                        ),
                        "task_id": task_id,
                        "message": (
                            f"任务等待验收已 {age_h:.1f}h（executor={executor or 'N/A'}），"
                            + (
                                "没有合法 independent reviewer path；需用户正式 authorize-reviewer"
                                if not routes
                                else "超过 2h 阈值——请从 eligible reviewer pool 主动验收"
                            )
                        )
                    })
                if age_h > 24:
                    alerts.append({
                        "level": "🟡",
                        "category": "REVIEW_BACKLOG",
                        "task_id": task_id,
                        "message": f"任务等待验收已 {age_h:.1f}h，长期验收积压"
                    })

            # --- v1.1 新增告警 ---

            # 治理审批待办
            if task_status == "awaiting_user_approval":
                output_paths = []
                card_path = task_dir / "card.md"
                if card_path.exists():
                    try:
                        output_paths = extract_output_paths(
                            card_path.read_text(encoding="utf-8")
                        )
                    except OSError:
                        output_paths = []
                # 等待时长：以完成（验收通过转入待审批）时刻为起点
                wait_start = completed_at or claimed_at or created
                wait_str = "未知"
                if wait_start:
                    wait_dur = now - wait_start
                    wait_str = f"{wait_dur.total_seconds()/3600:.1f}h"
                path_str = ", ".join(output_paths) if output_paths else "（card.md 未提及）"
                alerts.append({
                    "level": "🔴",
                    "category": "GOVERNANCE_APPROVAL",
                    "task_id": task_id,
                    "message": (
                        f"治理审批待办（reviewer={reviewer or 'N/A'}, "
                        f"等待 {wait_str}）输出: {path_str}"
                    )
                })

    # 2. 扫描心跳
    heartbeat_root = board_root / "experimental"
    if heartbeat_root.exists():
        for hb_file in heartbeat_root.glob("*-heartbeat.md"):
            try:
                content = hb_file.read_text(encoding="utf-8")
                # 提取时间戳 (格式: 时间=2026-08-08T00:33:55+0800)
                for line in content.split("\n"):
                    if "时间=" in line:
                        ts_str = line.split("时间=")[1].split("|")[0].strip()
                        hb_time = parse_iso(ts_str)
                        if hb_time:
                            age = now - hb_time
                            if age > timedelta(hours=heartbeat_stale_hours):
                                agent_name = hb_file.stem.replace("-heartbeat", "")
                                alerts.append({
                                    "level": "🔴",
                                    "category": "HEARTBEAT_STALE",
                                    "task_id": f"heartbeat:{agent_name}",
                                    "message": f"{agent_name} 心跳已过期 {age.total_seconds()/3600:.1f}h（最后: {ts_str}），端可能离线"
                                })
                        break
            except (OSError, IndexError):
                continue

    # 3. v1.1: 执行端重复失败
    alerts.extend(detect_executor_repeated_fail(task_records))

    # 4. v1.1: 验收积压趋势（依赖 board-index.json 的 generation）
    trend_path = board_root / "_meta" / "review-trend.json"
    generated, awaiting_count = read_index_awaiting(board_root)
    if generated is not None:
        trend_alert, _ = detect_review_backlog_trend(
            trend_path, generated, awaiting_count, persist=apply
        )
        if trend_alert:
            alerts.append(trend_alert)
    # 无 board-index.json 时跳过趋势检测（无法区分 generation）

    # 5. 端置信度表
    conf_rows = build_confidence_table(review_records_done)

    # 6. 输出报告
    return _write_report(board_root, alerts, conf_rows, now, apply=apply)


def red_fingerprint(alerts):
    """红告警指纹（v1.5/T49）：md5(排序后的 'category|task_id' 集合)；无红告警返回 'none'。
    供哨兵 alert_triage 触发做确定性去重——指纹不变不重复拉起分诊会话。"""
    reds = sorted(f"{a['category']}|{a['task_id']}"
                  for a in alerts if a.get("level") == "🔴")
    if not reds:
        return "none"
    return hashlib.md5("\n".join(reds).encode("utf-8")).hexdigest()


def _write_report(board_root, alerts, conf_rows, now, *, apply=False):
    """写 ALERTS.md 并打印摘要。"""
    alert_path = board_root / "ALERTS.md"
    now_str = now.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    conf_section = render_confidence_section(conf_rows)
    fp = red_fingerprint(alerts)

    # 按级别排序
    level_order = {"🔴": 0, "🟡": 1}
    alerts.sort(key=lambda a: (level_order.get(a["level"], 9), a["category"]))

    red = sum(1 for a in alerts if a["level"] == "🔴")
    yellow = sum(1 for a in alerts if a["level"] == "🟡")

    if not alerts:
        report_lines = [
            "# 公告牌告警报告",
            "",
            f"生成时间: {now_str}",
            "红告警指纹: none",
            "",
            "✅ 所有正常，无告警。",
            "",
            conf_section,
        ]
        if apply:
            alert_path.write_text("\n".join(report_lines), encoding="utf-8")
        print("✅ 所有正常，无告警")
        if conf_rows:
            print(f"📊 端置信度表已更新（{len(conf_rows)} 行）")
        print(f"\n{'完整报告' if apply else 'readonly 报告目标（未写）'}: {alert_path}")
        return 0

    lines = [
        "# 公告牌告警报告",
        "",
        f"生成时间: {now_str}",
        f"告警总数: {len(alerts)}（🔴 {red} / 🟡 {yellow}）",
        f"红告警指纹: {fp}",
        "",
    ]
    current_category = None
    for a in alerts:
        if a["category"] != current_category:
            lines.append(f"## {a['category']}")
            current_category = a["category"]
        lines.append(f"- {a['level']} [{a['task_id']}] {a['message']}")
    lines.append("")
    lines.append(conf_section)

    if apply:
        alert_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"📋 发现 {len(alerts)} 个告警（🔴 {red} / 🟡 {yellow}）:")
    for a in alerts:
        print(f"  {a['level']} [{a['task_id']}] {a['message']}")
    if conf_rows:
        print(f"📊 端置信度表已更新（{len(conf_rows)} 行）")
    print(f"\n{'完整报告' if apply else 'readonly 报告目标（未写）'}: {alert_path}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="公告牌告警检测（v1.1）")
    parser.add_argument(
        "--board-root",
        default=str(Path(__file__).parent),
        help="board 根目录（默认: 脚本所在目录）",
    )
    parser.add_argument(
        "--heartbeat-stale-hours",
        type=int,
        default=2,
        help="心跳过期阈值（小时）",
    )
    parser.add_argument("--mode", choices=["readonly", "apply"], default="readonly")
    args = parser.parse_args()

    board_root = Path(args.board_root).resolve()
    return check_alerts(board_root, args.heartbeat_stale_hours, apply=args.mode == "apply")


if __name__ == "__main__":
    sys.exit(main())
