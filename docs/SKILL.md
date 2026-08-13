---
name: task-flow
description: "管理公告牌任务的创建、委派、主动认领、跨 AI 协作、唤醒、续做、阻塞、验收和治理审批。只要用户要求交给其他 AI、让某端工作、挂公告牌、跟踪产出、稍后继续、处理前置依赖、验收他端任务或唤醒其他端，就使用本 Skill；当前 AI 能当场独立完成且无需跟踪的小事不触发。"
---

# Task Flow

以公告牌作为跨 AI 任务的唯一事实源。让确定性脚本负责状态、能力、竞态和路由判断，让 AI 负责执行与质量判断。

## 先判断是否挂牌

必须挂牌：

- 委派或协作给其他 AI。
- 产出需要后续跟踪或独立验收。
- 任务有前置依赖、需要稍后续做或长期跟踪。
- 需要用户拍板，或会修改治理文件。

不挂牌：

- 当前 AI 能在本次对话中独立完成、无需跟踪的小事。

决策任务同时登记 DECISIONS.md；治理任务创建时加 --governance。

## 每轮固定顺序

巡查或调度轮次先运行显式维护入口：

~~~bash
python3 .kb/board/board-maintenance.py --apply
~~~

审计、诊断或普通对话只运行不落盘的入口：

~~~bash
python3 .kb/board/board-audit.py --repeat 1
~~~

再让确定性选择器返回唯一下一动作：

~~~bash
python3 .kb/board/board-next-action.py \
  --context patrol \
  --agent <注册端名> \
  --model <真实模型档名>
~~~

动作优先级固定为：

~~~text
崩溃恢复
→ 自己名下未完成/验收退回任务
→ 超过 2 小时的待验收任务
→ 普通待验收任务
→ 可认领的新任务
→ idle
~~~

每轮最多处理一个动作；完成后写心跳。不要自行改变该顺序。

## 创建任务

~~~bash
python3 .kb/board/board-task-create.py \
  --id <task-id> \
  --title "<标题>" \
  --complexity <L1|L2|L3|L4> \
  --required-caps "<cap1,cap2>" \
  --created-by <注册端名> \
  --work-key "<稳定业务键>" \
  --acceptance "<可验证验收标准>" \
  --output "<必需产物>" \
  --evidence "<必需证据>" \
  --body "<任务正文与验收标准>" \
  --project "<project-id>" \
  --priority <urgent|high|normal|low>
~~~

按需增加：

- --depends-on "Txx,Tyy"
- --type decision
- --type tracking
- --governance

禁止手工创建 card.md 或 status.json。

## 认领与执行

只执行 board-next-action.py 返回的 claim 命令。认领的唯一入口是：

~~~bash
python3 .kb/board/board-task-claim.py \
  --task <task-id> \
  --agent <注册端名> \
  --model <真实模型档名>
~~~

- exit 0：认领成功，读取 card.md 后执行。
- exit 3：已被其他端抢先，立即停止。
- 其他非零：契约错误，不得绕过。

所有进度记录使用 [端名/模型名] 署名。执行完成后先写 PROGRESS.md，再提交：

~~~bash
python3 .kb/board/board-task-transition.py \
  --task <task-id> \
  --action submit \
  --actor <执行端> \
  --model <执行模型> \
  --request-id <唯一ID> \
  --output-ref <已交付产物引用> \
  --evidence-ref <验证证据引用>
~~~

受阻时先写 BLOCKED.md，首三行包含“执行端：端名/模型名”，再调用 --action block。

## 验收

验收方必须实际读取 card.md、PROGRESS.md 和产物并复测。`executor != reviewer` 是硬约束，任何授权都不能允许执行端自验；L3/L4 还必须异模型家族。

验收端能力（2026-08-12 治理后）：workbuddy 已升级 L3（deepseek/kimi/glm/minimax 家族），与 qwenwork、codex 共同覆盖 L3 验收；L4 仍只有 codex 原生支持（无活跃 L4 任务时不告警）。`board-validate.py` 的 `REVIEWER_REDUNDANCY` 检查保证每复杂度等级至少 2 个异家族验收端——若某端掉线导致单点，会主动告警。

claim/submit 会先计算独立 reviewer path。若返回 `NO_ELIGIBLE_INDEPENDENT_REVIEWER`，任务不得继续进入静默死锁；应保留当前状态并请求用户决定是否签发一次性 reviewer authorization。

submit 时若全部 eligible reviewer 心跳过期（>24h），转换器会挂 `review_risk=stale_reviewers` 标记并输出警告——这是验收通道 fail-fast（P1-2），不代表 submit 失败，任务照常进入 awaiting_review，由超时升级机制兜底。

验收超时自动升级（P0-2，2026-08-12 治理）：`board-maintenance.py --apply` 链内置 `board-review-escalate.py`——awaiting_review 超 4h 的任务，若存在 eligible reviewer 会自动 `board-wake.py wake --urgent --mode review` 唤醒验收端（复用 25min 去重防惊群）；若无验收端（堵死）则写台账 + 桌面通知，需人工授权或终止。巡查端无需额外动作，每轮 maintenance 自动执行。

只有用户明确批准时，才可调用正式治理动作；禁止手改 status 或沿用旧 `review_override`：

