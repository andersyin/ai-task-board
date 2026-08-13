# .kb/board —— 公告牌任务流转中心

> 状态：Phase 1 forward-correctness v3（2026-08-11；完整性隔离、工作身份、冻结合同、完整生命周期、交互/巡查分流、只读审计）
> 治理升级 2026-08-12：workbuddy 验收 L3 + 验收冗余校验 + 超时自动升级 + 过时检测 + submit fail-fast（见下「2026-08-12 治理」节）
> 协议入口：本仓库 `docs/SKILL.md`（知识库内部通过 skill-sync-all.sh 同步到各端）
>
> **Standalone clone：** 本目录即默认 `--board-root`。create/claim/transition **不读** `KB_ROOT`。
> 只有把脚本装进 `$KB_ROOT/.kb/board` 时，`board-wake.py` / `board-lease-check-runner.sh` 才使用 `KB_ROOT`（可用 `BOARD_WAKE_KB` 覆盖）。
> 验收报告模板：`templates/REVIEW.md`。

## 目录结构

- `board-index.json` —— 全局索引（`board-index-gen.py` 确定性脚本生成，AI 只读不写）
- `board-index-gen.py` —— 索引投影；默认只读，只有 `--mode apply` 才落盘
- `board-audit.py` —— 运行只读投影/租约/告警/仪表盘/validator，并核验公告牌全树 hash 不变
- `board-replay.py` / `board_replay.py` —— 历史任务只读 replay/dry-run 与证据包导出；不包含 reconcile/apply 能力
- `board-maintenance.py` —— 巡查端唯一显式持久化入口，必须传 `--apply`
- `board-review-escalate.py` —— 验收超时自动升级处置（P0-2，2026-08-12 治理新增）
- `board-task-claim.py` —— 原子认领唯一入口（intent + 锁内重读/校验 + status 更新）
- `board-task-transition.py` —— activate/submit/block/review/approve/requeue/cancel/supersede/archive/recover 统一 CLI
- `board-next-action.py` —— 只读选择单一下一动作；`interactive` 不消费无关 backlog，`patrol` 才执行队列调度
- `board_contract.py` —— 注册端、能力等级、状态枚举与原子写入的机器契约单源
- `board_task_contract.py` —— v3 acceptance/output/evidence 合同校验与指纹绑定
- `board_verdicts.py` —— PASS/FAIL 结论单源；旧第三结论仅只读兼容
- `board_transition.py` —— 转换门禁、任务锁、事件 hash 链与 pending 前滚恢复
- `tasks/<id>/card.md` —— 任务卡（冻结，任何端不得修改）
- `tasks/<id>/contract.json` —— v3 冻结任务合同（验收标准/产物/证据）
- `tasks/<id>/status.json` —— 任务状态（只由 create/claim/transition 脚本写，AI 禁止直接编辑）
- `tasks/<id>/PROGRESS.md` / `BLOCKED.md` —— 执行端进度与待裁决清单（执行端写）
- `tasks/<id>/events.jsonl` —— append-only 事件账本；新任务从 create 完整可回放，旧任务从首次 bootstrap 起部分可回放
- `tasks/<id>/.transition-pending.json` —— 崩溃恢复 journal；正常完成后不存在
- `templates/REVIEW.md` —— 验收报告模板；review 前复制到任务目录并填顶格字段
- `intents/` —— 认领意图投递箱（各端只写自己名下文件 `<端名>-<taskid>.md`）
- `experimental/` —— 心跳区（各端只写自己名下 `<端名>-heartbeat.md`）
- `board-wake.py` —— 唤醒路由器 v1.0（#HO-076，确定性脚本，值班端调用，AI 不手写唤醒逻辑）


## 字段口径

- `executor_model` / `reviewer_model`：必须填真实模型档名（宿主会话 runtime-config 的 model 字段或 cron job 的 model 配置），不接受自拟标签。例：`Qwen3.8-Max`、`qwork-advanced`、`glm-5.2`、`deepseek-v4-flash`。该字段既是异模型验收证据，也是模型级能力门禁输入：Qwen3.8-Max 可执行/验收 L3，qwork-advanced（Qwen 高级）及其他 QwenWork 模型默认 L2。

