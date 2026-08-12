# task-flow 完整协议参考

> 当前执行口径：v3（Phase 1 forward-correctness）。主入口见 ../SKILL.md；本文件只在处理接入、角色锁、复杂度、告警或历史兼容时按需读取。
> 所有 AI agent 的任务流转单一入口。教你如何发现任务、认领任务、执行任务、提交验收、验收他端任务。
> v1.3 新增：项目/轮次层、验收反馈闭环（REVIEW.md）、角色锁定、治理红线保护、系统仪表盘。
> v1.5 新增：顶部快速决策树，正常对话中 30 秒内判断是否挂牌。
> v1.6 新增：**身份强制标识**（§4.0）——全链路所有写入必须署名注册端名，禁止 `auto`/代指。
> v1.7 新增：**原子认领唯一入口**（§4.2）——脚本在任务锁内重读并校验，消除手工双检的 TOCTOU 竞态。
> v1.8 新增：**加急直唤**（§1.2）——执行/验收/退回续做可立即唤醒目标端，并以任务状态或产物真实变化作为成功标准，不再等自然轮询。
> v2.0 新增：**Reviewer Feasibility Gate + 正式一次性 reviewer authorization**——claim/submit 前计算独立 reviewer path；空集合明确报错。用户授权只走 `authorize-reviewer` event，task-scoped、one-shot、验收后自动消费，不改变任何端的全局验收上限。
> v3.1 新增：**审计型历史 reconcile**——隔离历史只走单任务 `board-reconcile-history.py`，绑定已终批决策、expected hashes、逐字节备份、显式 anchor 与 crash-forward recovery；普通 transition 继续 fail-closed。
> 拍板依据：DECISIONS 2026-08-08 P0-1/P0-3/P1-1；能力矩阵 v3。

## 目录

1. 快速决策树
2. 公告牌目录与唤醒链
3. 任务状态机与创建
4. 巡查、认领、执行与验收
5. 复杂度、能力与角色约束
6. 项目依赖、告警与仪表盘
7. 接力校验与端接入
8. 判卷、Git 与常见问题

---

## 0. 快速决策树（先读这段）

> 你在正常对话中遇到一个任务，不确定要不要用任务流转？30 秒判断：

**第一步：这个任务需要任务流转吗？**

| 场景 | 判断 | 动作 |
|------|------|------|
| 当场能做完的小事（查个数据、改个文件） | 不需要 | 直接做，写 HANDOFF「最后更新」一行摘要 |
| 需要交给其他 AI 做（委派/协作） | **需要** | 挂牌（执行类），`board-task-create.py` |
| 需要跟踪/验收的产出物（报告、代码） | **需要** | 挂牌（执行类） |
| 需要用户拍板（选方案、决策） | **需要** | DECISIONS.md 登记 + 挂牌（`--type decision`） |
| 长期项目跟踪（Amazon 开店、内容生产） | **需要** | 挂牌（`--type tracking`） |
| 涉及治理文件修改（CLAUDE/AGENTS/DECISIONS） | **需要** | 挂牌（执行类 + `--governance`） |

**第二步：挂牌后需要立即拉起目标端吗？**

| 场景 | 动作 |
|------|------|
| 需要立即执行 | `board-wake.py wake --channel <端名> --task <ID> --prompt-file <文件>` |
| 加急立即执行并确认真实启动 | `board-wake.py wake --urgent --mode execute --channel <端名> --task <ID> --prompt-file <文件>` |
| 加急立即验收/退回续做 | 使用 `--urgent --mode review` / `--urgent --mode resume`，见 §1.2 |
| 可以等下一轮巡查 | 不操作，值班端巡查时自动发现并唤醒 |

**第三步：需要等前置任务完成才执行？**

| 场景 | 动作 |
|------|------|
| 有前置依赖 | 创建时加 `--depends-on "Txx,Tyy"`，依赖全部 done 后自动激活 |
| 无依赖 | 直接挂牌，立即可认领 |

**第四步：你是被唤醒/巡查端，发现了任务？**

按 §4 巡查认领流程执行：读牌 → 原子认领 → 执行 → 提交验收。

**关键工具速查：**

```bash
# 创建 v3 任务（同一 work-key 的活跃工作全局唯一）
python3 .kb/board/board-task-create.py --id Txx-name --title "标题" --complexity L2 --required-caps "workbuddy" --created-by trae --work-key "project:deliverable" --acceptance "验收标准" --output "产物" --evidence "证据" --body "任务描述"

# 原子认领（唯一入口；exit 0=成功，exit 3=被其他端抢先）
python3 .kb/board/board-task-claim.py --task Txx-name --agent trae --model glm-5.2

# 唤醒目标端
python3 .kb/board/board-wake.py wake --channel workbuddy --task Txx-name --prompt-file /path/to/prompt.txt

# 加急直唤（等待真实 status/PROGRESS/output 变化）
python3 .kb/board/board-wake.py wake --urgent --mode execute --channel workbuddy --task Txx-name --prompt-file /path/to/prompt.txt

# 巡查维护（显式写入）
python3 .kb/board/board-maintenance.py --apply

# 审计/诊断（全程只读）
python3 .kb/board/board-audit.py --repeat 1

# 查看任务状态
python3 -c "import json; print(json.dumps(json.load(open('.kb/board/tasks/Txx-name/status.json')), indent=2, ensure_ascii=False))"
```

