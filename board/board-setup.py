#!/usr/bin/env python3
"""board-setup — 各端一键接入公告牌任务流转系统。

用法:
  python3 board-setup.py --list                       # 列出所有已注册端
  python3 board-setup.py --agent trae                  # 生成 TraeWork 接入配置
  python3 board-setup.py --agent workbuddy --check     # 只检查 WorkBuddy 当前状态
  python3 board-setup.py --agent antigravity           # 生成 Antigravity 接入配置

输出:
  1. 端 profile（能力/模型/角色/限制）
  2. 当前接入状态（心跳/意图/轮询配置）
  3. 接入步骤（端特定）
  4. 个性化巡查 prompt（写入 experimental/<端名>-polling-prompt.md）
  5. 就绪检查清单
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from board_contract import AGENT_CONTRACTS, canonical_agent, is_physical_task_dir
from board_verdicts import REVIEW_CLI_DISPLAY, REVIEW_DISPLAY

CN_TZ = timezone(timedelta(hours=8))
BOARD_ROOT = Path(__file__).parent.resolve()


# ─── 端 profile ───────────────────────────────────────────────

AGENT_PROFILES = {
    "trae": {
        "display_name": "Trae",
        "caps": ["scheduled-task", "trae"],
        "polling_period": "30min",
        "cron": "*/30 * * * *",
        "model": "glm-5.2 (会话固定，不可指定)",
        "can_specify_model": False,
        "cli": [
            "codex exec -m gpt-5.6-sol '<prompt>'",
            "codebuddy --model deepseek-v4-pro --print '<prompt>'",
        ],
        "roles": ["调度器", "L1/L2执行", "L1/L2验收"],
        "limitations": [
            "不能做 L3/L4 验收（模型能力不足）",
            "会话模型固定为 glm-5.2",
            "无 agent CLI 直接唤起，需通过 RunCommand 调 codex/codebuddy",
        ],
        "model_families": ["glm"],
        "max_execute_level": "L2",
        "max_review_level": "L2",
        "internal_hetero_model": False,
        "setup_type": "schedule_tool",
        "setup_steps": [
            "在 TraeWork 中使用 Schedule 工具创建定时任务：",
            '  Schedule action=create name="trae-board-poll" \\',
            '    cron="*/30 * * * *" \\',
            '    message="<下方生成的 prompt 全文>"',
            "或在 GUI 中手动创建每 30 分钟的定时会话，粘贴 prompt",
        ],
    },
    "workbuddy": {
        "display_name": "WorkBuddy",
        "caps": ["scheduled-task", "workbuddy"],
        "polling_period": "55min",
        "cron": "*/55 * * * *",
        "model": "可选 12 款（glm-5.2/5.1/5v-turbo, kimi-k3-1/k2.7/k2.6, deepseek-v4-flash/pro, minimax-m3, auto/hy3/custom-local）",
        "can_specify_model": True,
        "cli": [
            "codebuddy --model <model> --print '<prompt>'",
        ],
        "roles": ["L1/L2/L3执行", "L1/L2/L3验收", "内部异模型验收"],
        "limitations": [
            "rrule 需特定格式（FREQ=HOURLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR,SA,SU）",
            "valid_from/valid_until 用 .000Z UTC 格式",
        ],
        "model_families": ["deepseek", "kimi", "glm", "minimax"],
        "max_execute_level": "L3",
        "max_review_level": "L3",
        "internal_hetero_model": True,
        "setup_type": "automation_db",
        "setup_steps": [
            "方式一：在 WorkBuddy GUI 中创建 automation",
            "  rrule: FREQ=HOURLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR,SA,SU",
            "  valid_from/valid_until: .000Z UTC 格式",
            "  message: <下方生成的 prompt 全文>",
            "方式二：通过 sqlite3 写入 automations 表",
            "  sqlite3 ~/.workbuddy/workbuddy.db \"INSERT INTO automations ...\"",
        ],
    },
    "antigravity": {
        "display_name": "Antigravity",
        "caps": ["scheduled-task", "antigravity"],
        "polling_period": "60min",
        "cron": "0 * * * *",
        "model": "gemini-3.6-flash (sidecar 固定)",
        "can_specify_model": False,
        "cli": ["agy CLI"],
        "roles": ["L1/L2执行", "L1/L2验收"],
        "limitations": [
            "依赖 Antigravity 应用在线",
            "sidecar 固定模型，不可指定",
            "Google OAuth 可能周期性失效（需监控）",
        ],
        "model_families": ["gemini"],
        "max_execute_level": "L2",
        "max_review_level": "L2",
        "internal_hetero_model": False,
        "setup_type": "sidecar",
        "setup_steps": [
            "在 ~/.gemini/config/config.json 中注册 sidecar（增量修改，规则7）",
            "sidecar 每 15 分钟触发巡查会话",
            "确保 Antigravity 应用保持在线",
            "定期检查 OAuth 状态",
        ],
    },
    "qwenwork": {
        "display_name": "QwenWork",
        "caps": ["scheduled-task", "qwenwork"],
        "polling_period": "30min",
        "cron": "*/30 * * * *",
        "model": "Qwen3.8-Max（L3）/ qwork-advanced（L2）/ 其他默认 L2",
        "can_specify_model": False,
        "cli": [],
        "roles": ["Qwen3.8-Max L1/L2/L3执行验收", "Qwen高级 L1/L2执行验收", "文生图创作"],
        "limitations": [
            "需手动配接力包或 Schedule 工具",
            "必须写真实模型档名；未识别模型按 L2 保守处理",
        ],
        "model_families": ["qwen"],
        "max_execute_level": "L3",
        "max_review_level": "L3",
        "internal_hetero_model": False,
        "setup_type": "schedule_tool",
        "setup_steps": [
            "在 QwenWork 中使用 Schedule 工具创建定时任务：",
            '  cron="*/30 * * * *"',
            '  message="<下方生成的 prompt 全文>"',
            "或通过接力包指导 QwenWork 配置轮询",
        ],
    },
    "codex": {
        "display_name": "Codex",
        "caps": ["codex"],
        "polling_period": "无（被唤起）",
        "cron": None,
        "model": "gpt-5.6-sol / o3 (-m 指定)",
        "can_specify_model": True,
        "cli": ["codex exec -m gpt-5.6-sol '<prompt>'"],
        "roles": ["L3/L4执行", "L3/L4验收"],
        "limitations": [
            "无自主轮询能力",
            "需被其他端 CLI 唤起",
            "当前额度受限",
        ],
        "model_families": ["gpt"],
        "max_execute_level": "L4",
        "max_review_level": "L4",
        "internal_hetero_model": False,
        "setup_type": "invoked",
        "setup_steps": [
            "Codex 不需要自主轮询配置",
            "由 TraeWork / WorkBuddy 通过 CLI 唤起：",
            "  codex exec -m gpt-5.6-sol '<prompt>'",
            "确保 codex CLI 可用且额度充足",
        ],
    },
}

# 机器判定字段以 board_contract.py 为单源；下方 profile 只保留展示、通道与安装说明。
for _agent_key, _contract in AGENT_CONTRACTS.items():
    for _field in (
        "display_name",
        "caps",
        "model_families",
        "max_execute_level",
        "max_review_level",
        "internal_hetero_model",
    ):
        AGENT_PROFILES[_agent_key][_field] = _contract[_field]


# ─── 个性化 prompt 生成 ────────────────────────────────────────

def generate_polling_prompt(agent_key: str, profile: dict) -> str:
    """根据端 profile 生成个性化巡查 prompt。"""
    display = profile["display_name"]
    caps = profile["caps"]
    caps_str = ", ".join(caps)
    file_prefix = agent_key  # 文件名用小写 key
    cadence = {
        "trae": "每 30 分钟轮询，由 QwenWork 哨兵按需 wake",
        "workbuddy": "每 3 小时兜底，由 QwenWork 哨兵按需 wake",
        "antigravity": "每 3 小时兜底，由 QwenWork 哨兵按需 wake",
        "qwenwork": "约每 30 分钟值班巡查并承担他端发现职责",
        "codex": "不自主轮询，只在被唤起时运行",
    }[agent_key]

    # 端特定备注
    agent_notes = []
    if agent_key == "trae":
        agent_notes.extend([
            f"- 你的注册端名: {agent_key}（显示名 {display}），能力: {caps_str}",
            "- 你可以执行 L1/L2 任务，也可以验收 L1/L2 任务",
            "- 你不能做 L3/L4 验收（模型能力不足）",
            "- L3/L4 任务通过 CLI 唤起 Codex 执行: codex exec -m gpt-5.6-sol '<prompt>'",
            "- 也可唤起 CodeBuddy: codebuddy --model deepseek-v4-pro --print '<prompt>'",
        ])
    elif agent_key == "workbuddy":
        agent_notes.extend([
            f"- 你的注册端名: {agent_key}（显示名 {display}），能力: {caps_str}",
            f"- 你最高可执行 {profile['max_execute_level']}、验收 {profile['max_review_level']} 任务",
            "- 可用多个模型家族参与他端验收，但 executor 与 reviewer 必须是不同注册端",
            "- CLI 唤起: codebuddy --model <model> --print '<prompt>'",
        ])
    elif agent_key == "antigravity":
        agent_notes.extend([
            f"- 你的注册端名: {agent_key}（显示名 {display}），能力: {caps_str}",
            "- 你可以执行 L1/L2 任务，也可以验收 L1/L2 任务",
            "- 依赖应用在线，如果应用未启动则跳过本次巡查",
        ])
    elif agent_key == "qwenwork":
        agent_notes.extend([
            f"- 你的注册端名: {agent_key}（显示名 {display}），能力: {caps_str}",
            "- 模型级上限：Qwen3.8-Max 可执行/验收 L3；qwork-advanced（Qwen 高级）及其他模型最高 L2",
            "- 调用 next-action/claim/transition 时必须填写真实模型档名；未识别模型按 L2 保守处理",
            "- next-action 返回 wake 时，你是值班转发端：只执行 JSON.command，不自行认领该任务，也不把 rc=0 误报为目标端已开工",
            "- 你还擅长文生图创作类任务",
        ])
    elif agent_key == "codex":
        agent_notes.extend([
            f"- 你的注册端名: {agent_key}（显示名 {display}），能力: {caps_str}",
            "- 你不自主轮询，被其他端 CLI 唤起执行 L3/L4 任务",
            "- 你的模型: gpt-5.6-sol 或 o3",
        ])

    # 所有端共用的机器契约备注。
    constraint_notes = [
        "- 认领前检查 card.md 角色约束段（preferred_executor/locked_executor/require_hetero_model），不满足约束则跳过",
        "- 验收前检查 require_hetero_model，如为 true 则验证自己与执行端模型家族不同",
        "- executor 与 reviewer 必须是不同注册端；NO_ELIGIBLE_INDEPENDENT_REVIEWER 只能由用户 authorize-reviewer event 解锁",
        "- 验收时先填写 REVIEW.md，再调用 board-task-transition.py；治理任务由转换器自动进入 awaiting_user_approval",
        "- 认领前检查 depends_on 依赖是否全部 done，有未完成依赖则跳过（eligible=false）",
    ]

    prompt = f"""你是公告牌巡查端（{display}）。运行节奏：{cadence}。不要寒暄，每轮最多处理一个最高优先级动作。

