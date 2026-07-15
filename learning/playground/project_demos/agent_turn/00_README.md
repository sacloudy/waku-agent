# Waku Agent Turn 实践课

这组 demo 用真实项目代码拆开一次 Agent Turn。它不访问远程模型, 不需要 API Key, 也不会写 Apple Calendar。远程 LLM 被替换成行为可预测的脚本化 client, 其余装配、记忆检索、工具执行、SQLite、ICS、会话写回和 JSONL trace 都走真实路径。

## 目录

- [核心心智模型](#核心心智模型)
- [源码地图](#源码地图)
- [Demo 索引](#demo-索引)
- [新手推荐路线](#新手推荐路线)
- [第一课导读](#第一课导读)
- [理解检查](#理解检查)

## 运行前提

在仓库根目录完成开发环境安装:

```bash
uv venv --python 3.13
uv pip install -e '.[dev]'
```

不需要 `.env` 或模型密钥。Demo 1 只会重建 `/tmp/waku-agent-turn-demo`, 不接触仓库默认的 `.waku/`。

## 核心心智模型

1. **Gateway 不是 Agent**: CLI、Dashboard、Telegram 和 Voice 只负责搬运输入输出, 智能主路径统一落到 `Waku.respond()`。
2. **工作记忆先于 Loop**: 每轮先组装 SOUL、当前时间、相关长期记忆、匹配技能和会话历史, 再调用主模型。
3. **模型请求不等于副作用**: `tool_use` 只是结构化意图, 必须经过 `ToolRegistry.execute()` 才会真正写数据库或文件。
4. **工具结果必须回到模型**: Loop 把 `tool_result` 追加到消息后再次推理, 让模型基于真实执行结果生成回复。
5. **结束条件是设计的一部分**: 没有工具调用时自然结束; 模型持续请求工具时由 `max_iterations` 硬停止。

## 源码地图

- 总装配和一轮编排: [`Waku.respond()`](../../../../waku/app.py#L42)
- 系统提示和记忆注入: [`Session.build_system()`](../../../../waku/runtime/session.py#L64)
- 会话历史和持久化写回: [`Session.add_exchange()`](../../../../waku/runtime/session.py#L86)
- 主循环与两个退出条件: [`run_loop()`](../../../../waku/loop/agent.py#L40)
- 工具安全执行边界: [`ToolRegistry.execute()`](../../../../waku/tools/registry.py#L40)
- 日历幂等检查和副作用: [`create_event`](../../../../waku/tools/calendar.py#L119)
- 记忆检索门面: [`Memory.gated_retrieve()`](../../../../waku/memory/__init__.py#L45)
- trace 与 token ledger: [`Tracer.event()`](../../../../waku/ops/tracing.py#L83)

## Demo 索引

| Demo | 学习点 | 运行命令 |
| --- | --- | --- |
| [01 完整 Agent Turn](01_full_agent_turn_demo.py) | 跑真实 Waku 主链, 观察三次模型调用、两次主 Loop 迭代、记忆与技能注入、日历副作用及写回产物 | `uv run python learning/playground/project_demos/agent_turn/01_full_agent_turn_demo.py` |
| [02 迭代上限保护](02_iteration_guardrail_demo.py) | 让脚本化模型永远请求工具, 观察硬退出如何阻止无限循环 | `uv run python learning/playground/project_demos/agent_turn/02_iteration_guardrail_demo.py` |

## 新手推荐路线

先运行 Demo 1。它覆盖用户可见的完整成功路径, 能先建立“输入如何变成副作用和回复”的整体感觉。然后运行 Demo 2, 专门理解生产 Agent 不能只依赖模型自觉停止的原因。

推荐每个 demo 都采用三遍法:

1. 第一遍只运行, 不看代码, 根据阶段日志猜数据在流向哪里。
2. 第二遍对照上面的源码地图, 逐个找到日志对应的真实函数。
3. 第三遍修改脚本化模型响应, 预测输出后再运行验证。

## 第一课导读

### 运行前先预测

Demo 1 中用户要求安排一次咖啡会面。请先猜三个数字:

- 小模型检索门会调用几次?
- 主模型会调用几次?
- 最终 SQLite 中会新增多少条聊天记录?

### 运行时观察

重点看五组输出:

1. `observer` 事件说明主路径什么时候经过 gate、LLM 和 tool。
2. 模型请求快照说明第一次主推理只有用户消息, 第二次主推理多了 assistant tool request 和 user tool result。
3. 系统提示检查说明事实记忆与 `schedule-meeting` 技能正文已经注入。
4. SQLite、ICS 和 `MEMORY.md` 说明一次调用产生了哪些不同性质的持久状态。
5. JSONL timeline 说明可观测性没有侵入 Loop, 而是通过 observer 旁路记录。

### 运行后回到设计

这个 demo 最重要的不是“成功创建日历”, 而是看清依赖注入的价值。真实 `Waku` 接受可替换的 client 和 connection, 因而同一套业务装配既能连接远程模型, 也能在离线评测和教学中获得确定结果。

## 理解检查

完成两个 demo 后, 尝试回答:

1. 为什么单工具任务通常是两次主模型调用, 而不是一次?
2. 为什么 `ToolRegistry.execute()` 把异常转成文本返回给模型, 而不是直接让进程崩溃?
3. `max_iterations` 保护的是哪一种失败模式? 它能否保证任务一定完成?
4. 哪些状态只存在当前进程, 哪些状态在重启后仍能恢复?
5. 如果把脚本化 client 换回真实 Provider, 哪些数据会离开本机?

建议不要一次看答案。先根据运行输出写下自己的解释, 再回到源码验证。