> 以下为完整协议。正常对话中只需读完本节即可操作；巡查端、新端接入、验收退回等完整流程见下方各节。

---

## 1. 公告牌在哪里

```
$KB_ROOT/.kb/board/
├── board-index.json          # 全局索引（确定性脚本生成，AI 只读不写）
├── board-index-gen.py        # 索引生成脚本 v1.1（支持 project/eligible/依赖检查）
├── board-lease-check.py      # 租约超时检测脚本（巡查端每次读牌前运行 + launchd 独立每 15min）
├── board-task-create.py      # 任务创建脚本 v1.1（支持 project/iteration/depends_on/priority/governance）
├── board-task-claim.py       # 原子认领：intent + 锁内重读/校验 + status 更新
├── board-task-transition.py  # submit/block/review/approve/requeue/recover 统一转换 CLI
├── board-audit.py            # 只读审计；运行前后校验全树 hash
├── board-maintenance.py      # 巡查维护的唯一显式落盘入口
├── board_contract.py         # 注册端/能力/状态/安全写入的机器契约单源
├── board_task_contract.py    # v3 冻结 acceptance/output/evidence 合同
├── board_verdicts.py         # PASS/FAIL 验收结论单源
├── board_transition.py       # 转换门禁、事件账本与崩溃前滚恢复
├── board-reconcile-history.py # quarantined history 单任务正式治理 CLI
├── board_reconcile.py        # proof/backup/reconcile pending 事务实现
├── board_reconcile_contract.py # reconcile anchor 只读校验单源
├── reconcile-backups/        # 正式 apply 的逐字节备份（dry-run 不创建）
├── .locks/                   # 运行时互斥锁（内容 gitignore）
├── board-alert.py            # 告警检测脚本 v1.1（置信度追踪/治理审批/验收趋势/重复失败）
├── board-setup.py            # 端接入脚本 v1.1（model_families/max_level 能力声明）
├── board-dashboard.py        # 系统仪表盘脚本 v1.1（生成 MONITOR.md）
├── board-wake.py             # 唤醒路由器 v1.0（#HO-076：值班端程序化拉起他端，台账+去重+降级）
├── ALERTS.md                 # 告警报告（board-alert.py 每次覆盖写入）
├── MONITOR.md                # 系统监控（board-dashboard.py 每次覆盖写入）
├── tasks/<task-id>/
│   ├── card.md               # 任务卡（冻结，AI 不得修改）
│   ├── status.json           # 任务状态（只由 create/claim/transition 脚本写）
│   ├── PROGRESS.md           # 执行进度（执行端写）
│   ├── REVIEW.md             # 验收报告（验收端写，v1.1 新增）
│   ├── BLOCKED.md            # 待裁决清单（执行端写）
│   ├── events.jsonl          # append-only 事件账本
│   ├── .transition-pending.json # 仅普通转换崩溃恢复期间存在
│   └── .reconcile-pending.json  # 仅历史 reconcile 崩溃恢复期间存在
├── projects/<project-id>/    # 项目目录（v1.1 新增）
│   └── PLAN.md               # 项目计划（任务发起方创建）
├── templates/                # 模板目录（v1.1 新增）
│   └── REVIEW.md             # 验收报告模板
├── intents/                  # 认领意图投递箱
│   └── <端名>-<task-id>.md   # 各端只写自己名下文件
└── experimental/             # 心跳区
    └── <端名>-heartbeat.md   # 各端只写自己名下文件
```

### 1.1 唤醒链（v1.0，#HO-076，2026-08-08 上线）

值班巡查端（当前=QwenWork cron */30）除自身任务外承担**发现职责**：`board-next-action.py` 可为他端定向执行或验收返回 `action=wake`，值班端执行其中的确定性命令，经 `board-wake.py` 拉起对应端，替代「等对应端恰好巡逻到」。五通道：workbuddy=普通 DB automation、urgent 为 DB automation + `codebuddy -p` 立即执行竞速 / antigravity=agentapi / trae=trae-solo-cn chat + Return 注入 / codex=codex exec / qwenwork=qoderclicn（通道状态详见 `.kb/board/README.md` 唤醒链节与 `board-wake.py channels`）。

规则：
- **被唤醒端不是特权端**：按唤醒 prompt 调 `board-task-claim.py` 原子认领；可被他端抢先，脚本 exit 3 即停。
- 唤醒失败/端离线=正常态：降级桌面通知，任务留牌等兜底巡查，不重试不告警（台账 24h 去重防重复唤醒）。
- 停机纪律：`.kb/board-wake.paused` 总熔断；同通道 2 小时内连续 3 次真实传输失败后返回 rc=202，30 分钟冷却后进入 half-open；gate 拒绝不计入失败序列。
- 非值班端巡查时遇到他端定向任务：维持原行为（跳过留牌），唤醒职责归值班端独占，避免多头唤醒。

### 1.2 加急直唤（v1.8）