0. 运行确定性预检（如遇 python3 报 No module named 'encodings' 等环境异常，加 env -u PYTHONHOME -u PYTHONPATH 前缀）：
   env -u PYTHONHOME -u PYTHONPATH python3 $KB_ROOT/.kb/board/board-maintenance.py --apply
1. 获取唯一下一动作（恢复未完成任务 → 逾期验收 → 普通验收/跨端唤醒 → 新任务/跨端唤醒）：
   env -u PYTHONHOME -u PYTHONPATH python3 $KB_ROOT/.kb/board/board-next-action.py --context patrol --agent {agent_key} --model <实际模型档名>
2. 严格按 JSON 的 action 执行：
   - recover：执行 JSON.command，再重新获取一次下一动作。
   - resume：读 card.md、PROGRESS.md、REVIEW.md 后续做；完成写 PROGRESS.md 并调用 transition submit；受阻写带署名的 BLOCKED.md 并调用 transition block。
   - execute_from_queue：他端通过共享队列委派给你的任务。读 JSON.prompt_ref 指向的 intent 文件，按其中指令执行（claim+execute 或 review）。这是定向委派，优先于普通验收和新任务认领。
   - review：实际读盘/复测，填写 REVIEW.md（验收端/模型、{REVIEW_DISPLAY}、合同指纹、问题数），再执行 JSON.command 中的 transition review 模板。
   - claim：执行 JSON.command；仅 exit 0 继续，exit 3 立即停止。认领后读 card.md 执行，完成写 PROGRESS.md 并调用 transition submit。
   - wake：只执行 JSON.command。只有返回 liveness_observed=true 才记录“目标端已开工”；rc=202/210/211 均保留任务在牌面，等待冷却后的半开探针、内部 Qwen job 或下一轮巡查。
   - idle：不认领任务，直接写心跳。