## 标准状态转换

除 create、claim 与已隔离历史的专用 reconcile 外，状态变化统一走 `board-task-transition.py`，每次请求都提供唯一 `request_id`。同一 ID 同参数重试幂等成功；同一 ID 不同参数硬拒绝。`history_status_diverged` / `invalid_event_chain` 不得放宽普通 transition，必须走下文独立的 `board-reconcile-history.py`。

```bash
# 创建 v3 任务（work_key 活跃唯一）
python3 .kb/board/board-task-create.py --id Txx --title "标题" \
  --complexity L2 --required-caps scheduled-task --created-by trae \
  --work-key "project:deliverable" --acceptance "可验证标准" \
  --output "output/report.md" --evidence "复测结果" --body "任务正文"

# 执行完成：claimed → awaiting_review，绑定产物/证据与冻结合同
python3 .kb/board/board-task-transition.py --task Txx --action submit \
  --actor trae --model glm-5.2 --request-id Txx-submit-20260811-01 \
  --output-ref tasks/Txx/output/report.md --evidence-ref test:Txx

# 正常 reviewer pool 为空时：仅由用户签发 task-scoped one-shot 授权
python3 .kb/board/board-task-transition.py --task Txx --action authorize-reviewer \
  --actor user --model human --request-id Txx-auth-20260811-01 \
  --reviewer trae --reviewer-model glm-5.2 --reviewer-family glm \
  --requested-max-level L4 --scope independent-technical-review --one-time \
  --reason "no native independent L4 reviewer" --approval-evidence "user approval"

# 正式验收：awaiting_review → done / awaiting_user_approval / claimed
python3 .kb/board/board-task-transition.py --task Txx --action review \
  --actor workbuddy --model deepseek-v4-flash --request-id Txx-review-20260811-01 \
  --result pass --issues 0

# 显式恢复未完成 journal
python3 .kb/board/board-task-transition.py --task Txx --action recover
```

claim/submit 会先计算合法 independent reviewer path；空集合返回 `NO_ELIGIBLE_INDEPENDENT_REVIEWER`。`submit` 要求 PROGRESS 署名与 v3 产物/证据引用；`block` 要求 BLOCKED 前三行署名；`review` 只接受 PASS/FAIL，且 REVIEW.md 必须精确绑定验收端、模型、结论和冻结 contract_fingerprint。跨文件提交顺序为 `pending → status → event → clear pending`，任何崩溃点由下一次命令或 `recover` 前滚；损坏或不自洽的 journal 在写 status 前拒绝。

## 调度与运维上下文

```bash
# 普通用户对话：无指定任务时返回 idle
python3 .kb/board/board-next-action.py --context interactive --agent codex --model gpt-5.6-sol

# 后台巡查：允许从 backlog 选动作
python3 .kb/board/board-next-action.py --context patrol --agent trae --model glm-5.2

# 只读审计 / 显式维护
python3 .kb/board/board-audit.py --repeat 10
python3 .kb/board/board-maintenance.py --apply
```

## 历史只读 replay

`board-replay.py` 仅分析 `status.json` 当前投影、event chain 和 event tail；不自动认定任何一方为权威。重建不唯一或链损坏时输出 `needs_human_decision`，候选结果不排序、不推荐。

```bash
# 默认只向 stdout 输出机器可读分析，零写入
python3 .kb/board/board-replay.py --task Txx --pretty

# 只有显式指定时才向独立目录导出原始字节快照
python3 .kb/board/board-replay.py --task Txx --bundle-dir /path/to/evidence
```

证据导出在临时目录中验证源 hash 后原子落盘；已存在的 bundle 拒绝覆盖，目标不得位于源 task 目录内。历史裁决与 reconcile apply 必须使用后续独立治理任务，不得把本工具当修复命令。

## 历史正式 reconcile

