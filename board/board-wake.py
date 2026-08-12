#!/usr/bin/env python3
# board-wake.py — 公告牌确定性唤醒路由器 v1.6（混合模式：队列兜底 + 直接 push 快路径）
# 职责：值班巡查端发现定向任务后，通过本脚本程序化拉起对应端执行。
# 原则：确定性管道（零 LLM），LLM 判断留在值班端；所有拉起记台账；失败降级不阻塞。
#
# v1.6（2026-08-12）：
#   trae 降级为队列-only：CLI 不接受 prompt、cliclick 受 TCC 限制、URL scheme 不支持 chat，
#   外部进程无法直接唤醒 TraeWork GUI。仅写共享队列，依赖 Trae 自己的 Schedule 30min
#   轮询从 _wake-queue.jsonl 拾取任务。任务路由优先分配给可直接唤醒的 agent。
#
# v1.5 混合模式（2026-08-12）：
#   Layer 1 = 共享队列 _wake-queue.jsonl（保证兜底，各端 poller 拾取）
#   Layer 2 = 直接 push 快路径（DB/CLI，成功则低延迟，失败靠队列兜底）
#   每次唤醒先写队列，再尝试直接 push；直接 push 失败不影响队列已写入。
#
# 通道矩阵（2026-08-12 实测）：
#   workbuddy   = 普通模式写 DB automation；urgent 模式 DB 可见任务与 codebuddy -p 立即执行竞速
#   codex       = ChatGPT.app 内 codex exec（额度断档时探测跳过，正常态）
#   antigravity = language_server agentapi new-conversation（env 从运行中进程动态提取）
#   trae        = 队列-only（无法直接唤醒；依赖 Schedule 30min 轮询队列拾取）
#   qwenwork    = qoderclicn -p；未登录返回结构化 internal_qwen_job 回退要求
#
# 用法：
#   board-wake.py channels                        # 列出通道与就绪状态（二进制/应用/额度探测）
#   board-wake.py wake --channel C --task T --prompt-file F [--dry-run] [--force]
#   board-wake.py ledger [--tail N]
#
# 台账：.kb/_reports/board-wake-ledger.jsonl
# 去重（2026-08-08 修订 3，用户拍板）：窗口 25min（< 哨兵 30min 周期，未认领任务每轮可再唤醒）；
#   仅成功唤醒(ok=true)计去重——失败不去重，靠下一轮哨兵定时重试兜底（冷静期=30min 轮询节奏）；
#   NOT_OPEN 守卫：任务非 open（已认领/待验收等）直接跳过不唤醒，认领态不受任何长窗口锁死。
# 熔断：.kb/board-wake.paused 存在 → 拒绝一切唤醒（零 LLM 停机纪律）
# 权限（v1.4）：workbuddy 普通通道权限由前端 interactive 会话管理；urgent 的立即 CLI
#   使用显式最小工具集，并与前台可见 automation 并行保留可追溯性。
# 测试注入：BOARD_WAKE_KB 环境变量可覆盖 KB 根路径（临时 KB 回归，不动真实台账）

import argparse, fcntl, json, os, re, subprocess, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from board_contract import (
    AGENT_CONTRACTS,
    canonical_agent,
    effective_execute_level,
    level_value,
)
from board_transition import TransitionError, validate_reviewer

_kb_default = os.environ.get("KB_ROOT", str(Path(__file__).resolve().parent.parent.parent))
KB = Path(os.environ.get("BOARD_WAKE_KB", _kb_default))
LEDGER = KB / ".kb/_reports/board-wake-ledger.jsonl"
BOARD = KB / ".kb/board"
PAUSED = KB / ".kb/board-wake.paused"
WAKE_QUEUE = KB / ".kb/board/_wake-queue.jsonl"

CODEBUDDY = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"
CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex"
LANGSERVER = "/Applications/Antigravity.app/Contents/Resources/bin/language_server"
TRAE = "/Applications/TRAE SOLO CN.app/Contents/Resources/app/bin/trae-solo-cn"
QODER = "/Applications/QwenWorkCN.app/Contents/Resources/bin/qoderclicn"

# 只有实际由通道实现固定下来的模型才能用于 task-level review override。
# 其他端若模型由前台会话动态选择，保持 None，不能靠猜测绕过精确模型门禁。
CHANNEL_FIXED_REVIEW_MODELS = {
    "workbuddy": "deepseek-v4-flash",
}