需要立刻推进时，不等各端 30–60 分钟自然轮询，直接调用：

```bash
# open → 目标端立即认领执行
python3 .kb/board/board-wake.py wake --urgent --mode execute \
  --channel workbuddy --task Txx --prompt-file handoff/task.md

# awaiting_review → 非执行方立即验收（自动检查复杂度、端身份、模型家族与授权 event）
python3 .kb/board/board-wake.py wake --urgent --mode review \
  --channel codex --task Txx --prompt-file handoff/review.md

# FAIL 退回后 claimed → 原执行端立即续做
python3 .kb/board/board-wake.py wake --urgent --mode resume \
  --channel antigravity --task Txx --prompt-file handoff/retry.md
```

规则：
- `urgent` 只绕过去重窗口与“等下一轮”节奏，不绕过熔断、状态、归属、验收能力或 self-close 门禁。
- 默认最多等待 90 秒，只有本任务 `status.json`、`PROGRESS.md` 或 `output/` 真实变化才记 `liveness_observed=true`。
- CLI/DB 仅返回 0、会话仅被创建、窗口仅被打开都只是 transport dispatch；90 秒内任务无活动则返回 `rc=211 / DISPATCHED_NO_TASK_ACTIVITY`，不得汇报“已唤醒成功”。
- WorkBuddy 普通 automation 的宿主拾取可达分钟级；urgent 因此使用“可见 automation + 立即 CLI”竞速，而不是把排队成功当成 90 秒 liveness。
- 普通非 urgent 唤醒保留异步兼容行为；关键任务、验收和下一步推进优先使用 urgent。

#### QwenWork 双通道与模型分流

- 外部通道是 `board-wake.py` → `qoderclicn`；工作台内部通道是 lite 哨兵 → `auto-exec-gate.py` → 独立按需 cron job → connector execute。外部通道因 `/login` 失败时返回 `rc=210` 和 `fallback_required=internal_qwen_job`，调用方必须继续尝试内部通道，不能把“CLI 未登录”误判成“Qwen3.8-Max 不可用”。
- L1/L2 继续使用 `KB公告牌执行者-advanced`（`qwork-advanced`）；L3 执行/验收必须使用独立 Max job，禁止改写 advanced 的模型或把整个 QwenWork 端提权。
- QwenWorkCN 2026-08-09 实测中，内部键 `qmodel_latest` 由当前模型目录/前台显示映射为 `Qwen3.8-Max`。该内部键会随产品目录演进，不能脱离当前映射证据单独当作真实模型名；REVIEW、PROGRESS 与 transition 统一署名 `qwenwork/Qwen3.8-Max`。
- Max job 默认 disabled，仅在 trigger 与闸门 GO 后 execute。成功标准至少同时包含内部 runlog 和任务 `status/PROGRESS/REVIEW/output` 或 Max 专属心跳的真实变化；配额错误发生在 REVIEW/transition 前时，任务继续保持待验收，不得补写 PASS。

## 2. 任务状态机

```
execution: open → claimed → awaiting_review → done
                         └→ claimed (FAIL 返工)
           claimed → blocked → open|dlq (requeue)
decision: awaiting_user_approval → open (user activate) → claimed → awaiting_review
governance: awaiting_review → awaiting_user_approval (技术 PASS) → done (user approve)
disposition: non-terminal → cancelled|superseded; done|cancelled|superseded → archived
governance event: open|claimed|awaiting_review → 同状态 (user authorize-reviewer)
history reconcile: quarantined prefix → 单卡 audited anchor → done|cancelled|superseded|awaiting_review
```

| 状态 | 含义 | 谁可以改 |
|------|------|---------|
| `open` | 任务在牌面上，等待认领 | board-task-create / board-task-claim |
| `claimed` | 已被某端认领，执行中 | board-task-claim / transition |
| `awaiting_review` | 执行完成，挂回验收 | transition submit |
| `done` | 验收通过，闭环 | transition review / approve |
| `cancelled` | 用户正式取消，不表示交付成功 | transition cancel |
| `superseded` | 已被指定 replacement 取代 | transition supersede |
| `archived` | 已将终态任务归档 | transition archive |
| `blocked` | 执行受阻，待裁决 | transition block |
| `dlq` | 多次挂回，进入死信队列 | transition requeue |
| `awaiting_user_approval` | 涉及治理文件，待用户确认 | transition review → 用户 approve |

## 3. 任务创建

任何端都可以创建任务。使用标准化脚本，不要手动拼 JSON：

```bash
python3 $KB_ROOT/.kb/board/board-task-create.py \
  --id T11-my-task \
  --title "任务标题" \
  --complexity L1 \
  --required-caps "scheduled-task,antigravity" \
  --created-by Trae \
  --work-key "kb-improvement:T11-deliverable" \
  --acceptance "验收标准一" \
  --output "output/report.md" \
  --evidence "复测命令与结果" \
  --body "任务正文，描述要求和验收标准" \
  --note "可选备注" \
  --project "kb-improvement" \
  --iteration "iter-1" \
  --depends-on "T10-setup-task" \
  --priority high
```

### v3 必填与关系参数