`board-reconcile-history.py` 是 quarantined history 的唯一写入口；一次只接受一个 task，普通任务与批量参数均拒绝。它要求：已独立 PASS 且用户终批的决策任务、冻结 decision set、逐卡 adjudication、T59 evidence manifest、五个受保护文件的 hash/absence 预期，以及本次 user/human approval evidence。

```bash
# 只读预演：验证所有 proof/expected-hash，不创建 backup/pending/event
python3 .kb/board/board-reconcile-history.py --task Txx --dry-run \
  --decision-file .kb/board/tasks/T61.../output/adjudications.json \
  --decision-set-id <decision-set-id> \
  --evidence-manifest .kb/board/tasks/T59.../output/evidence_bundles/Txx/manifest.json \
  --actor user --model human --request-id <unique-id> \
  --approval-evidence "<用户对该 apply 的明确批准>"

# 正式单卡 apply（参数同上，把 --dry-run 改为 --apply）
# 崩溃恢复只需 task；selector 会优先返回该命令
python3 .kb/board/board-reconcile-history.py --task Txx --recover
```

apply 写前在 `reconcile-backups/<task>/<request>/` 生成并逐字节复核备份，再以 `.reconcile-pending.json → status → reconcile-history event → clear pending` 前滚。有效分叉从可信 event tail 桥接；无效链用原始 events 前缀 SHA-256 建立 `invalid_prefix_reset` anchor。旧事件永不删除或改写，integrity 只把 anchor 之后标为 `reconciled_history`；decision/evidence/backup 任一 proof 漂移都会重新隔离。机制验收与用户终批前不得把 dry-run 扩成生产 apply。

## 生命周期与工作身份

- decision 创建后是 `awaiting_user_approval` / `decision_pending`，必须用户 `activate` 后才能调度。
- 治理实施的技术 PASS 进入 `awaiting_user_approval`，用户终批使用 `approve`；与实施前 `activate` 不混用。
- `cancelled` / `superseded` / `archived` 是正式可解释终态；归档只能从 done/cancelled/superseded 进入。
- `work_key` 标识工作语义，`task_id` 标识记录；continuation/follow-up/supersede 共用 `root_work_id` 表达谱系。
- replacement 在旧任务被正式 supersede 前不可认领，避免新旧工作并行。

历史口径：升级后由 create 生成 genesis 的任务显示 `HISTORY_FULL`；旧任务首次采用时写 bootstrap，显示 `HISTORY_PARTIAL`；尚未采用事件源的旧任务显示白色 `HISTORY_UNTRACEABLE`，不回填伪造升级前历史。

## 唤醒链（#HO-076，2026-08-08 上线）

值班巡查端（QwenWork cron */30）发现他端定向任务后，经 `board-wake.py` 程序化拉起对应端执行，替代「等对应端恰好巡逻到」。

| 通道 | 实现 | 状态（2026-08-11 23:20 WorkBuddy 实测复验） |
|------|------|------------------------|
| workbuddy | 普通模式写入一次性 automation；urgent 模式同时写入前台可见 automation，并用 `codebuddy -p` 立即执行，任一路真实开工即可 | ✅ 实测通过：codebuddy CLI v2.115.0 无头执行 + urgent 竞速形态（--permission-mode bypassPermissions --tools）均正常返回；automation DB 直插有 last_run_at 拾取记录。直接唤醒并交互结果链路可用 |
| antigravity | language_server agentapi new-conversation（env 从运行中进程动态提取） | ⏸️ 当前应用离线（app_online=false，无 language_server 进程），不可唤醒；应用在线时历史 5/7 成功。**唤醒依赖应用在线**，当前离线=正常态 |
| trae | trae-solo-cn chat -m agent + AppleScript Return（Trae 内开会话） | ⚠️ 投递可靠（ok=26）但 liveness 弱：urgent 频繁 rc=211（14 次，prompt 已发送但 90 秒内任务无活动）。2026-08-09 界面直发 GLM-5.2 实测真实认领。**可投递但无法确认执行** |
| codex | ChatGPT.app 内 codex exec | ⚠️ 间歇性：2026-08-11 21:04/21:47 两次 notify 成功（rc=0），但 23:15 两次实测 TLS handshake eof、重连 5 次失败（当前网络层不可用）。通道就绪但**当前不可用**，非 401（额度已恢复） |
| qwenwork | 外部 `qoderclicn -p` + QwenWorkCN 内部按需 cron job | ❌ **外部 CLI 完全无法直接唤醒**（Account: Not logged in，20 次唤醒 0 成功、16 次 rc=42）；内部链 2026-08-09 已实测（lite 同构闸门 GO → connector execute → Max runlog），但需 QwenWork 应用在线 + 闸门 GO + 积分充足（8/11 实测积分耗尽）。L1/L2 用 `qwork-advanced`，L3 用独立 Max job |

