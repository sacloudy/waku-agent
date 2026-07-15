# Playground 索引

这里存放为了理解 waku-agent 而编写的可运行小实验。实验应保持短小、可独立运行, 并在自己的说明中写清前置条件、运行命令和预期输出。

- `standard_library/`: 只依赖 Python 标准库的语法、SQLite、HTTP 或并发小实验。
- `external_frameworks/`: 用于理解 Anthropic/OpenAI SDK、OpenTelemetry、MCP 等外部库边界的实验。
- `project_demos/`: 直接复用或简化本项目模块, 演示完整数据流、工具调用或记忆生命周期的实验。

主学习入口见 [项目学习指南](../learning.md)。新增实验时, 请在本页追加名称、目标与运行命令, 不要把一次性排查产物混入这里。

## 当前 demo

| 分类 | Demo | 目标 | 运行命令 |
| --- | --- | --- | --- |
| project_demos | [Waku Agent Turn 实践课](project_demos/agent_turn/00_README.md) | 用脚本化模型驱动真实 Waku 主链, 观察记忆、Loop、工具副作用、写回、trace 和迭代保护 | `.venv/bin/python learning/playground/project_demos/agent_turn/01_full_agent_turn_demo.py` |