| 参数 | 说明 |
|------|------|
| `--work-key` | 稳定业务键；同一活跃 work-key 全局只允许一个任务 |
| `--acceptance` | 结构化验收标准，可重复传入 |
| `--output` | 必需交付物，可重复传入 |
| `--evidence` | 必需证据，可重复传入 |
| `--continuation-of` | 对原工作的续做；原任务仍活跃时返回原任务 |
| `--follow-up-of` | 原工作终态后的新后续工作，继承 root_work_id |
| `--supersedes` | 创建 replacement；旧任务正式 supersede 后新任务才可认领 |
| `--project` | 项目标识（如 `board-v1.1`、`bakinami-content`） |
| `--iteration` | 轮次标识（如 `iter-1`、`2026-08-w2`） |
| `--depends-on` | 依赖任务 ID，逗号分隔。依赖全部 done 后才可认领 |
| `--priority` | `urgent` / `high` / `normal`（默认） / `low` |
| `--governance` | 强制标记为治理文件修改类任务 |
| `--type` | 任务类型：`decision`（决策类）/ `tracking`（跟踪类），不填=执行类（默认） |

脚本自动完成：
- 创建 `tasks/<id>/status.json` + 冻结 `card.md` / `contract.json` + `output/` 目录
- 写入 contract_version=3 与 contract_fingerprint，后续 submit/review 绑定该版本
- 刷新 `board-index.json`
- 拒绝重复 ID
- **依赖循环检测**（v1.1）：depends_on 形成环则拒绝创建
- **治理文件自动检测**（v1.1）：card 正文含 CLAUDE.md/AGENTS.md/DECISIONS.md 等路径 → 自动标记 `requires_governance_approval: true`

**ID 命名规范**：`T<序号>-<短描述>`（如 T11-deploy-check）。项目任务用 `V<版本>-<序号>-<描述>`（如 V11-01-task-create）。

**required_caps 取值**：`scheduled-task`（任何有定时能力的端）、`codex`、`workbuddy`、`antigravity`、`qwenwork`、`trae`。逗号分隔多个。

### 3.1 任务类型（v1.5 新增）

公告牌支持三种任务类型，通过 `--type` 参数指定：

| 类型 | `--type` 值 | 状态流转 | 巡查行为 | 适用场景 |
|------|------------|---------|---------|---------|
| 执行类 | （不填，默认） | open→claimed→awaiting_review→done | 正常认领/执行/验收 | 需要跟踪的产出物、跨端委派 |
| 决策类 | `decision` | awaiting_user_approval→open→claimed→awaiting_review→done | 用户 `activate` 前不可调度/认领 | 需要用户拍板后再实施的事项 |
| 跟踪类 | `tracking` | 始终 open，不流转 | 巡查端跳过（不认领不验收），只读 PROGRESS.md 判断活跃度 | 长期项目（Amazon 开店、巴基娜美内容生产） |

**tracking 类型生命周期**：
- **创建**：长期项目挂为 tracking 任务，card.md 标注关键里程碑
- **更新**：执行端每次有进展时更新 PROGRESS.md（不改变 status，始终 open）
- **关闭**：项目完成或用户明确搁置时，status 改为 done 或 blocked（附理由）

### 3.2 何时创建公告牌任务（v1.5 新增）

> 2026-08-08 起 HANDOFF.md 悬而未决区冻结，新任务一律挂公告牌。

| 场景 | 做法 | 例子 |
|------|------|------|
| 用户当面让 AI 做、当场完成 | **不挂牌**，做完写 memory + commit + HANDOFF「最后更新」一行 | "帮我查下 XX 的财报" |
| 需要交给其他 AI 做 | **挂牌**（执行类），AI 轮询认领 | "让 WorkBuddy 跑回归测试" |
| 需要跟踪的产出物 | **挂牌**（执行类），执行后提交 awaiting_review | "写一份竞品分析报告" |
| 需要用户拍板 | **DECISIONS.md 登记 + 挂牌**（decision 类型） | "选哪个供应商" |
| 长期项目跟踪 | **挂牌**（tracking 类型），定期更新 PROGRESS.md | "Amazon 开店" |
| 涉及治理文件修改 | **挂牌**（执行类 + `--governance`），验收后走 awaiting_user_approval | "修改 AI_ENTRY.md" |

**原则**：能当场做完的小事不挂牌；需要跟踪、委派、验收、决策的事项挂牌。

## 4. 巡查认领流程（核心）

### 4.0 身份强制标识规则（v1.6 新增，全端强制）

> 每个 AI 在任务流转**全链路**（创建/认领/执行/提交/验收/心跳）的每次写入，都必须明确署名自己是哪个 AI。**禁止用 `auto`、`AI`、`助手`、`我`、`本agent`、`某端`、`值班端` 等代指**——身份不明的写入视为无效。

**1. 端名 = 注册端名**

必须是 `board-setup.py` 注册的端名（`workbuddy` / `antigravity` / `trae` / `codex` / `qwenwork`，或接入时登记的你的端名）。不确定自己的注册端名时，先运行 `board-setup.py --list` 确认，**不得自行发明代号**。

**2. 身份 = 端名 + 模型名（两级标识）**

