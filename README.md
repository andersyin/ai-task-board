# AI Task Board

多 AI Agent 任务流转与公告牌系统。一套确定性 Python 脚本，让多个 AI Agent（Trae、WorkBuddy、Antigravity、QwenWork、Codex 等）通过文件系统共享任务看板，原子认领、流转状态、互验产出，无需额外服务端。

## 核心设计

- **公告牌**：所有任务以文件形式存在（`card.md` + `contract.json` + `status.json` + `events.jsonl`），AI 只读不手改，状态变更必须走脚本。
- **原子认领**：`board-task-claim.py` 通过文件锁实现 intent → claim 原子操作，杜绝竞态。
- **状态机**：open → claimed → awaiting_review → done / blocked / dlq / cancelled / superseded → archived。转换门禁、事件 hash 链、pending 前滚恢复。
- **验收协议**：执行端不自评闭环（self_close 禁止），验收方 = 非执行方任一在线端。支持 L1-L4 能力等级与跨模型验收。
- **唤醒路由**：`board-wake.py` 确定性唤醒，值班端调用，AI 不手写唤醒逻辑。
- **只读审计**：`board-audit.py` 核验公告牌全树 hash 不变，运行只读投影/租约/告警/仪表盘。

## 目录结构

```
board/                # 核心脚本（Python 3.9+，零外部依赖）
  board_contract.py          # 注册端、能力等级、状态枚举与原子写入的机器契约单源
  board_task_contract.py     # v3 acceptance/output/evidence 合同校验与指纹绑定
  board_verdicts.py          # PASS/FAIL 结论单源
  board_transition.py        # 转换门禁、任务锁、事件 hash 链与 pending 前滚恢复
  board_integrity.py         # 完整性检查
  board_reconcile.py         # 历史任务 reconcile
  board_reconcile_contract.py
  board_replay.py            # 历史只读 replay/dry-run 与证据包导出
  board-task-create.py       # 创建任务
  board-task-claim.py        # 原子认领
  board-task-transition.py   # activate/submit/block/review/approve/requeue/cancel/... 统一 CLI
  board-next-action.py       # 只读选择单一下一动作
  board-audit.py             # 只读审计
  board-validate.py          # 任务全生命周期校验
  board-wake.py              # 唤醒路由器
  board-setup.py             # 巡查端定时任务安装
  board-dashboard.py         # 仪表盘
  board-alert.py             # 告警
  board-callback.py          # 回调
  board-delivery.py          # 产物交付
  board-family.py            # 模型家族
  board-index-gen.py         # 索引投影
  board-lease-check.py       # 租约检查
  board-maintenance.py       # 巡查端唯一显式持久化入口
  board-review-escalate.py   # 验收超时自动升级
  board-reconcile-history.py
  board-replay.py            # CLI replay
  board-retrofill-review.py
  board-indirect-verify.py
  project-delivery-tracker.py
  trae-wake-proxy.py
  auto-exec-gate.py
  board-lease-check-runner.sh
  README.md                  # 公告牌详细文档

docs/                 # 协议文档
  SKILL.md                   # Skill 入口（各 AI Agent 通过 skill-sync 同步）
  protocol.md                # 完整任务流转协议 v2.1
```

## 快速开始

```bash
# 设置 KB_ROOT 环境变量指向你的知识库根目录
export KB_ROOT=/path/to/your/knowledge-base

# （可选）board-wake.py 通道 CLI 路径：默认自动探测常见安装位置，
# 如需指向非标准路径，用以下环境变量显式指定：
#   export WORKBUDDY_CLI=/path/to/codebuddy
#   export CODEX_CLI=/path/to/codex
#   export ANTIGRAVITY_CLI=/path/to/language_server
#   export TRAE_CLI=/path/to/trae-solo-cn
#   export QWENWORK_CLI=/path/to/qoderclicn

# 创建一个任务
python3 board/board-task-create.py \
  --id T01-hello \
  --title "示例任务" \
  --work-key your-agent \
  --acceptance "产物存在且可读" \
  --output "output/result.txt"

# 认领任务
python3 board/board-task-transition.py --task T01-hello --action claim --agent your-agent

# 提交产物
python3 board/board-task-transition.py --task T01-hello --action submit \
  --agent your-agent --request-id req-001

# 验收（另一个 agent）
python3 board/board-task-transition.py --task T01-hello --action review \
  --agent reviewer-agent --verdict PASS --request-id req-002
```

## 注册端

在 `board_contract.py` 的 `AGENT_CONTRACTS` 中添加你的 Agent：

```python
"your-agent": {
    "display_name": "YourAgent",
    "caps": ["scheduled-task", "your-agent"],
    "model_families": ["your-model-family"],
    "max_execute_level": "L2",
    "max_review_level": "L2",
    "internal_hetero_model": False,
},
```

## 技术特点

- **零外部依赖**：纯 Python 3.9+ 标准库
- **文件系统即数据库**：无需 SQLite/PostgreSQL，`json` + `jsonl` + `md`
- **hash 链事件账本**：append-only `events.jsonl`，每条事件含前一条的 hash，可回放验证
- **文件锁**：`fcntl.flock` 实现原子操作，多进程安全
- **幂等转换**：同一 request-id + 同参数重试幂等成功；不同参数硬拒绝
- **能力等级**：L1-L4 执行/验收等级，高等级可验收低等级，反之不可
- **DLQ 死信队列**：多次失败的任务进入 DLQ，不再自动重投，等待人工裁决

## 许可

MIT