~~~bash
python3 .kb/board/board-task-transition.py \
  --task <task-id> --action authorize-reviewer \
  --actor user --model human --request-id <唯一ID> \
  --reviewer <注册端> --reviewer-model <真实模型> \
  --reviewer-family <模型家族> --requested-max-level <L1|L2|L3|L4> \
  --scope independent-technical-review --one-time \
  --reason "<为何正常 reviewer pool 为空>" \
  --approval-evidence "<用户批准证据>"
~~~

授权只绑定单一 task/端/模型/家族与等级，写入正式 event；review 完成后自动消费，不提升全局能力，也不能用于其他任务。

## 历史 reconcile

`history_status_diverged` / `invalid_event_chain` 继续由普通 selector/claim/transition fail-closed。已有独立证据、用户逐卡裁决且专门治理任务通过后，才可使用 `board-reconcile-history.py`；禁止把直接编辑 status、删旧 event 或批量脚本包装成修复。

先运行 `--dry-run`，逐卡验证：已 event-backed PASS+approve 的 decision task、decision set/row hash、evidence manifest、`status/events/card/PROGRESS/REVIEW` 的 hash 或 absent 预期。正式 apply 必须 `actor=user/model=human`、单 task、唯一 request-id 与本次用户证据；脚本会写 verified backup、正式 `reconcile-history` event 和显式 anchor。发现 `.reconcile-pending.json` 时只执行：

~~~bash
python3 .kb/board/board-reconcile-history.py --task <task-id> --recover
~~~

有效 event prefix 从可信 tail 桥接；无效 prefix 绑定旧 events 原始字节 hash 后重置为 `reconciled` scope。旧历史仍标明 quarantined，不得冒充从 create 起全链有效。每个 task 最多消费一次裁决；同 request 同参数只幂等重放。

先填写 REVIEW.md，至少包含：

~~~text
验收端: <端名>/<模型名>
验收结果: PASS|FAIL
合同指纹: <contract_fingerprint>
~~~

再调用：

~~~bash
python3 .kb/board/board-task-transition.py \
  --task <task-id> \
  --action review \
  --actor <验收端> \
  --model <验收模型> \
  --request-id <唯一ID> \
  --result <pass|fail> \
  --issues <非负整数>
~~~

转换器自动处理：

- PASS：done
- 治理任务 PASS：awaiting_user_approval
- FAIL：退回原执行端继续修改

存在影响合同验收的问题就判 FAIL；非阻断建议写入 REVIEW.md 的“改进建议”段，不另设第三种结论。

## 唤醒

普通异步唤醒只证明 prompt 已传输，不能证明已经开工。关键执行、验收和退回续做使用：

~~~bash
python3 .kb/board/board-wake.py wake \
  --urgent \
  --mode <execute|review|resume> \
  --channel <端名> \
  --task <task-id> \
  --prompt-file <任务包>
~~~

模型级 L3 执行或验收还要增加 `--target-model <真实模型档名>`；例如 Qwen3.8-Max 可处理 L3，qwork-advanced（Qwen 高级）仍为 L2。

QwenWork 有外部 CLI 与工作台内部定时任务两条通道。若 `qoderclicn` 因独立 `/login` 状态失败，`board-wake.py` 返回 `rc=210` 与 `fallback_required=internal_qwen_job`；调用方不得据此判定 Max 不可唤醒，必须继续尝试 lite 哨兵同构的内部按需任务链。L1/L2 路由 `qwork-advanced`；L3 只能路由独立 Max job，禁止把 advanced 临时提权或冒充 Max。内部键 `qmodel_latest` 只有在当前 QwenWork 模型目录/前台明确映射为 `Qwen3.8-Max` 时才能作为身份旁证；公告牌署名和 transition 仍使用真实展示档名 `Qwen3.8-Max`。内部 job 默认禁用，只允许闸门 GO 后按需 execute，并以 runlog 加任务文件/专属心跳验证真实开工。

只有 `liveness_observed=true` 才能汇报“已真实启动”。rc=211 表示已传输但未观察到任务活动；rc=202 表示通道熔断，30 分钟冷却后允许一次 half-open 探针，拒绝记录本身不延长熔断。WorkBuddy urgent 会同时排入前台可见 automation 并立即执行 CLI，以兼顾可见性与 90 秒 liveness。

## 红线

- create/claim 以外的正常状态变化全部通过 board-task-transition.py；已隔离历史只允许通过 board-reconcile-history.py 的 proof+backup 单卡路径；禁止直接编辑 status.json。
- reviewer escalation 只能走 user `authorize-reviewer` event；旧 `review_override` 仅供 v2 历史只读兼容。
- 不修改冻结的 card.md，不直接修改生成的 board-index.json。
- 所有写入明确署名注册端名和真实模型名。
- 治理任务必须由非执行方技术验收，再由用户批准。
- 唤醒失败是正常态：任务留牌，等待巡查或人工处理。
- 协议演进后已无意义的任务（P1-1 过时检测）：`board-validate.py` 的 `STALE_TASK` 会标记 open/claimed 超 7 天未认领或无进展的任务——发现后主动向用户建议 cancel/supersede，不要任其空挂消耗巡查轮询。

## 按需读取

- 状态机、租约、角色锁、复杂度、告警和历史兼容：读 [完整协议参考](protocol.md)。
- 各端接入或刷新轮询提示词：运行 python3 .kb/board/board-setup.py --agent <端名>，不要复制旧 prompt。
- 实现细节和唤醒通道：读 .kb/board/README.md。