用法：`board-wake.py channels`（通道状态）/ `wake --channel C --task T --prompt-file F`（普通异步派发）/ `wake --urgent --mode execute|review|resume ...`（加急直唤并确认任务真实活动；模型级 L3 增加 `--target-model Qwen3.8-Max`）/ `ledger --tail N`（查台账）。
台账：`.kb/_reports/board-wake-ledger.jsonl`。去重机制（2026-08-08 修订 3，用户拍板）：窗口 25min（<哨兵 30min 周期，未认领任务每轮可再唤醒）；仅成功唤醒计去重，失败不去重、由下一轮哨兵定时重试兜底；NOT_OPEN 守卫——任务非 open（已认领/待验收等）直接跳过不唤醒，认领态不受长窗口锁死。测试注入：`BOARD_WAKE_KB` 环境变量覆盖 KB 根路径。
停机纪律：`.kb/board-wake.paused` 总熔断；同通道 2 小时内连续 3 次真实传输失败后返回 rc=202，30 分钟冷却后自动进入 half-open 探针；`wake_gate` 拒绝记录不参与失败计数，避免熔断器自我毒化。端离线=正常态（rc=210），降级桌面通知不告警。被唤醒端按唤醒 prompt 调 `board-task-claim.py` 原子认领。

加急模式不绕过安全门禁：`execute` 只接受 open 并按 `--target-model` 检查执行上限；`review` 只接受 awaiting_review，拒绝同端自验并检查模型级 reviewer 上限；未提供模型时按端的保守默认档处理；`resume` 只允许原 executor 续做 claimed。默认 90 秒内必须看到本任务 status/PROGRESS/output 变化，否则 `rc=211`，避免把 CLI 返回 0 误报为工具已恢复。

QwenWork 外部 CLI 未登录时返回 `rc=210` 且 `fallback_required=internal_qwen_job`，这不等于 Max 不可唤醒；调用方必须继续尝试工作台内部定时任务通道。当前内部键 `qmodel_latest` 需由实时模型目录/前台标签确认其映射为 `Qwen3.8-Max`，不能单独作为公告牌真实模型档名；Max job 保持 disabled/按需执行，正式写入仍署名 `Qwen3.8-Max`。内部 runlog 只证明会话已启动，仍须任务文件或 Max 专属心跳前进才算 liveness。

正常 reviewer pool 为空时，只有用户可通过 `authorize-reviewer` transition 签发单任务、精确端/模型/家族/等级、one-shot 的技术验收授权。授权写正式 event、纳入 event-managed v2+ 状态 hash，review 完成即消费；不能跨任务、不能 self-review、不会提高全局能力。直接注入 status 会被 integrity/validator 隔离；旧 `review_override` 仅保留既有 v2 历史只读兼容，不作为新授权路径。

## 预警分诊链（T49，2026-08-09 上线，用户拍板）

补齐「哨兵跑出红告警只落 ALERTS.md、无后续处置」的漏洞：红告警自动拉起 advanced 档分诊会话逐条处置。

链路：`board-alert.py` 生成 ALERTS.md（v1.5 起头部含「红告警指纹」= 红告警集合 md5）→ 哨兵（QwenWork cron */30）无 execute 目标时检查指纹 → 去重（指纹变化，或距上次分诊 ≥2h；状态在 `alert-triage-state.json`）→ 写 `_trigger.json`（reason=alert_triage）→ `auto-exec-gate.py --triage`（共享全局每日预算 5，另设分诊每日上限 `TRIAGE_PER_DAY=2`，超限 NOGO:triage_budget_exhausted）→ 拉起「KB公告牌执行者-advanced」分诊分支 → `--commit "alert-triage" "alert_triage"` 记账。

