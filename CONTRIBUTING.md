# 贡献指南

感谢你对 AI Task Board 的兴趣！

## 开发环境

```bash
git clone https://github.com/andersyin/ai-task-board.git
cd ai-task-board
pip install pytest
```

## 代码规范

- Python 3.9+，只用标准库（零外部依赖是核心设计原则）
- 4 空格缩进
- 所有公共函数应有 docstring
- 禁止 bare `except:`，必须捕获具体异常
- 禁止 `print` 做调试输出（用 `logging` 或结构化输出）

## 测试

```bash
# 运行所有测试
pytest tests/ -v

# 运行单个测试文件
pytest tests/test_contract.py -v
```

提交前确保所有测试通过。

## 提交流程

1. Fork 仓库
2. 创建分支：`git checkout -b feature/your-feature`
3. 提交改动：`git commit -m "描述"`
4. 推送：`git push origin feature/your-feature`
5. 创建 Pull Request

## 添加新 Agent

1. 在 `board/board_contract.py` 的 `AGENT_CONTRACTS` 中添加注册
2. 在 `board/board-wake.py` 中添加唤醒通道（如需）
3. 添加测试用例
4. 更新 README

## 架构原则

- **确定性优先**：脚本不做 LLM 推理，只做确定性状态转换
- **文件系统即数据库**：不引入数据库依赖
- **只读审计**：审计脚本不修改任何文件
- **执行端不自评**：验收必须由非执行端完成
- **幂等性**：同一请求重试不产生副作用

## 许可

提交即表示你同意将代码以 MIT 许可发布。