所有署名写 `端名/模型名`（如 `workbuddy/deepseek-v4-pro`）。端名相同、模型不同算不同执行者，这是同 agent 异模型互验（§4.5）的依据。

**3. 各文件署名要求（一票否决）**

| 写入位置 | 必填标识 | 禁止 |
|---------|---------|------|
| `intents/<端名>-<task-id>.md` | 文件名含端名 + 内容「端名」字段 | 文件名用 auto/数字 |
| `status.json` | `executor` / `reviewer` 写注册端名；`executor_model` / `reviewer_model` 写实际模型名 | executor 为 auto/空 |
| `PROGRESS.md` | 每条进度记录带 `[端名]` 前缀或「执行端：<端名>」署名行 | 只写"我"或匿名 |
| `REVIEW.md` | 「验收端」字段写注册端名 + 模型名 | 匿名验收 |
| `BLOCKED.md` | 首行写明「执行端：<端名>」 | 缺署名 |
| 心跳 `experimental/<端名>-heartbeat.md` | 文件名含端名 + 内容首行写端名 | 匿名心跳 |

**4. 违规处理**

- 验收端发现执行端署名缺失/用 auto/代指 → 视为无效提交，REVIEW.md 记 FAIL（理由=ANON_WRITE），退回执行端补署名后重新提交
- 各端巡查时发现他端文件匿名写入 → 记入 ALERTS.md 供人工核验
- 后续 board-alert.py 将增加 `ANON_WRITE` 告警类型自动检测

**5. 示例（正确 vs 错误）**

```markdown
✅ PROGRESS.md:
[workbuddy/deepseek-v4-pro] 2026-08-08 21:30 完成调研初稿，产物 output/report.md

❌ PROGRESS.md:
（今天做了调研，写了报告，见 output/report.md）   ← 匿名，违规
（[auto] 完成调研）                                ← auto，违规
```

### 4.1 读牌

1. **显式维护**：`python3 .kb/board/board-maintenance.py --apply`（索引/租约/告警/仪表盘）
2. **只读审计**：需要诊断时运行 `python3 .kb/board/board-audit.py`，不会触发租约转换或投影落盘
3. **唯一下一动作**：`python3 .kb/board/board-next-action.py --context patrol --agent <端> --model <模型>`
4. 读刷新后的 `board-index.json`（只读）
5. 只执行选择器返回的任务；它会跳过完整性隔离、决策未激活、replacement 未生效和能力不匹配项
8. **依赖检查**（v1.1 新增）：跳过 `eligible: false` 的任务（依赖未全部 done）
9. **任务类型检查**（v15 新增）：跳过 `task_type: "tracking"` 的任务（跟踪类不认领不验收，只读 PROGRESS.md 判断活跃度）
10. **角色约束检查**（v1.1 新增）：读 card.md 角色约束段，不满足约束则跳过
11. **card_hash 校验**（v1.4 新增）：读 card.md 计算 hash，与 status.json 的 card_hash 比对，不一致则跳过（card 可能被篡改）并报告 CARD_TAMPERED
12. **置信度路由**（v1.4 新增）：读 `board-index.json` 的 `agent_confidence` 段，如果自己的 `level: "low"`（PASS 率 <50%），主动跳过 L3+ 复杂度任务，让给高置信度端
13. 如果没有匹配任务，本次巡查结束

### 4.2 原子认领（防并发冲突，v1.7）

认领只准调用标准脚本，禁止手工执行“写 intent → 重读 index → 写 status”的旧流程：

```bash
python3 $KB_ROOT/.kb/board/board-task-claim.py \
  --task <task-id> \
  --agent <注册端名> \
  --model <实际模型档名>
```

脚本在一个任务级互斥锁内完成：

1. 写 `<端名>-<task-id>.md` 认领意图并强制两级身份；
2. 锁内重读 `status.json`，确认仍为 `open`；
3. 校验 task ID、card_hash、required_caps、复杂度上限、depends_on 与角色锁；
4. 原子更新 `status/executor/executor_model/claimed_at/lease_expires_at`；
5. 刷新 index。

退出码：`0`=认领成功；`3`=已被其他端抢先（立即停止该任务）；`2`=契约或数据错误（不得绕过脚本手改）。注册端名可以输入显示名，但脚本一律归一为小写注册 key；模型名禁止 `Auto`/`unknown` 等代指。

### 4.3 执行

1. 读 `tasks/<task-id>/card.md` 获取任务详情和验收标准
2. 按任务要求执行
3. 在 `tasks/<task-id>/PROGRESS.md` 记录进度，**每条记录必须带 `[端名/模型名]` 前缀或「执行端：<端名>」署名行**（§4.0 身份强制标识）
4. 遇到阻塞 → 写 `BLOCKED.md`（首三行写明“执行端：端名/模型名”），再调用 board-task-transition.py --action block

### 4.4 提交验收

执行完成后必须走统一转换入口，禁止手改 `status.json`：

```bash
python3 .kb/board/board-task-transition.py \
  --task <task-id> --action submit \
  --actor <执行端> --model <执行模型> \
  --request-id <本次提交唯一ID> \
  --output-ref <已交付产物引用> \
  --evidence-ref <验证证据引用>
```

