#!/usr/bin/env python3
"""auto-exec-gate.py — 高级执行端自动拉起的零 LLM 闸门（拍板决策 D3/停机纪律，2026-08-08）

职责（全确定性，无推理）：
  1. 对账上一次拉起：心跳行数前进=成功；超 20min 无前进=失败；连败 2 次→建熔断文件
  2. 拉起前闸门：测试窗口(≤2026-08-22) / auto-exec.paused 熔断 / 每日预算(≤5 launch)
  3. NOGO 一律落台账（type=nogo），作为"被挡需求"的量化证据
模式：
  无参数                拉起前检查。GO → rc0 stdout=GO；NOGO:<reason> → rc1
  --triage              分诊拉起前检查（T49 预警分诊链）：在全局预算之外，额外校验
                        当日 reason=alert_triage 的拉起次数 < TRIAGE_PER_DAY。
                        可与 --selfcheck 组合（--selfcheck --triage）
  --commit <task_id> <reason>  拉起成功后调用：记台账 launch 行 + 写 pending 快照
  --selfcheck           advanced 会话启动自检（窗口/熔断/预算），同 GO/NOGO 语义。
                        预算计数排除 pending 快照对应的 launch（=本次拉起自身），
                        否则 --commit 先于会话启动记账会导致当日最后一次拉起自我否决
                        （2026-08-08 实证：预算 2 时第 2 次拉起空转，有效预算仅 1）
心跳口径：只认 advanced 专属心跳 experimental/qwenwork-heartbeat-advanced.md
          （不与哨兵-lite 共用，避免哨兵心跳伪造 verify 成功）
测试注入（仅测试用）：环境变量 GATE_BOARD_ROOT 覆盖 board 根目录；GATE_TODAY=YYYY-MM-DD 覆盖当前日期
"""
import json, os, sys, datetime
from pathlib import Path

BOARD = Path(os.environ.get("GATE_BOARD_ROOT", Path(__file__).resolve().parent))
LEDGER = BOARD / "auto-exec-ledger.jsonl"
STATE = BOARD / "auto-exec-state.json"
PAUSED = BOARD / "auto-exec.paused"
HEARTBEAT = BOARD / "experimental" / "qwenwork-heartbeat-advanced.md"
WINDOW_END = datetime.date(2026, 8, 22)
BUDGET_PER_DAY = 5       # 2026-08-08 拍板 2→3（修 selfcheck 自锁后的真实可用预算）；2026-08-09 用户拍板 3→5
TRIAGE_PER_DAY = 2       # 2026-08-09 用户拍板（T49）：分诊拉起每日上限，防告警风暴挤占任务执行预算
FAIL_AFTER_S = 1200      # 拉起后 20min 内心跳无前进判失败
FAILS_TO_BREAK = 2       # 连败 2 次自动熔断

def now():
    if os.environ.get("GATE_TODAY"):
        return datetime.datetime.strptime(os.environ["GATE_TODAY"], "%Y-%m-%d").replace(
            tzinfo=datetime.datetime.now().astimezone().tzinfo)
    return datetime.datetime.now().astimezone()

def load_state():
    if STATE.exists():
        try: return json.loads(STATE.read_text())
        except Exception: return {}
    return {}

def save_state(s):
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1))