3. submit/block/review/requeue/approve 只准调用 board-task-transition.py，并为每次转换提供唯一 request-id；禁止手改 status.json。
4. 在 experimental/{file_prefix}-heartbeat.md 写心跳（端名/模型、时间戳、本轮 action/task、结果、告警摘要）。

注意：
"""
    for note in agent_notes:
        prompt += note + "\n"
    for note in constraint_notes:
        prompt += note + "\n"
    prompt += f"""- 只写自己名下的运行文件（experimental/{file_prefix}-*.md、自己执行任务的 PROGRESS/BLOCKED、自己验收任务的 REVIEW）
- 不修改 card.md（冻结）
- 不修改 board-index.json（由确定性脚本生成）
- 不直接编辑 status.json；create/claim 以外的状态变化全部走 board-task-transition.py
- 执行方不自评闭环（self_close=false），验收方=非执行方
- 创建新任务用 board-task-create.py，必须提供 work-key 与 acceptance/output/evidence，不要手动拼 JSON
- v3 submit 必须提供 output-ref 与 evidence-ref；review 只接受 {REVIEW_CLI_DISPLAY}
- board-next-action.py 只负责选择，不会修改任务；原子认领脚本和转换脚本是唯一写入口
"""
    return prompt


# ─── 状态检查 ─────────────────────────────────────────────────

def parse_iso(ts_str):
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CN_TZ)
        return dt
    except ValueError:
        return None


def check_status(agent_key: str, profile: dict, board_root=None) -> list:
    """检查端当前接入状态，返回 [(检查项, 状态, 详情)] 列表。"""
    checks = []
    now = now_cn()
    root = board_root or BOARD_ROOT

    # 1. 心跳文件
    hb_path = root / "experimental" / f"{agent_key}-heartbeat.md"
    if hb_path.exists():
        content = hb_path.read_text(encoding="utf-8")
        # 尝试找最近的时间戳
        stamps = []
        for line in content.split("\n"):
            for fmt in ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"]:
                try:
                    stamps.append(datetime.strptime(line.strip()[:16], fmt).replace(tzinfo=CN_TZ))
                except ValueError:
                    pass
        if stamps:
            latest = max(stamps)
            age_min = int((now - latest).total_seconds() / 60)
            if age_min < 120:
                checks.append(("心跳", f"活跃（{age_min}min 前）", str(hb_path.name)))
            else:
                checks.append(("心跳", f"过期（{age_min}min 前）", str(hb_path.name)))
        else:
            checks.append(("心跳", "存在但无时间戳", str(hb_path.name)))
    else:
        checks.append(("心跳", "不存在", "尚未巡查过"))

    # 2. 认领意图文件
    intent_dir = root / "intents"
    agent_intents = list(intent_dir.glob(f"{agent_key}-*.md")) if intent_dir.exists() else []
    if agent_intents:
        checks.append(("意图文件", f"{len(agent_intents)} 个", ", ".join(p.name for p in agent_intents[:3])))
    else:
        checks.append(("意图文件", "无", ""))

    # 3. 轮询配置（端特定）
    setup_type = profile["setup_type"]
    if setup_type == "schedule_tool":
        checks.append(("轮询配置", "需手动确认", "检查 Schedule 工具中是否有定时任务"))
    elif setup_type == "automation_db":
        checks.append(("轮询配置", "需手动确认", "检查 WorkBuddy automations 表"))
    elif setup_type == "sidecar":
        config_path = Path.home() / ".gemini" / "config" / "config.json"
        if config_path.exists():
            checks.append(("轮询配置", "config.json 存在", str(config_path)))
        else:
            checks.append(("轮询配置", "config.json 不存在", str(config_path)))
    elif setup_type == "invoked":
        checks.append(("轮询配置", "无需（被唤起）", f"{profile['display_name']} 不自主轮询"))

    # 4. 已执行/验收的任务
    tasks_root = root / "tasks"
    executed = 0
    reviewed = 0
    if tasks_root.exists():
        for task_dir in tasks_root.iterdir():
            if not is_physical_task_dir(task_dir, tasks_root):
                continue
            sp = task_dir / "status.json"
            if not sp.exists():
                continue
            try:
                st = json.loads(sp.read_text(encoding="utf-8"))
                if canonical_agent(st.get("executor")) == agent_key:
                    executed += 1
                if canonical_agent(st.get("reviewer")) == agent_key:
                    reviewed += 1
            except (json.JSONDecodeError, OSError):
                pass
    checks.append(("已执行任务", f"{executed} 个", ""))
    checks.append(("已验收任务", f"{reviewed} 个", ""))

    return checks


def now_cn():
    return datetime.now(CN_TZ)


# ─── 主逻辑 ───────────────────────────────────────────────────

def print_list():
    print(f"{'='*70}")
    print(f"  公告牌已注册端")
    print(f"{'='*70}\n")
    print(f"{'Key':<14} {'端名':<14} {'轮询周期':<10} {'角色':<30}")
    print(f"{'-'*14} {'-'*14} {'-'*10} {'-'*30}")
    for key, p in AGENT_PROFILES.items():
        print(f"{key:<14} {p['display_name']:<14} {p['polling_period']:<10} {', '.join(p['roles']):<30}")
    print(f"\n使用 --agent <key> 生成接入配置")


def print_profile(agent_key: str, profile: dict):
    display = profile["display_name"]
    print(f"\n{'='*60}")
    print(f"  {display} 接入配置")
    print(f"{'='*60}\n")
    print(f"  端名:     {display}")
    print(f"  能力:     {', '.join(profile['caps'])}")
    print(f"  轮询周期: {profile['polling_period']}")
    if profile["cron"]:
        print(f"  cron:     {profile['cron']}")
    print(f"  模型:     {profile['model']}")
    print(f"  角色:     {', '.join(profile['roles'])}")
    print(f"  模型家族: {', '.join(profile['model_families'])}")
    if agent_key == "qwenwork":
        print("  模型分级: Qwen3.8-Max 执行/验收 L3；qwork-advanced 与其他模型 L2")
    else:
        print(f"  最高执行: {profile['max_execute_level']}")
        print(f"  最高验收: {profile['max_review_level']}")
    print(f"  内部异模型: {'是' if profile['internal_hetero_model'] else '否'}")
    if profile["cli"]:
        print(f"  CLI:      {profile['cli'][0]}")
        for c in profile["cli"][1:]:
            print(f"            {c}")
    if profile["limitations"]:
        print(f"  限制:     {profile['limitations'][0]}")
        for lim in profile["limitations"][1:]:
            print(f"            {lim}")


def print_status(checks: list):
    print(f"\n  --- 当前接入状态 ---")
    for name, status, detail in checks:
        line = f"  {name}: {status}"
        if detail:
            line += f" ({detail})"
        print(line)


def print_setup_steps(profile: dict):
    print(f"\n  --- 接入步骤 ---")
    for i, step in enumerate(profile["setup_steps"], 1):
        print(f"  {i}. {step}")


def write_prompt(agent_key: str, profile: dict, board_root=None) -> Path:
    prompt = generate_polling_prompt(agent_key, profile)
    root = board_root or BOARD_ROOT
    prompt_path = root / "experimental" / f"{agent_key}-polling-prompt.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def print_readiness(profile: dict, board_root=None):
    root = board_root or BOARD_ROOT
    print(f"\n  --- 就绪检查清单 ---")
    checklist = [
        "定时任务已创建（使用生成的 prompt 作为会话内容）",
        "外接存储卷可访问",
        f"能运行 python3 {root}/board-index-gen.py",
        "首次巡查后心跳文件将自动创建",
    ]
    if profile["can_specify_model"]:
        checklist.append("已决定轮询/执行使用哪个模型")
    if profile["setup_type"] == "sidecar":
        checklist.append("Antigravity 应用保持在线")
    for item in checklist:
        print(f"  [ ] {item}")


def main():
    parser = argparse.ArgumentParser(
        description="各端一键接入公告牌任务流转系统"
    )
    parser.add_argument(
        "--agent",
        choices=list(AGENT_PROFILES.keys()),
        help="要配置的端名",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查当前状态，不生成文件",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="列出所有已注册端",
    )
    parser.add_argument(
        "--board-root",
        type=Path,
        default=BOARD_ROOT,
        help="公告牌根目录；用于隔离检查和 prompt 生成测试",
    )
    args = parser.parse_args()

    if args.list:
        print_list()
        return 0

    if not args.agent:
        parser.error("--agent 或 --list 必填")

    profile = AGENT_PROFILES[args.agent]

    # 1. 打印 profile
    print_profile(args.agent, profile)

    # 2. 检查状态
    checks = check_status(args.agent, profile, args.board_root)
    print_status(checks)

    if args.check:
        return 0

    # 3. 接入步骤
    print_setup_steps(profile)

    # 4. 生成个性化 prompt
    prompt_path = write_prompt(args.agent, profile, args.board_root)
    print(f"\n  --- 个性化巡查 prompt 已生成 ---")
    print(f"  路径: {prompt_path}")
    print(f"  将此文件内容用于你的定时任务配置")

    # 5. 就绪清单
    print_readiness(profile, args.board_root)

    display = profile["display_name"]
    print(f"\n  ✅ {display} 接入配置完成。将生成的 prompt 填入定时任务即可开始巡查。\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