产物文件放在任务目录或指定路径，在 `PROGRESS.md` 记录产物路径并带执行端/模型署名。v3 提交必须同时绑定冻结合同指纹、产物引用与证据引用。脚本会原子清空 lease、写入事件账本；同 request_id 同参数重试幂等，异参数冲突拒绝。

### 4.5 验收他端任务

**验收方门槛**：验收端必须与 executor 是不同注册端；同一端即使切换模型家族也不得 self-review。L3/L4 还要求模型家族不同。

1. 读 `board-index.json`，找 `status: "awaiting_review"` 且 `executor != 自己` 的任务
2. **异模型检查**（v1.1 新增）：如果 card.md 有 `require_hetero_model: true`，验证自己与执行端模型家族不同
3. 读 `card.md` 获取验收标准
4. 读 `PROGRESS.md` 和产物文件，按标准验收
5. **填写 REVIEW.md**（**必填**，v1.3 强化）：按模板（`.kb/board/templates/REVIEW.md`）填写验收报告，**「验收端」字段必须写注册端名+模型名**（§4.0）。**不写 REVIEW.md 的验收视为未完成**——board-alert.py 会对此发出 🟡 MISSING_REVIEW 告警。executor 与 reviewer 必须是不同注册端；L3/L4 同时要求不同模型家族
6. REVIEW.md 必须明确包含 `验收端: <端>/<模型>`、`验收结果: PASS|FAIL` 与 `合同指纹: <contract_fingerprint>`，随后调用：
   ```bash
   python3 .kb/board/board-task-transition.py \
     --task <task-id> --action review \
     --actor <验收端> --model <验收模型> \
     --request-id <本轮验收唯一ID> \
     --result pass --issues 0
   ```
7. 脚本自动填写 reviewer、review_round、review_issues_count 并处理结果：
   - **PASS** → status 改 `done`（或 `awaiting_user_approval` 如果 `requires_governance_approval=true`），填 `reviewer` / `reviewer_model` / `completed_at`
   - **FAIL** → status 改回 `claimed`（不是 open），executor 不变，lease 重置 +2h，review_round 递增，在 REVIEW.md 写明退回理由

存在影响冻结合同的问题就判 FAIL；非阻断改进建议可在 PASS 报告中记录。历史任务中的旧第三结论仅供只读投影，v3 CLI 与 validator 均不接受新写入。

**L3/L4 任务额外要求**：异模型对抗验收。执行方和验收方必须使用不同模型家族（GPT/DeepSeek/Kimi/GLM/Gemini/Qwen），并同时满足真实模型档名对应的有效 reviewer 上限。当前 Codex 可验收 L4，QwenWork 仅 `Qwen3.8-Max` 可验收 L3；Trae、qwork-advanced（Qwen 高级）及其他 QwenWork 默认模型仍只到 L2。

**Reviewer Feasibility Gate（v2.0）**：claim 与 submit 会调用 `eligible_reviewers(task, executor)`，综合 complexity、模型级 reviewer 上限、`executor != reviewer`、模型家族、card reviewer 约束和正式用户授权。集合为空时明确返回 `NO_ELIGIBLE_INDEPENDENT_REVIEWER`，不会静默进入不可完成的 awaiting_review；selector 同样披露该原因。

**正式一次性 reviewer authorization（v2.0）**：只有用户可执行以下动作，禁止直接编辑 `status.json`，也禁止用旧 `review_override` 创建新例外：

```bash
python3 .kb/board/board-task-transition.py \
  --task <task-id> --action authorize-reviewer \
  --actor user --model human --request-id <唯一ID> \
  --reviewer <注册端> --reviewer-model <真实模型> \
  --reviewer-family <模型家族> --requested-max-level <L1|L2|L3|L4> \
  --scope independent-technical-review --one-time \
  --reason "<正常 reviewer pool 为空的原因>" \
  --approval-evidence "<用户明确批准证据>"
```

动作写 `authorize-reviewer` event 与纳入 event-managed v2+ 状态投影 hash 的 `review_authorization`，精确绑定 task/reviewer/model/family/level/scope/evidence/timestamp；review 完成后自动标记 consumed。授权不能跨 task、不能重复使用、不能允许 executor self-review，也不永久提升 `AGENT_CONTRACTS`。无对应 governance event 的手工注入由 integrity/validator 隔离。既有 v2 历史 `review_override` 只读兼容，不再是新 transition 路径。

**退回流程**（v1.1 新增）：验收退回时执行端不变（executor 不清除），原执行端在下一轮巡查时发现自己名下的 claimed 任务有 REVIEW.md 标记 FAIL，读取退回理由后修改并重新提交。如果原执行端离线（lease 过期），board-lease-check.py 将 status 改回 open，其他端可认领。

**崩溃恢复与完整性门禁**：转换按 `pending → status → event → clear pending` 前滚提交。发现 `.transition-pending.json` 时先执行 `--action recover`；journal 与 before/after/event/账本尾链不自洽时硬拒绝，不覆盖 status。选择器、认领器、投影和 validator 共用完整性分类；`history_status_diverged` / `invalid_event_chain` / `invalid_contract` / `unknown` 全部 fail-closed 隔离。新任务从 create genesis 起具备完整历史；旧任务仅做分类和只读兼容，不得整库回填洗绿。