def append_ledger(rec):
    with open(LEDGER, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def hb_lines():
    if not HEARTBEAT.exists(): return 0
    return sum(1 for _ in open(HEARTBEAT))

def launches_today(today_str, exclude=None):
    """计数当日 launch。exclude={"task_id","ts"} 时排除同任务同秒记录（自身拉起）；
    同日 refund 记录逐条扣减。不用精确 ts 匹配——commit 若产生微秒差会漏排（8/8 实证）。"""
    if not LEDGER.exists(): return 0
    n = 0
    for line in open(LEDGER):
        try: rec = json.loads(line)
        except Exception: continue
        if str(rec.get("ts", "")).startswith(today_str):
            if rec.get("type") == "launch":
                if exclude and rec.get("task_id") == exclude.get("task_id") \
                        and str(rec.get("ts"))[:19] == str(exclude.get("ts"))[:19]:
                    continue
                n += 1
            elif rec.get("type") == "refund":
                n -= 1
    return n

def triage_launches_today(today_str, exclude=None):
    """计数当日 reason=alert_triage 的 launch（分诊预算口径，T49）。
    exclude 语义同 launches_today（selfcheck 排除自身拉起）。"""
    if not LEDGER.exists(): return 0
    n = 0
    for line in open(LEDGER):
        try: rec = json.loads(line)
        except Exception: continue
        if str(rec.get("ts", "")).startswith(today_str):
            if rec.get("type") == "launch" and rec.get("reason") == "alert_triage":
                if exclude and rec.get("task_id") == exclude.get("task_id") \
                        and str(rec.get("ts"))[:19] == str(exclude.get("ts"))[:19]:
                    continue
                n += 1
    return n

def nogo(reason, selfcheck=False, triage=False):
    rec = {"type": "nogo", "ts": now().isoformat(), "reason": reason,
           "mode": "selfcheck" if selfcheck else "precheck"}
    if triage:
        rec["triage"] = True
    append_ledger(rec)
    print("NOGO:" + reason)
    return 1

def reconcile(state):
    """对账 pending 拉起。返回 (state, broke:bool)"""
    p = state.get("pending")
    if not p: return state, False
    cur = hb_lines()
    ts = datetime.datetime.fromisoformat(p["ts"])
    elapsed = (now() - ts).total_seconds()
    if cur > p.get("hb_lines", 0):
        append_ledger({"type": "verify", "ref_ts": p["ts"], "ok": True})
        state.pop("pending", None); state["consecutive_fails"] = 0
    elif elapsed > FAIL_AFTER_S:
        append_ledger({"type": "verify", "ref_ts": p["ts"], "ok": False,
                        "task_id": p.get("task_id")})
        state.pop("pending", None)
        state["consecutive_fails"] = state.get("consecutive_fails", 0) + 1
        if state["consecutive_fails"] >= FAILS_TO_BREAK:
            PAUSED.write_text(f"连败{state['consecutive_fails']}次自动熔断 {now().isoformat()}\n")
            save_state(state)
            return state, True
    save_state(state)
    return state, False

def check(selfcheck=False, triage=False):
    state = load_state()
    # selfcheck 场景：--commit 已把本次拉起记入台账，须先捕获自身再对账，
    # 否则预算计数包含自身 → 当日最后一次拉起必然 NOGO:budget_exhausted（自锁 bug）
    p = state.get("pending") or {}
    self_excl = {"task_id": p.get("task_id"), "ts": p.get("ts")} if selfcheck and p else None
    state, broke = reconcile(state)
    if broke:
        return nogo("circuit_break_consecutive_fails", selfcheck, triage)
    n = now()
    if n.date() > WINDOW_END:
        return nogo("window_expired", selfcheck, triage)
    if PAUSED.exists():
        return nogo("paused", selfcheck, triage)
    if launches_today(n.date().isoformat(), exclude=self_excl) >= BUDGET_PER_DAY:
        return nogo("budget_exhausted", selfcheck, triage)
    if triage and triage_launches_today(n.date().isoformat(), exclude=self_excl) >= TRIAGE_PER_DAY:
        return nogo("triage_budget_exhausted", selfcheck, triage)
    print("GO"); return 0

def commit(task_id, reason):
    ts = now().isoformat()   # 单次取时间戳：pending 与台账必须同值，否则 selfcheck 排除失效
    state = load_state()
    state["pending"] = {"ts": ts, "task_id": task_id,
                        "reason": reason, "hb_lines": hb_lines()}
    save_state(state)
    append_ledger({"type": "launch", "ts": ts,
                   "task_id": task_id, "reason": reason})
    print("COMMITTED"); return 0

def refund(ref_ts, reason):
    """退回一次被浪费的拉起：记 refund 行（当日预算扣减）+ 作废对应 pending 快照。
    匹配口径=launch 记录 ts[:19] 相同（容忍微秒差）。台账只追加，历史不改写。"""
    ref_task = None
    if LEDGER.exists():
        for line in open(LEDGER):
            try: rec = json.loads(line)
            except Exception: continue
            if rec.get("type") == "launch" and str(rec.get("ts"))[:19] == str(ref_ts)[:19]:
                ref_task = rec.get("task_id")
    append_ledger({"type": "refund", "ts": now().isoformat(), "ref_ts": ref_ts,
                   "task_id": ref_task, "reason": reason})
    state = load_state()
    p = state.get("pending") or {}
    if str(p.get("ts", ""))[:19] == str(ref_ts)[:19]:
        state.pop("pending", None)
        save_state(state)
    print("REFUNDED" + (f" task={ref_task}" if ref_task else " (launch记录未找到,仅记账)"))
    return 0

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--commit":
        if len(sys.argv) < 4:
            print("usage: --commit <task_id> <reason>"); sys.exit(2)
        sys.exit(commit(sys.argv[2], sys.argv[3]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--refund":
        if len(sys.argv) < 4:
            print("usage: --refund <ref_ts> <reason>"); sys.exit(2)
        sys.exit(refund(sys.argv[2], sys.argv[3]))
    flags = set(sys.argv[1:])
    if flags - {"--selfcheck", "--triage"}:
        print("usage: [--selfcheck] [--triage] | --commit <task_id> <reason> | --refund <ref_ts> <reason>")
        sys.exit(2)
    sys.exit(check(selfcheck="--selfcheck" in flags, triage="--triage" in flags))