CHANNEL_DEFAULT_MODELS = {
    "workbuddy": "deepseek-v4-flash",
    "codex": "gpt-5.6-sol",
    "antigravity": "gemini-3.6-flash",
    "trae": "glm-5.2",
    "qwenwork": "qwork-advanced",
}

CIRCUIT_FAILURES = 3
CIRCUIT_FAILURE_WINDOW = timedelta(hours=2)
CIRCUIT_COOLDOWN = timedelta(minutes=30)

TZ = timezone(timedelta(hours=8))

def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")

def log_entry(entry):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

def enqueue_wake(task, channel, mode, model, prompt_ref):
    """写入共享唤醒队列（Layer 1 保证兜底）。

    各端 poller 从 _wake-queue.jsonl 拉取自己名下的 pending 任务。
    直接 push（cliclick/DB/CLI）是 Layer 2 快路径——成功则低延迟，
    失败则 poller 在 30s 内拾取队列任务兜底。
    """
    entry = {
        "ts": now_iso(),
        "task": task,
        "channel": channel,
        "mode": mode,
        "model": model,
        "prompt_ref": prompt_ref,
        "status": "pending",
    }
    WAKE_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(WAKE_QUEUE, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return True
    except Exception:
        return False

def read_ledger_entries():
    if not LEDGER.exists():
        return []
    entries = []
    try:
        with open(LEDGER, "r", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            lines = f.read().splitlines()
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        return []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except (TypeError, json.JSONDecodeError):
            continue
    return entries

def parse_entry_time(entry):
    try:
        parsed = datetime.fromisoformat(entry["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)

def is_transport_attempt(entry):
    """Only real dispatches affect the circuit. Gate refusals must not self-poison it."""
    if entry.get("kind") != "wake":
        return False
    if isinstance(entry.get("dispatch_ok"), bool):
        return True
    return (
        "latency_s" in entry
        and entry.get("rc") not in {200, 201, 202, 203}
        and "DEGRADED" not in str(entry.get("note", ""))
    )

def channel_circuit_state(channel, *, now=None):
    current = now or datetime.now(TZ)
    cutoff = current - CIRCUIT_FAILURE_WINDOW
    attempts = []
    for entry in read_ledger_entries():
        if entry.get("channel") != channel or not is_transport_attempt(entry):
            continue
        ts = parse_entry_time(entry)
        if ts is not None and ts >= cutoff:
            attempts.append((ts, entry))
    attempts.sort(key=lambda item: item[0])
    consecutive_failures = 0
    for _, entry in reversed(attempts):
        if entry.get("ok") is True:
            break
        consecutive_failures += 1
    last_ts = attempts[-1][0] if attempts else None
    cooldown_remaining_s = 0
    circuit_open = False
    half_open = False
    if consecutive_failures >= CIRCUIT_FAILURES and last_ts is not None:
        elapsed = current - last_ts
        if elapsed < CIRCUIT_COOLDOWN:
            circuit_open = True
            cooldown_remaining_s = max(0, int((CIRCUIT_COOLDOWN - elapsed).total_seconds()))
        else:
            half_open = True
    return {
        "open": circuit_open,
        "half_open": half_open,
        "consecutive_failures": consecutive_failures,
        "cooldown_remaining_s": cooldown_remaining_s,
        "last_attempt_at": last_ts.isoformat(timespec="seconds") if last_ts else None,
    }

def dedup_hit(task, channel):
    """25min 窗口内已有成功唤醒 → 去重命中。只统计 ok=true：失败唤醒不产生去重，
    由下一轮哨兵（30min 节奏）定时重试兜底。"""
    cutoff = datetime.now(TZ) - timedelta(minutes=25)
    for e in read_ledger_entries():
        if (e.get("task") == task and e.get("channel") == channel
                and e.get("kind") == "wake" and e.get("ok") is True):
            ts = parse_entry_time(e)
            if ts is not None and ts > cutoff:
                return True
    return False

def task_is_open(task):
    """NOT_OPEN 守卫：认领中/待验收/已完成的任务不唤醒。
    status.json 不存在或不可读 → 放行（视为 open，不挡板上外任务）。"""
    sj = BOARD / "tasks" / task / "status.json"
    if not sj.exists():
        return True
    try:
        return json.loads(sj.read_text()).get("status") == "open"
    except Exception:
        return True

def task_status(task):
    """读取任务当前状态；不可读返回 None。"""
    path = BOARD / "tasks" / task / "status.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return None

def validate_wake_mode(
    status,
    channel,
    mode,
    reviewer_model=None,
    *,
    task_dir=None,
    card_text="",
):
    """加急直唤的状态/能力前置门禁，不因 urgency 绕过安全约束。"""
    if not isinstance(status, dict):
        return False, "TASK_STATUS_UNREADABLE"
    current = status.get("status")
    if mode == "execute":
        if current != "open":
            return False, f"MODE_EXECUTE_REQUIRES_OPEN:{current}"
        profile = AGENT_CONTRACTS.get(channel)
        if profile is None:
            return False, f"MODE_EXECUTE_UNKNOWN_CHANNEL:{channel}"
        required = set(status.get("required_caps", []))
        if not required.issubset(set(profile.get("caps", []))):
            return False, f"MODE_EXECUTE_CAPS_MISMATCH:{channel}/{sorted(required)}"
        complexity = status.get("complexity", "L1")
        max_level = effective_execute_level(channel, reviewer_model)
        if level_value(complexity) > level_value(max_level):
            return False, f"MODE_EXECUTE_LEVEL_EXCEEDED:{channel}/{reviewer_model}/{complexity}"
        return True, "OK"
    if mode == "resume":
        executor = canonical_agent(status.get("executor"))
        return (
            current == "claimed" and executor == channel,
            f"MODE_RESUME_REQUIRES_OWN_CLAIM:{current}/{executor}",
        )
    if mode == "review":
        if current != "awaiting_review":
            return False, f"MODE_REVIEW_REQUIRES_AWAITING_REVIEW:{current}"
        if channel not in AGENT_CONTRACTS:
            return False, f"MODE_REVIEW_UNKNOWN_CHANNEL:{channel}"
        try:
            validate_reviewer(
                status,
                channel,
                reviewer_model,
                task_dir=task_dir,
                card_text=card_text,
            )
        except TransitionError as exc:
            return False, f"MODE_REVIEW_INELIGIBLE:{exc}"
        return True, "OK"
    if mode == "notify":
        # 通知模式：不要求特定状态，仅唤醒目标端告知信息（如验收完成通知）
        if channel not in AGENT_CONTRACTS:
            return False, f"MODE_NOTIFY_UNKNOWN_CHANNEL:{channel}"
        return True, "OK"
    return False, f"UNKNOWN_MODE:{mode}"

def liveness_snapshot(task):
    """状态与本任务可写产物的轻量指纹，用于确认被唤醒端真的开始工作。"""
    task_dir = BOARD / "tasks" / task
    status = task_status(task) or {}
    mtimes = {}
    for name in ("status.json", "PROGRESS.md", "REVIEW.md", "BLOCKED.md"):
        path = task_dir / name
        try:
            mtimes[name] = path.stat().st_mtime_ns
        except OSError:
            mtimes[name] = 0
    intent_mtime = 0
    intents_dir = BOARD / "intents"
    if intents_dir.is_dir():
        for path in intents_dir.glob(f"*-{task}.md"):
            try:
                intent_mtime = max(intent_mtime, path.stat().st_mtime_ns)
            except OSError:
                pass
    output_dir = task_dir / "output"
    output_mtime = 0
    if output_dir.exists():
        for path in output_dir.rglob("*"):
            if path.is_file():
                try:
                    output_mtime = max(output_mtime, path.stat().st_mtime_ns)
                except OSError:
                    pass
    return {
        "status": status.get("status"),
        "executor": status.get("executor"),
        "status_changed_at": status.get("status_changed_at"),
        "status_mtime": mtimes["status.json"],
        "progress_mtime": mtimes["PROGRESS.md"],
        "review_mtime": mtimes["REVIEW.md"],
        "blocked_mtime": mtimes["BLOCKED.md"],
        "intent_mtime": intent_mtime,
        "output_mtime": output_mtime,
    }

def wait_for_liveness(task, before, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        current = liveness_snapshot(task)
        if current != before:
            return True, current
        time.sleep(2)
    return False, liveness_snapshot(task)

# ---------- AGY env 动态提取 ----------
def agy_env():
    """从运行中的 language_server 进程提取 LS_ADDRESS/CSRF/PROJECT_ID（端口每次启动变化，必须动态取）"""
    try:
        out = subprocess.run(["pgrep", "-f", "language_server"], capture_output=True, text=True, timeout=10)
        pids = [p for p in out.stdout.split() if p]
        env = {}
        for pid in pids:
            ps = subprocess.run(["ps", "eww", "-p", pid], capture_output=True, text=True, timeout=10)
            text = ps.stdout
            for key in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_PROJECT_ID"):
                m = re.search(key + r"=(\S+)", text)
                if m and key not in env:
                    env[key] = m.group(1)
            if len(env) == 3:
                break
        return env if len(env) == 3 else None
    except Exception:
        return None

def qwen_cli_status():
    if not os.path.exists(QODER):
        return {"binary": False, "logged_in": False, "account": None}
    env = {"HOME": os.environ.get("HOME", ""), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin"}
    try:
        result = subprocess.run(
            [QODER, "status"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            cwd=str(KB),
        )
    except Exception as exc:
        return {"binary": True, "logged_in": False, "account": None, "error": type(exc).__name__}
    account = None
    match = re.search(r"^Account:\s*(.+)$", result.stdout or "", re.MULTILINE)
    if match:
        account = match.group(1).strip()
    logged_in = result.returncode == 0 and bool(account) and account.lower() != "not logged in"
    return {
        "binary": True,
        "logged_in": logged_in,
        "account": account,
        "status_rc": result.returncode,
    }

# ---------- 通道状态 ----------
def channels_status():
    st = {}
    st["workbuddy"] = {"binary": os.path.exists(CODEBUDDY), "headless": False,
                       "note": "普通=前台automation；urgent=前台automation+即时CLI竞速，先产生任务活动者生效"}
    st["codex"] = {"binary": os.path.exists(CODEX), "headless": True,
                   "note": "额度断档=正常态；探测失败自动跳过"}
    agy = agy_env()
    st["antigravity"] = {"binary": os.path.exists(LANGSERVER), "app_online": agy is not None,
                         "headless": True, "note": "env动态提取" + ("成功" if agy else "失败(应用离线=正常态,降级ping)")}
    st["trae"] = {"binary": os.path.exists(TRAE), "headless": False,
                  "queue": WAKE_QUEUE.exists(),
                  "note": "v1.6: 队列-only（无法直接唤醒；Schedule 30min 轮询 _wake-queue.jsonl 拾取）"}
    qwen = qwen_cli_status()
    st["qwenwork"] = {**qwen, "headless": True,
                      "note": "外部CLI需独立登录；未登录时明确返回内部job fallback_required"}
    for channel in st:
        st[channel]["circuit"] = channel_circuit_state(channel)
    return st

# ---------- 唤醒实现 ----------
def run(cmd, env=None, timeout=600, cwd=None):
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd or str(KB))
        dt = round(time.time() - t0, 1)
        tail = (p.stdout or "")[-300:] + (("\n[stderr]" + p.stderr[-200:]) if p.stderr else "")
        return p.returncode, dt, tail.strip()
    except subprocess.TimeoutExpired:
        return 124, round(time.time() - t0, 1), "TIMEOUT"
    except Exception as e:
        return 125, round(time.time() - t0, 1), f"EXC:{type(e).__name__}:{e}"

def cli_prompt(prompt, *, task="", channel="", mode="execute", target_model=None):
    """Wrap a frozen card in an executable, task-scoped handoff."""
    identity = f"{channel}/{target_model or CHANNEL_DEFAULT_MODELS.get(channel, '实际模型档名')}"
    scope = ""
    if task:
        scope = f"""目标任务：{task}
目标身份：{identity}
目标动作：{mode}

执行纪律：
1. 先完整读取 raw/skills/效率工具/task-flow/SKILL.md 与 .kb/board/tasks/{task}/card.md。
2. execute 只通过 board-task-claim.py 原子认领；review 先真实复测并写 REVIEW.md 再走 transition；resume 只续做原 executor 名下 claimed 任务。
3. PROGRESS/REVIEW/BLOCKED 必须署名 {identity}；不得手改 card.md、status.json 或 board-index.json。
4. 若状态已经不适合 {mode}，只报告并停止，不得改做其他 backlog。

"""
    return "公告牌定向接力：只处理下方指定任务。\n\n" + scope + prompt

def queue_workbuddy_automation(prompt):
    # v1.2（2026-08-08 前端可见性改造定稿）：原 codebuddy -p 无头（用户前端看不到）、
    # deep link 预填（需人工点发送，T31 实测）。定稿方案=DB 直插一次性 automation：
    # 向 ~/.workbuddy/workbuddy.db 的 automations 表插入 schedule_type=once 记录
    # （含 next_run_at 毫秒时间戳），WorkBuddy 前端调度器自动拾取到期任务，唤起
    # 前端 interactive 会话执行（零人工干预 + 前端可见）。
    # 实测：T32（工具创建）/T33（DB 直插）均走通全链路；T33 前端会话 PID 52422。
    # 关键字段：next_run_at 必须设毫秒时间戳（None 不会被调度器拾取）。
    import json, sqlite3, time
    from datetime import datetime, timezone, timedelta
    wb_db = Path.home() / ".workbuddy" / "workbuddy.db"
    if not wb_db.exists():
        return 210, 0.0, "WBDb_MISSING: ~/.workbuddy/workbuddy.db 不存在(WorkBuddy 未初始化)"
    tz8 = timezone(timedelta(hours=8))
    now = datetime.now(tz8)
    delay = timedelta(seconds=30)  # 给调度器拾取留窗口
    sched_iso = (now + delay).isoformat(timespec="seconds")
    ts = int(time.time() * 1000)
    aid = f"automation-{ts}"
    db = sqlite3.connect(str(wb_db))
    try:
        db.execute(
            """INSERT INTO automations
               (id, name, prompt, status, schedule_type, next_run_at, last_run_at,
                cwds, rrule, scheduled_at, valid_from, valid_until, model_id,
                model_is_thinking, push_to_wechat, created_at, updated_at, skills_json,
                deleted_at, expert_id, expert_marketplace, connector_ids_json,
                permission_mode, owner_user_id, owner_status, owner_source,
                push_to_wecom_bot, wecom_bot_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, "board-wake:workbuddy", prompt, "ACTIVE", "once",
             (now + delay).timestamp() * 1000, None,
             json.dumps([str(KB)]), "", sched_iso, "", "", "deepseek-v4-flash",
             0, 0, ts, ts, "[]", None, None, None, "[]",
             "fullAccess", "", "active", "workbuddy", 0, None))
        db.commit()
    except Exception as e:
        db.rollback()
        return 125, 0.0, f"WBDb_WRITE_FAIL: {type(e).__name__}: {e}"
    finally:
        db.close()
    return 0, 0.1, f"automation 已投递 {aid} @ {sched_iso}"

def wake_workbuddy(prompt, *, urgent=False, task=""):
    """Keep the visible automation, but race it with the verified immediate CLI for urgent work."""
    queued_rc, queued_dt, queued_tail = queue_workbuddy_automation(prompt)
    if not urgent:
        return queued_rc, queued_dt, queued_tail
    immediate_cmd = [
        CODEBUDDY,
        "-p",
        "--model",
        "deepseek-v4-flash",
        "--permission-mode",
        "bypassPermissions",
        "--tools",
        "Bash,Read,Write,Edit,Glob,Grep",
    ]
    if task:
        immediate_cmd += ["--name", f"board-wake-{task}"]
    immediate_cmd.append(prompt)
    cli_rc, cli_dt, cli_tail = run(immediate_cmd, timeout=900)
    tail = (
        f"visible_queue_rc={queued_rc}: {queued_tail}\n"
        f"immediate_cli_rc={cli_rc}: {cli_tail}"
    ).strip()
    if cli_rc == 0 or queued_rc == 0:
        return 0, round(queued_dt + cli_dt, 1), tail
    return cli_rc or queued_rc, round(queued_dt + cli_dt, 1), tail

def wake_codex(prompt, *, target_model=None):
    cmd = [CODEX, "exec", "--skip-git-repo-check"]
    if target_model:
        cmd += ["-m", target_model]
    cmd.append(prompt)
    return run(cmd, timeout=600)

def wake_agy(prompt):
    env = agy_env()
    if not env:
        return 210, 0.0, "AGY_OFFLINE: env提取失败(应用未运行=正常态,应降级ping)"
    e = dict(os.environ)
    e.update({"ANTIGRAVITY_LS_ADDRESS": env["ANTIGRAVITY_LS_ADDRESS"],
              "ANTIGRAVITY_CSRF_TOKEN": env["ANTIGRAVITY_CSRF_TOKEN"],
              "ANTIGRAVITY_PROJECT_ID": env["ANTIGRAVITY_PROJECT_ID"]})
    return run([LANGSERVER, "agentapi", "new-conversation", prompt], env=e, timeout=120)

def wake_trae(prompt, *, task=""):
    """v1.6: Trae 无法被外部进程直接唤醒（GUI-only，CLI 不接受 prompt，
    cliclick 受 TCC 限制，URL scheme 不支持 chat）。

    仅依赖共享队列 _wake-queue.jsonl——Trae 自己的 Schedule 定时任务
    （30min）会从队列拾取任务。不尝试 CLI push。
    """
    return 0, 0.0, "QUEUE_ONLY: trae 无法直接唤醒，依赖 Schedule 30min 轮询队列拾取"

def wake_qwenwork(prompt, *, target_model=None):
    env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin"}
    qwen = qwen_cli_status()
    if not qwen.get("logged_in"):
        return 210, 0.0, "QWEN_CLI_NOT_LOGGED_IN; fallback_required=internal_qwen_job"
    # 修订4：同 workbuddy 的 variadic 吞 prompt 修复（同族 CLI）；--tools 单值逗号合并。
    # 注：本通道待一次性 /login，登录后须再实测确认 --tools 选项在位。
    cmd = [QODER, "-p", "--permission-mode", "dont_ask"]
    if target_model:
        cmd += ["-m", target_model]
    cmd += ["--tools", "Read,Write,Edit,Glob,Grep", "--", prompt]
    return run(cmd, env=env, timeout=900)

WAKERS = {"workbuddy": wake_workbuddy, "codex": wake_codex, "antigravity": wake_agy,
          "trae": wake_trae, "qwenwork": wake_qwenwork}

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("channels")
    w = sub.add_parser("wake")
    w.add_argument("--channel", required=True, choices=WAKERS.keys())
    w.add_argument("--task", required=True)
    w.add_argument("--prompt-file", required=False, default=None,
                   help="任务包文件路径；与 --auto-prompt 二选一")
    w.add_argument("--auto-prompt", action="store_true",
                   help="自动从 card.md 生成标准操作模板，无需手写 prompt-file")
    w.add_argument("--dry-run", action="store_true")
    w.add_argument("--force", action="store_true")
    w.add_argument("--urgent", action="store_true", help="加急直唤：绕过去重/降级等待，并确认真实任务活动")
    w.add_argument("--mode", choices=["execute", "review", "resume", "notify"], default="execute",
                   help="execute=认领开放任务；review=验收待审任务；resume=原执行端续做退回任务；notify=通知某端信息（不要求特定状态）")
    w.add_argument("--target-model", "--reviewer-model", dest="target_model", default=None,
                   help="目标真实模型档名；模型级 L3 执行/验收必须显式提供")
    w.add_argument("--confirm-seconds", type=int, default=None,
                   help="等待 status/PROGRESS/output 真实变化的秒数；urgent 默认 90，普通默认 0")
    led = sub.add_parser("ledger")
    led.add_argument("--tail", type=int, default=10)
    a = ap.parse_args()

    if a.cmd == "channels":
        print(json.dumps(channels_status(), ensure_ascii=False, indent=1))
        return 0
    if a.cmd == "ledger":
        for entry in read_ledger_entries()[-a.tail:]:
            print(json.dumps(entry, ensure_ascii=False))
        return 0

    # wake
    before_status = task_status(a.task)
    reviewer_model = (
        a.target_model
        or CHANNEL_FIXED_REVIEW_MODELS.get(a.channel)
        or CHANNEL_DEFAULT_MODELS.get(a.channel)
    )
    confirm_seconds = a.confirm_seconds if a.confirm_seconds is not None else (90 if a.urgent else 0)
    if a.mode == "notify":
        confirm_seconds = 0
    if confirm_seconds < 0 or confirm_seconds > 300:
        print(json.dumps({"ok": False, "reason": "confirm-seconds 必须在 0..300"}, ensure_ascii=False))
        return 2
    task_dir = BOARD / "tasks" / a.task
    try:
        card_text = (task_dir / "card.md").read_text(encoding="utf-8")
    except OSError:
        card_text = ""
    mode_ok, mode_reason = validate_wake_mode(
        before_status,
        a.channel,
        a.mode,
        reviewer_model=reviewer_model,
        task_dir=task_dir,
        card_text=card_text,
    )
    if not mode_ok and not a.force:
        print(json.dumps({"ok": False, "reason": mode_reason, "mode": a.mode}, ensure_ascii=False))
        return 203
    circuit = channel_circuit_state(a.channel)
    if a.dry_run:
        print(json.dumps({
            "ok": True,
            "dry_run": True,
            "channel": a.channel,
            "task": a.task,
            "mode": a.mode,
            "target_model": reviewer_model,
            "confirm_seconds": confirm_seconds,
            "circuit": circuit,
        }, ensure_ascii=False))
        return 0
    if PAUSED.exists():
        print(json.dumps({"ok": False, "reason": "PAUSED", "marker": str(PAUSED)}, ensure_ascii=False))
        log_entry({"ts": now_iso(), "kind": "wake_gate", "task": a.task, "channel": a.channel,
                   "rc": 200, "ok": False, "note": "PAUSED熔断中,拒绝唤醒"})
        return 200
    if dedup_hit(a.task, a.channel) and not a.force and not a.urgent:
        print(json.dumps({"ok": False, "reason": "DEDUP_25M"}, ensure_ascii=False))
        return 201
    if circuit["open"] and not a.force:
        entry = {
            "ts": now_iso(),
            "kind": "wake_gate",
            "task": a.task,
            "channel": a.channel,
            "rc": 202,
            "ok": False,
            "note": "CHANNEL_COOLDOWN: 连续真实失败后冷却；到期自动半开探测",
            "circuit": circuit,
        }
        log_entry(entry)
        print(json.dumps(entry, ensure_ascii=False))
        return 202
    # v1.5: --auto-prompt 从 card.md 自动生成操作模板
    if not a.prompt_file and not a.auto_prompt:
        print(json.dumps({"ok": False, "reason": "必须提供 --prompt-file 或 --auto-prompt"}, ensure_ascii=False))
        return 2
    if a.auto_prompt:
        task_card = BOARD / "tasks" / a.task / "card.md"
        try:
            card_body = task_card.read_text(encoding="utf-8")
        except OSError as exc:
            print(json.dumps({"ok": False, "reason": f"CARD_UNREADABLE:{type(exc).__name__}"}, ensure_ascii=False))
            return 2
        target_model_name = reviewer_model or CHANNEL_DEFAULT_MODELS.get(a.channel, "实际模型档名")
        mode_desc = {
            "execute": "认领并执行任务",
            "review": "验收任务（读盘复测后写 REVIEW.md）",
            "resume": "续做退回任务",
            "notify": "接收通知",
        }.get(a.mode, a.mode)
        raw_prompt = (
            f"你是公告牌任务执行端（{a.channel}/{target_model_name}）。\n"
            f"目标任务：{a.task}\n"
            f"目标动作：{mode_desc}\n\n"
            f"执行步骤：\n"
            f"1. 认领任务：\n"
            f"   python3 .kb/board/board-task-claim.py --task {a.task} --agent {a.channel} --model {target_model_name}\n"
            f"   （review 模式跳过认领，直接读盘复测）\n"
            f"2. 读取任务卡片：\n"
            f"   cat .kb/board/tasks/{a.task}/card.md\n\n"
            f"--- 以下是 card.md 原文 ---\n"
            f"{card_body}\n"
            f"--- card.md 原文结束 ---\n\n"
            f"3. 按卡片要求执行任务\n"
            f"4. 写 PROGRESS.md（署名格式: [{a.channel}/{target_model_name}]）\n"
            f"5. 提交：\n"
            f"   python3 .kb/board/board-task-transition.py --task {a.task} --action submit "
            f"--actor {a.channel} --model {target_model_name} "
            f"--request-id {a.channel}-{a.task}-$(date +%s) "
            f"--output-ref <产物路径> --evidence-ref <证据路径>\n"
        )
        if a.mode == "review":
            raw_prompt = raw_prompt.replace(
                "1. 认领任务：\n   python3 .kb/board/board-task-claim.py --task {a.task} --agent {a.channel} --model {target_model_name}\n   （review 模式跳过认领，直接读盘复测）\n",
                "1. 验收模式：跳过认领，直接读盘复测\n"
            )
            raw_prompt += (
                f"\n验收完成后：\n"
                f"1. 填写 REVIEW.md（格式：验收端: {a.channel}/{target_model_name}\\n验收结果: PASS 或 FAIL\\n合同指纹: <从 card.md 读取>）\n"
                f"2. 调用验收：\n"
                f"   python3 .kb/board/board-task-transition.py --task {a.task} --action review "
                f"--actor {a.channel} --model {target_model_name} "
                f"--request-id {a.channel}-review-{a.task}-$(date +%s) "
                f"--result <pass|fail> --issues <非负整数>\n"
            )
    else:
        try:
            raw_prompt = Path(a.prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(json.dumps({"ok": False, "reason": f"PROMPT_UNREADABLE:{type(exc).__name__}"}, ensure_ascii=False))
            return 2
    prompt = cli_prompt(
        raw_prompt,
        task=a.task,
        channel=a.channel,
        mode=a.mode,
        target_model=reviewer_model,
    )
    # Layer 1: 写入共享唤醒队列（保证兜底，各端 poller 30s 内拾取）
    intent_path = BOARD / "intents" / f"{a.channel}-{a.task}-wake.md"
    prompt_ref = ""
    try:
        intent_path.parent.mkdir(parents=True, exist_ok=True)
        intent_path.write_text(prompt, encoding="utf-8")
        prompt_ref = str(intent_path.relative_to(KB))
    except Exception:
        pass
    queue_ok = enqueue_wake(a.task, a.channel, a.mode, reviewer_model, prompt_ref)
    before = liveness_snapshot(a.task)
    started = time.time()
    if a.channel == "workbuddy":
        rc, dispatch_dt, tail = wake_workbuddy(prompt, urgent=a.urgent, task=a.task)
        transport_strategy = "visible_automation+immediate_cli" if a.urgent else "visible_automation"
    elif a.channel == "codex":
        rc, dispatch_dt, tail = wake_codex(prompt, target_model=reviewer_model)
        transport_strategy = "codex_exec"
    elif a.channel == "qwenwork":
        rc, dispatch_dt, tail = wake_qwenwork(prompt, target_model=reviewer_model)
        transport_strategy = "qwen_external_cli"
    elif a.channel == "trae":
        rc, dispatch_dt, tail = wake_trae(prompt, task=a.task)
        transport_strategy = "trae_queue_only"
    else:
        rc, dispatch_dt, tail = WAKERS[a.channel](prompt)
        transport_strategy = "agentapi"
    dispatch_ok = rc == 0
    liveness = None
    after = liveness_snapshot(a.task)
    if dispatch_ok and confirm_seconds > 0:
        liveness, after = wait_for_liveness(a.task, before, confirm_seconds)
        if not liveness:
            rc = 211
            tail = (tail + "\nDISPATCHED_NO_TASK_ACTIVITY").strip()
    # ok 判定：
    # - liveness is True → ok（确认有活动）
    # - liveness is False → not ok（确认无活动）
    # - liveness is None（未做 liveness check）：
    #   - notify 模式 → ok = dispatch_ok（通知只关心投递）
    #   - headless 通道 → ok = dispatch_ok（调度器会拾取）
    #   - 非 headless（trae）→ ok = False（无法确认执行）
    if liveness is True:
        ok = dispatch_ok
    elif liveness is False:
        ok = False
    elif a.mode == "notify" or a.channel in {"workbuddy", "codex", "antigravity", "qwenwork"}:
        ok = dispatch_ok
    elif a.channel == "trae":
        ok = queue_ok  # 队列-only：投递成功 = 队列写入成功
    else:
        ok = False  # 非 headless 通道未做 liveness check → 不确认
        if dispatch_ok:
            tail = (tail + "\nDISPATCHED_NO_LIVENESS_VERIFY").strip()
    total_dt = round(time.time() - started, 1)
    entry = {"ts": now_iso(), "kind": "wake", "task": a.task, "channel": a.channel,
             "mode": a.mode, "target_model": reviewer_model, "urgent": a.urgent, "rc": rc, "ok": ok,
             "dispatch_ok": dispatch_ok, "liveness_observed": liveness,
             "transport_strategy": transport_strategy,
             "queue_enqueued": queue_ok, "queue_prompt_ref": prompt_ref or None,
             "dispatch_latency_s": dispatch_dt, "latency_s": total_dt,
             "confirm_seconds": confirm_seconds, "circuit_before": circuit,
             "fallback_required": "internal_qwen_job" if "fallback_required=internal_qwen_job" in tail else None,
             "before": before, "after": after, "tail": tail}
    log_entry(entry)
    print(json.dumps(entry, ensure_ascii=False))
    return 0 if ok else rc

if __name__ == "__main__":
    sys.exit(main())