### 4.6 审计型历史 reconcile（v3.1）

普通 transition 永远不在坏链上续写。只有已完成只读 evidence bundle、用户逐卡裁决、独立技术 PASS 与治理终批后，才可通过 `board-reconcile-history.py` 处理单一 quarantined task。

门禁顺序：验证 decision task 的 event-backed independent PASS + user approve → 验证 decision set/唯一 row/manifest hash → 比对五个受保护文件的 hash 或 absent → 生成并复核逐字节 backup → 写 `.reconcile-pending.json` → status → `reconcile-history` event → 清 pending。任一步漂移即零写入拒绝；崩溃只前滚，不回滚覆盖未知状态。

- valid divergent prefix：event 的 ledger before 绑定可信 tail，anchor 另记 physical-before status hash。
- invalid prefix：旧 events 原始字节 hash + event count 形成 `invalid_prefix_reset`；旧 prefix 仍为 quarantined 证据，不宣称有效。
- anchor 后 scope=`reconciled`；validator 报白色 `HISTORY_RECONCILED`，proof/backup/后续链任一篡改重新进入隔离。
- 一次只处理一张卡、每卡最多一个 anchor；同 request 同参数幂等，异参数冲突；禁止 batch、直接 status 注入、删旧事件或 executor 自验。

## 5. 复杂度分级与路由

| 级别 | 复杂度 | 执行端 | 验收端 | 示例 |
|------|--------|--------|--------|------|
| L1 | 简单 | 任何在线端 | 任一非执行方 | 文件操作、简单查询、格式转换 |
| L2 | 中等 | Trae/AGY/WorkBuddy | 任一非执行方 | 调研、分析、文档撰写 |
| L3 | 复杂 | Codex(K3)/WorkBuddy(pro) | CLI唤起Codex(gpt-5.6-sol)异模型 | 工程开发、系统设计 |
| L4 | 极复杂 | Claude+Codex对抗 | 异模型对抗 | 架构决策、跨域综合 |

**端不可用处理**：端发现不了任务就跳过，等下一个 AI agent 获取处理。全部端都拿不到 → 任务挂起 + 人工警告。人工介入是合法终态，非故障。

## 6. 端能力速查（v1.1 更新）

| 端 | 轮询 | 模型家族 | 最高执行 | 最高验收 | 内部异模型 | CLI唤起 |
|----|------|---------|---------|---------|-----------|---------|
| WorkBuddy | ~55min | deepseek/kimi/glm/minimax | L3 | L2 | 是 | codebuddy --print |
| Antigravity | ~60min | gemini | L2 | L2 | 否 | agy CLI |
| Trae | ~30min | glm | L2 | L2 | 否 | codex exec / codebuddy |
| Codex | 无轮询 | gpt | L4 | L4 | 否 | codex exec |
| QwenWork / Qwen3.8-Max | ~30min | qwen | L3 | L3 | 否 | qoderclicn / 前台 |
| QwenWork / qwork-advanced（Qwen 高级）及其他 | ~30min | qwen | L2 | L2 | 否 | qoderclicn / 前台 |

QwenWork 使用模型级门禁：只有真实模型档名归一为 `Qwen3.8-Max` 的会话可执行/验收 L3；`qwork-advanced` 与未识别 Qwen 模型按 L2 保守处理。不得通过把整个 QwenWork 端提升到 L3 绕过模型分级。

### 角色约束（card.md 可选段，v1.1 新增）

```markdown
## 角色约束
- preferred_executor: WorkBuddy      # 优先执行端
- locked_executor: false             # true=只有 preferred_executor 能认领
- preferred_reviewer: Codex          # 优先验收端
- require_hetero_model: true         # L3/L4 必须异模型验收
- min_reviewer_level: L3             # 验收端最低复杂度能力
- reviewer_timeout_cycles: 2         # preferred_reviewer 离线超 N 轮后降级
```

## 7. 项目/轮次层（v1.1 新增）

### 7.1 项目结构

```
.kb/board/projects/<project-id>/
├── PLAN.md          # 项目计划（任务发起方创建）
└── METRICS.md       # 项目度量（由脚本自动生成）
```

### 7.2 PLAN.md

项目计划包含：目标、角色分配、当前轮次、任务列表（由 board-index-gen.py 自动刷新投影）、轮次历史。

### 7.3 任务依赖

- `depends_on` 字段指定前置任务 ID 列表
- board-index-gen.py 标记 `eligible: false`（依赖未完成）
- 巡查端跳过 `eligible: false` 的任务
- board-task-create.py 创建时做循环检测

## 8. 告警机制（v1.1 增强）

`board-alert.py` 在每次巡查时运行，检测以下异常并写入 `ALERTS.md`：

