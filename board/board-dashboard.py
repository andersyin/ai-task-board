#!/usr/bin/env python3
"""board-dashboard — 公告牌系统健康监控仪表盘。

用法: python3 board-dashboard.py [--board-root <path>]
退出码: 0=成功; 1=错误

扫描 board 目录并生成 MONITOR.md，包含：
  - System Health（任务状态统计 + 健康指示灯）
  - Agent Health（心跳 + 认领/验收计数）
  - Project Progress（项目维度进度）
  - Agent Confidence（验收置信度，如有 REVIEW.md）
  - Recent Alerts Summary（最近 3 条告警）
  - Generation Info（生成时间与工具）
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from board_contract import AGENT_CONTRACTS, canonical_agent
from board_verdicts import LEGACY_REVIEW_RESULTS, parse_legacy_verdict

CN_TZ = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:?\d{2}|Z)?"
)


def now_cn():
    """当前 CN 时区时间。"""
    return datetime.now(CN_TZ)


def parse_iso(ts_str):
    """解析 ISO 8601 时间字符串，兼容 +0800 和 +08:00 两种时区格式。"""
    if not ts_str:
        return None
    ts_str = str(ts_str).strip()
    if not ts_str:
        return None
    # Python <3.11 的 fromisoformat 不接受 +0800，统一加冒号
    ts_str = re.sub(r"([+-])(\d{2})(\d{2})$", r"\1\2:\3", ts_str)
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CN_TZ)
        return dt
    except (ValueError, TypeError):
        return None


def extract_timestamps(text):
    """从文本中提取所有 ISO 格式时间戳。"""
    return _TS_RE.findall(text)


def latest_timestamp(text):
    """返回文本中最新的时间戳，无则 None。"""
    best = None
    for raw in extract_timestamps(text):
        dt = parse_iso(raw)
        if dt and (best is None or dt > best):
            best = dt
    return best


def fmt_dt(dt):
    """格式化时间用于表格展示。"""
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")


def agent_key(name):
    """把显示名或“端/模型”身份归一为注册端 key。"""
    raw = (name or "").strip().split("/", 1)[0]
    return canonical_agent(raw) or raw.lower()


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_tasks(board_root):
    """加载所有任务的 status.json。返回 [(task_dir, status_dict), ...]。"""
    tasks = []
    tasks_root = board_root / "tasks"
    if not tasks_root.is_dir():
        return tasks
    for task_dir in sorted(tasks_root.iterdir()):
        if not task_dir.is_dir() or task_dir.name.startswith((".", "_")):
            continue
        status_path = task_dir / "status.json"
        if not status_path.exists():
            continue
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        tasks.append((task_dir, data))
    return tasks


def load_heartbeats(board_root):
    """加载心跳文件。返回 {agent_key: (display_name, latest_dt)}。

    兼容多种心跳格式：
      - Trae:      ## 2026-08-08T01:30:33+08:00 (markdown 标题)
      - Antigravity: 时间=2026-08-08T01:01:51+0800 | ... (内联)
      - WorkBuddy:   时间=... (多行，取最新)
      - QwenWork:    2026-08-08T00:20:19+08:00 ... (行首)
    """
    result = {}
    hb_root = board_root / "experimental"
    if not hb_root.is_dir():
        return result
    for hb_file in sorted(hb_root.glob("*-heartbeat.md")):
        display_name = hb_file.stem.replace("-heartbeat", "")
        key = agent_key(display_name)
        try:
            content = hb_file.read_text(encoding="utf-8")
        except OSError:
            content = ""
        dt = latest_timestamp(content)
        result[key] = (display_name, dt)
    return result


def load_reviews(board_root, tasks):
    """加载 tasks/*/REVIEW.md，返回 review 记录列表。

    每条记录包含: agent, agent_key, result, complexity, time, task_id。
    """
    reviews = []
    for task_dir, status in tasks:
        review_path = task_dir / "REVIEW.md"
        if not review_path.exists():
            continue
        try:
            content = review_path.read_text(encoding="utf-8")
        except OSError:
            continue

        # 新契约只有 PASS/FAIL；旧结果只用于历史仪表盘兼容。
        parsed_result = parse_legacy_verdict(content)
        result = parsed_result.upper() if parsed_result else None

        # 解析验收端: 从 "端:" 或 "端：" 字段提取
        reviewer = None
        for line in content.split("\n"):
            if "端:" not in line and "端：" not in line:
                continue
            parts = re.split(r"端[：:]", line, maxsplit=1)
            if len(parts) < 2:
                continue
            reviewer = parts[1].strip()
            # 去除 markdown 列表标记 (如 "- 端: xxx" 残留的 "- ")
            reviewer = re.sub(r"^[-*]\s*", "", reviewer).strip()
            break

        # 回退到 status.json 的 reviewer 字段
        if not reviewer:
            reviewer = status.get("reviewer")

        if not reviewer or not result:
            continue

        complexity = status.get("complexity", "L1")

        # 时间优先取 REVIEW.md 内时间戳，其次 status.json completed_at，最后文件 mtime
        review_time = latest_timestamp(content)
        if not review_time:
            review_time = parse_iso(status.get("completed_at"))
        if not review_time:
            try:
                review_time = datetime.fromtimestamp(
                    review_path.stat().st_mtime, tz=CN_TZ
                )
            except OSError:
                review_time = None

        reviewer_key = agent_key(reviewer)
        if reviewer_key not in AGENT_CONTRACTS:
            continue
        reviews.append({
            "agent": reviewer,
            "agent_key": reviewer_key,
            "result": result,
            "complexity": complexity,
            "time": review_time,
            "task_id": status.get("id", task_dir.name),
        })
    return reviews


def load_alerts(board_root, max_alerts=3):
    """读取 ALERTS.md 最后 max_alerts 条告警。无文件返回 None。"""
    alerts_path = board_root / "ALERTS.md"
    if not alerts_path.exists():
        return None
    try:
        content = alerts_path.read_text(encoding="utf-8")
    except OSError:
        return None

    if "✅" in content and "无告警" in content:
        return ["✅ 所有正常，无告警"]

    alert_lines = []
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- ") and ("🔴" in stripped or "🟡" in stripped):
            alert_lines.append(stripped)

    if not alert_lines:
        return ["（ALERTS.md 存在但未解析到告警条目）"]

    return alert_lines[-max_alerts:]


def load_wake_metrics(board_root):
    """汇总唤醒台账，严格区分传输成功与任务真实活动。"""
    ledger = board_root.parent / "_reports" / "board-wake-ledger.jsonl"
    counts = Counter()
    if not ledger.exists():
        return counts
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError:
        return counts
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("kind") != "wake":
            continue
        counts["attempts"] += 1
        dispatch = entry.get("dispatch_ok")
        liveness = entry.get("liveness_observed")
        if dispatch is True:
            counts["dispatch_ok"] += 1
        elif dispatch is False:
            counts["dispatch_failed"] += 1
        else:
            counts["dispatch_unknown"] += 1
        if liveness is True:
            counts["liveness_observed"] += 1
        elif liveness is False:
            counts["no_task_activity"] += 1
        elif dispatch is True:
            counts["async_unconfirmed"] += 1
    return counts


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

STATUS_ORDER = [
    "open", "claimed", "awaiting_review", "done",
    "blocked", "dlq", "awaiting_user_approval",
]


def _status_indicator(status_name, count):
    """根据状态和数量返回健康指示灯。

    open>5=🟡, claimed>3=🟡, awaiting_review>3=🟡,
    dlq>0=🔴, awaiting_user_approval>0=🔴, 其余=🟢。
    """
    if status_name == "open":
        return "🟡" if count > 5 else "🟢"
    if status_name == "claimed":
        return "🟡" if count > 3 else "🟢"
    if status_name == "awaiting_review":
        return "🟡" if count > 3 else "🟢"
    if status_name == "dlq":
        return "🔴" if count > 0 else "🟢"
    if status_name == "awaiting_user_approval":
        return "🔴" if count > 0 else "🟢"
    return "🟢"


def build_system_health(tasks):
    """构建 System Health 段。返回 (markdown_str, status_counts)。"""
    counts = Counter()
    for _, status in tasks:
        counts[status.get("status", "open")] += 1
    total = sum(counts.values())

    lines = [
        "### System Health",
        "",
        "| 指标 | 数量 | 状态 |",
        "|------|------|------|",
        f"| 总计 | {total} | — |",
    ]
    for s in STATUS_ORDER:
        c = counts.get(s, 0)
        lines.append(f"| {s} | {c} | {_status_indicator(s, c)} |")
    lines.append("")
    return "\n".join(lines), counts


def build_agent_health(tasks, heartbeats):
    """构建 Agent Health 段。返回 (markdown_str, alive_count, total_agents)。"""
    claimed = Counter()
    reviewed = Counter()
    display = {
        key: profile["display_name"] for key, profile in AGENT_CONTRACTS.items()
    }

    for _, status in tasks:
        ex = status.get("executor")
        if ex:
            k = agent_key(ex)
            if k in AGENT_CONTRACTS:
                claimed[k] += 1
        rv = status.get("reviewer")
        if rv:
            k = agent_key(rv)
            if k in AGENT_CONTRACTS:
                reviewed[k] += 1

    if not display:
        return "### Agent Health\n\n无 Agent 数据。\n", 0, 0

    now = now_cn()
    lines = [
        "### Agent Health",
        "",
        "| Agent | 最后心跳 | 状态 | 已认领 | 已审查 |",
        "|-------|---------|------|--------|--------|",
    ]

    alive = 0
    for k in sorted(display.keys()):
        name = display[k]
        hb_dt = heartbeats.get(k, (None, None))[1]
        if hb_dt is None:
            icon = "⚫"
            hb_str = "无心跳记录"
        else:
            age = now - hb_dt
            if age < timedelta(hours=2):
                icon = "🟢"
                alive += 1
            elif age <= timedelta(hours=4):
                icon = "🟡"
            else:
                icon = "🔴"
            hb_str = fmt_dt(hb_dt)
        lines.append(
            f"| {name} | {hb_str} | {icon} | {claimed.get(k, 0)} | {reviewed.get(k, 0)} |"
        )
    lines.append("")
    return "\n".join(lines), alive, len(display)


def build_project_progress(tasks):
    """构建 Project Progress 段。返回 (markdown_str, project_count)。"""
    projects = defaultdict(
        lambda: {"total": 0, "done": 0, "in_progress": 0, "blocked": 0}
    )
    for _, status in tasks:
        proj = status.get("project") or "(无项目)"
        p = projects[proj]
        p["total"] += 1
        s = status.get("status", "open")
        if s == "done":
            p["done"] += 1
        elif s == "blocked":
            p["blocked"] += 1
        elif s in ("claimed", "awaiting_review"):
            p["in_progress"] += 1

    if not projects:
        return "### Project Progress\n\n无项目数据。\n", 0

    lines = [
        "### Project Progress",
        "",
        "| 项目 | 总任务 | 已完成 | 进行中 | 阻塞 | 完成率 |",
        "|------|--------|--------|--------|------|--------|",
    ]
    for name in sorted(projects.keys()):
        p = projects[name]
        rate = (p["done"] / p["total"] * 100) if p["total"] > 0 else 0
        lines.append(
            f"| {name} | {p['total']} | {p['done']} | {p['in_progress']} | {p['blocked']} | {rate:.0f}% |"
        )
    lines.append("")
    return "\n".join(lines), len(projects)


def build_agent_confidence(reviews):
    """构建 Agent Confidence 段。无 REVIEW.md 时返回 None。

    按 agent × complexity 分组，从最近一次验收往前数连续通过数。
    """
    if not reviews:
        return None

    grouped = defaultdict(lambda: defaultdict(list))
    display = {}
    for r in reviews:
        grouped[r["agent_key"]][r["complexity"]].append(r)
        display[r["agent_key"]] = AGENT_CONTRACTS[r["agent_key"]]["display_name"]

    levels = ["L1", "L2", "L3", "L4"]
    lines = [
        "### Agent Confidence",
        "",
        "| Agent | L1 | L2 | L3 | L4 |",
        "|-------|----|----|----|----|",
    ]
    for k in sorted(display.keys()):
        name = display[k]
        cells = []
        for lv in levels:
            revs = grouped[k].get(lv, [])
            revs_sorted = sorted(
                revs,
                key=lambda r: r["time"] or datetime.min.replace(tzinfo=CN_TZ),
                reverse=True,
            )
            consecutive = 0
            for r in revs_sorted:
                if r["result"] == "PASS" or r["result"].lower() in LEGACY_REVIEW_RESULTS:
                    consecutive += 1
                else:
                    break
            if consecutive > 0:
                cells.append(f"{consecutive}✅")
            else:
                cells.append(str(consecutive))
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def build_alerts_summary(alerts):
    """构建 Recent Alerts Summary 段。"""
    lines = ["### Recent Alerts Summary", ""]
    if alerts is None:
        lines.append("（ALERTS.md 不存在）")
    else:
        for i, a in enumerate(alerts, 1):
            lines.append(f"{i}. {a}")
    lines.append("")
    return "\n".join(lines)


def build_wake_delivery(metrics):
    """构建唤醒传输与真实活动分栏，避免把窗口/会话创建当开工。"""
    lines = [
        "### Wake Delivery",
        "",
        "| 指标 | 数量 |",
        "|------|------|",
        f"| 唤醒尝试 | {metrics.get('attempts', 0)} |",
        f"| 传输成功 | {metrics.get('dispatch_ok', 0)} |",
        f"| 已观察到任务活动 | {metrics.get('liveness_observed', 0)} |",
        f"| 已传输但未观察到活动 | {metrics.get('no_task_activity', 0)} |",
        f"| 普通异步唤醒（未确认） | {metrics.get('async_unconfirmed', 0)} |",
        f"| 传输失败 | {metrics.get('dispatch_failed', 0)} |",
        f"| 旧台账/传输状态未知 | {metrics.get('dispatch_unknown', 0)} |",
        "",
        "> 传输成功只表示 prompt 已送达；只有“已观察到任务活动”可证明真实开工。",
        "",
    ]
    return "\n".join(lines)


def build_generation_info():
    """构建 Generation Info 段。"""
    now = now_cn()
    return (
        "### Generation Info\n"
        "\n"
        f"- 生成时间: {now.strftime('%Y-%m-%d %H:%M:%S')} CST (UTC+8)\n"
        "- 由 board-dashboard.py 自动生成\n"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="公告牌系统健康监控仪表盘"
    )
    parser.add_argument(
        "--board-root",
        default=str(Path(__file__).parent),
        help="board 根目录（默认: 脚本所在目录）",
    )
    parser.add_argument("--mode", choices=["readonly", "apply"], default="readonly")
    args = parser.parse_args()

    try:
        board_root = Path(args.board_root).resolve()
        if not board_root.is_dir():
            print(f"错误: board 根目录不存在或不是目录: {board_root}", file=sys.stderr)
            return 1

        now = now_cn()

        # 加载数据
        tasks = load_tasks(board_root)
        heartbeats = load_heartbeats(board_root)
        reviews = load_reviews(board_root, tasks)
        alerts = load_alerts(board_root)
        wake_metrics = load_wake_metrics(board_root)

        # 构建各段
        health_md, counts = build_system_health(tasks)
        agent_md, alive, total_agents = build_agent_health(tasks, heartbeats)
        project_md, project_count = build_project_progress(tasks)
        confidence_md = build_agent_confidence(reviews)
        wake_md = build_wake_delivery(wake_metrics)
        alerts_md = build_alerts_summary(alerts)
        gen_md = build_generation_info()

        # 组装 MONITOR.md
        sections = [
            "# Board Monitor",
            "",
            f"> 生成时间: {now.strftime('%Y-%m-%d %H:%M:%S')} CST (UTC+8)",
            "",
            health_md,
            agent_md,
            wake_md,
            project_md,
        ]
        if confidence_md:
            sections.append(confidence_md)
        sections.extend([alerts_md, gen_md])

        monitor_path = board_root / "MONITOR.md"
        if args.mode == "apply":
            monitor_path.write_text("\n".join(sections), encoding="utf-8")

        # stdout 摘要
        total = sum(counts.values())
        print("Board Dashboard Generated")
        print("=" * 50)
        print(f"Board Root : {board_root}")
        print(
            f"Tasks      : {total} total "
            f"(open:{counts.get('open', 0)}, "
            f"claimed:{counts.get('claimed', 0)}, "
            f"awaiting_review:{counts.get('awaiting_review', 0)}, "
            f"done:{counts.get('done', 0)}, "
            f"blocked:{counts.get('blocked', 0)}, "
            f"dlq:{counts.get('dlq', 0)}, "
            f"awaiting_user_approval:{counts.get('awaiting_user_approval', 0)})"
        )
        print(f"Agents     : {total_agents} (alive: {alive})")
        print(f"Projects   : {project_count}")
        if reviews:
            print(f"Reviews    : {len(reviews)} REVIEW.md parsed")
        else:
            print("Reviews    : no REVIEW.md files found")
        print(f"Output     : {monitor_path if args.mode == 'apply' else 'readonly (not written)'}")
        print()

        return 0

    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
