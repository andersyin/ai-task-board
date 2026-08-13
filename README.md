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
  board-task-create.py       # 创建任务
  board-task-claim.py        # 原子认领（唯一入口；不是 transition 的 action）
  board-task-transition.py   # activate/submit/block/review/approve/... 统一 CLI
  templates/REVIEW.md        # 验收报告模板（review 前复制到任务目录并填字段）
  README.md                  # 公告牌详细文档与完整脚本清单

docs/                 # 协议文档
  SKILL.md                   # Skill 入口（各 AI Agent 通过 skill-sync 同步）
  protocol.md                # 完整任务流转协议

tests/                # pytest（契约 + 隔离目录下的 create/claim/submit/review）
examples/             # 任务卡模板与完整生命周期命令
```

完整脚本清单见 `board/README.md`。

## 快速开始

克隆后，**默认把本仓库的 `board/` 当作公告牌根目录**（任务写在 `board/tasks/`，已 gitignore）。`--board-root` 可指向任意空目录做隔离实验。

`KB_ROOT` **不是** create/claim/transition 的输入。只有把脚本装进 `$KB_ROOT/.kb/board` 时，唤醒路由 `board-wake.py` 和 `board-lease-check-runner.sh` 才读取它（也可用 `BOARD_WAKE_KB` 覆盖）。

需要 Python 3.9+。文件锁使用 `fcntl`，面向 POSIX（macOS / Linux）。

```bash
# （可选）board-wake.py 通道 CLI 路径：默认自动探测常见安装位置，
# 如需指向非标准路径，用以下环境变量显式指定：
#   export WORKBUDDY_CLI=/path/to/codebuddy
#   export CODEX_CLI=/path/to/codex
#   export ANTIGRAVITY_CLI=/path/to/language_server
#   export TRAE_CLI=/path/to/trae-solo-cn
#   export QWENWORK_CLI=/path/to/qoderclicn

# 1. 创建任务（created-by 必须是已注册端；v3 必填 work-key / acceptance / output / evidence / body）
#    created-by 不在 required-caps 里算跨端委托，必须声明 --delivery
python3 board/board-task-create.py \
  --id T01-hello \
  --title "示例任务" \
  --work-key demo-hello \
  --required-caps scheduled-task \
  --created-by trae \
  --complexity L1 \
  --delivery return_result \
  --acceptance "产物存在且可读" \
  --output "output/result.txt" \
  --evidence "cat output/result.txt" \
  --body "写一个可读的示例产物。"

# 2. 原子认领（独立脚本；--model 禁止 auto/unknown）
python3 board/board-task-claim.py \
  --task T01-hello --agent workbuddy --model deepseek-v4-flash

# 3. 执行端署名进度并交出产物，再 submit（--actor 不是 --agent）
#    在 board/tasks/T01-hello/PROGRESS.md 写入一行：
#    [workbuddy/deepseek-v4-flash] 已交付 output/result.txt
python3 board/board-task-transition.py --task T01-hello --action submit \
  --actor workbuddy --model deepseek-v4-flash --request-id req-001 \
  --output-ref output/result.txt --evidence-ref cat:output/result.txt

# 4. 另一个注册端验收：先按 board/templates/REVIEW.md 填写任务目录下的 REVIEW.md
#    必填顶格字段：验收端 / 验收结果 / 合同指纹（从 card.md 复制）
python3 board/board-task-transition.py --task T01-hello --action review \
  --actor trae --model glm-5.2 --request-id req-002 \
  --result pass --issues 0
```

隔离实验时给每个命令加上 `--board-root /tmp/my-board`（create 会自动建 `tasks/`）。完整字段示例见 `examples/README.md`。

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
- **文件锁**：`fcntl.flock` 实现原子操作，多进程安全（POSIX）
- **幂等转换**：同一 request-id + 同参数重试幂等成功；不同参数硬拒绝
- **能力等级**：L1-L4 执行/验收等级，高等级可验收低等级，反之不可
- **DLQ 死信队列**：多次失败的任务进入 DLQ，不再自动重投，等待人工裁决

## 许可

MIT