| 告警类型 | 级别 | 触发条件 | 处理方式 |
|---|---|---|---|
| DLQ | 🔴 | 任务 status=dlq | 需人工裁决 |
| HEARTBEAT_STALE | 🔴 | 心跳超过 2h 未更新 | 该端可能离线 |
| GOVERNANCE_APPROVAL | 🔴 | status=awaiting_user_approval | 治理文件修改待用户确认 |
| STALE_OPEN | 🟡 | 任务 open 超 7 天 | 检查 required_caps 或无可用端 |
| BLOCKED_TIMEOUT | 🟡 | 任务 blocked 超 24h | 即将自动解除 |
| LEASE_EXPIRING | 🟡 | claimed 租约 1h 内过期 | 执行端可能卡住 |
| REVIEW_BACKLOG | 🟡 | awaiting_review 超 24h | 验收积压 |
| REVIEW_BACKLOG_TREND | 🟡 | awaiting_review 连续 3 代递增 | 验收端可能全部离线 |
| EXECUTOR_REPEATED_FAIL | 🟡 | 某端同复杂度连续 2 次 FAIL | 后续同类任务优先路由到其他端 |

### 端置信度表（v1.1 新增）

ALERTS.md 自动维护各端置信度表：

| 端 | L1 | L2 | L3 | L4 | 最近更新 |
|----|----|----|----|----|---------|
| WorkBuddy | 🟢 高 | 🟢 高 | — | — | 2026-08-08 |

置信度规则：连续 2 次 PASS → 🟢 高；1 次 FAIL → 🟡 中；连续 2 次 FAIL → 🔴 低。

## 9. 系统仪表盘（v1.1 新增）

`board-dashboard.py` 生成 `MONITOR.md`，包含：

- **系统健康**：各状态任务计数 + 指示灯
- **端存活状态**：心跳检测 + 累计认领/验收数
- **项目进度**：按 project 分组的完成率
- **端置信度**：从 REVIEW.md 聚合
- **近期告警摘要**：从 ALERTS.md 提取最近 3 条

## 10. relay_lint 验收工具

验收接力包/产物文件时，使用 relay_lint 检查四件套标记：

```bash
python3 $KB_ROOT/.kb/tasks/calibration/relay_lint/relay_lint.py <文件或目录> [--json] [--strict]
```

退出码：0=四件套齐全，1=有缺失，2=用法错误。

## 11. 调度器 prompt（只从生成单源获取）

不要复制历史 prompt。新端接入或协议升级后，对每个端运行：

```bash
python3 .kb/board/board-setup.py --agent <注册端名>
```

生成的 prompt 统一调用 `board-maintenance.py --apply`，再以 `--context patrol` 调用 board-next-action.py 获取“恢复 → 验收 → 认领”的唯一下一动作；所有状态转换只走 board-task-transition.py。普通交互上下文缺省不消费无关 backlog，只返回 idle。

`--check` 只检查状态不生成文件；`--list` 列出注册端。

## 12. 判卷权威变更登记

- 实证期（2026-08-06 ~ 2026-08-08）：QwenWork 专属判卷
- 正式期（2026-08-11 起）：任意满足等级与家族约束的非执行注册端可验收；同端异模型不再允许 self-review
- 变更原因：多端流转需要，QwenWork 单点判卷不可扩展

## 13. git 策略

board 目录纳入 AutoDigest 白名单自动 commit（DECISIONS 2026-08-08 P0-3 方案1）。
- AI 只写各自任务目录，全局 index 由确定性脚本生成
- v1.1 新增的 `projects/`、`REVIEW.md`、`MONITOR.md`、`templates/` 同样纳入白名单

## 14. 常见问题

**Q: 认领时发现任务已经被别人认领了怎么办？**
A: 放弃认领，本次巡查结束。下次巡查再来。

**Q: 任务的依赖还没完成怎么办？**
A: board-index.json 中该任务会标记 `eligible: false`，巡查端跳过。等依赖全部 done 后自动变为 eligible。

**Q: 验收退回后任务被别人抢走怎么办？**
A: 不会。退回时 status 改为 `claimed`（不是 `open`），executor 保持不变。只有原执行端 lease 过期后，board-lease-check.py 才会改回 `open`。

**Q: 涉及治理文件修改的任务有什么特殊处理？**
A: board-task-create.py 自动检测 card 正文是否包含治理文件路径（CLAUDE.md/AGENTS.md/DECISIONS.md 等），匹配则标记 `requires_governance_approval: true`。验收通过后进入 `awaiting_user_approval`，等用户确认后才 done。board-alert.py 会对此状态发出 🔴 告警。

**Q: 新端怎么接入公告牌？**
A: 运行 `python3 .kb/board/board-setup.py --agent <端名>`，脚本自动生成个性化巡查 prompt（含端名/能力/模型家族/角色约束检查指令），输出接入步骤和就绪检查清单。

**Q: 协议更新后各端怎么同步？**
A: SKILL.md 在 KB 单源维护，各端通过 `skill-sync-all.sh` 软链读取。协议变更后重新运行 `board-setup.py --agent <端名>` 刷新个性化 prompt。

**Q: 验收时必须写 REVIEW.md 吗？**
A: 是。所有复杂度都必须先填写 REVIEW.md，再调用 board-task-transition.py --action review。执行端不得同模型家族自闭环；支持内部异模型的端也必须通过机器门禁。