分诊权限边界（用户 8/9 拍板）：可直接验收他端 ≤L2 逾期任务、`board-wake.py` 补唤（含 --urgent review）、诊断卡住任务、`board-task-create.py` 挂机制漏洞任务、桌面通知；**不碰治理文件与机制脚本**，机制修复一律挂牌等批准；GOVERNANCE_APPROVAL / DLQ 属用户终态/人工裁决，只上报不代办；executor==QwenWork 的自产任务不自评，唤醒他端验收。分诊记录落 `.kb/_reports/alert-triage-latest.md`，会话必须写 advanced 专属心跳（闸门对账口径）。

路由优先级：execute > alert_triage > review（每轮最多一个触发；review 选取改为等待最久优先）。哨兵 job=e92a39fd，advanced job=f8b1cfbd（均为 2026-08-09 重建，替代 70ad7a31/29e115fa；cron prompt 只能创建时写入，变更须重建）。测试窗口至 2026-08-22，沿用 auto-exec 熔断。

## git 策略

board 目录纳入 AutoDigest 白名单自动 commit（DECISIONS 2026-08-08 P0-3 方案1）。AI 只写各自任务目录，全局 index 由确定性脚本生成，AutoDigest commit 各任务目录中间态不影响其他任务。

## 判卷权威变更登记

- 2026-08-06 ~ 2026-08-08 实证期：QwenWork 专属判卷
- 2026-08-11 起：验收方必须是不同注册端；同端异模型不再允许 self-review，能力死锁走 user `authorize-reviewer` event
- 变更原因：多端流转需要，QwenWork 单点判卷不可扩展

## 实证期历史

- 2026-08-06 16:30 初始挂牌 E1-E3，index 指纹 52ea9628…（初版）
- 2026-08-06 17:20 增挂 E4-antigravity-poll（用户拍板「agy 挂上」）
- 2026-08-06 E1-codex-poll 暂缓派发（用户拍板：Codex 无额度）
- 2026-08-07 E2/E3/E4 实证全部通过（定时能力确认）
- 2026-08-08 正式实施 v1.0 启动，board-index-gen.py 上线，status.json 队列结构建立

## 2026-08-12 治理（验收积压复盘）

实证：2026-08-12 前 workbuddy/trae/antigravity 验收上限 L2，L3 验收只剩 codex/qwenwork 单点；
codex 额度断档 + qwenwork 未登录 → 5 个 L3 任务积压 60h+。治理后新增 4 项机制：

1. **验收通道冗余（P0-1）**：workbuddy max_review_level L2→L3（用户拍板）；`board-validate.py`
   新增 `REVIEWER_REDUNDANCY` 校验——每复杂度等级至少 2 个异家族验收端，单点验收报告警。
2. **验收超时自动升级（P0-2）**：`board-review-escalate.py` 扫 awaiting_review 超 4h：
   有 eligible reviewer → 自动 `board-wake.py wake --urgent --mode review` 唤醒验收端
   （复用 wake ledger 25min 去重，幂等）；无 reviewer（堵死）→ 台账 + 桌面通知需人工授权。
   已接入 `board-maintenance.py --apply` 链（max-wakes=2 防惊群）。
3. **任务过时/被替代检测（P1-1）**：`board-validate.py` 新增 `STALE_TASK`——open/claimed 超 7 天
   未认领或无进展 → 🟡 提示人工复核 cancel/supersede，消灭僵尸任务。
4. **submit 验收通道 fail-fast（P1-2）**：submit 时若全部 eligible reviewer 心跳过期（>24h），
   挂 `review_risk=stale_reviewers` 状态标记 + stderr 警告，提前暴露验收通道风险。

测试：122/122 PASS（含 4 项新机制的隔离回归）。校验器无新增红。
