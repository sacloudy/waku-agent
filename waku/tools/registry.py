"""Tool registry — the 'Agentic Tools' box on the whiteboard.

A tool is three things: a name+description the model reads, a JSON schema for
its arguments, and a Python function that runs. That's it. (Registry pattern
adapted from launch-agentic-rag's app/agents/tools/registry.py.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., str]  # tools return a string the model observes

    def to_api(self) -> dict[str, Any]:
        """
        把内部 Tool 描述转换成 Messages API 的 tools 参数形状, 不暴露本地 Python callable。

        @return: 仅含 name、description 和 input_schema 的字典, 会随模型请求发送给 provider。
        side effect: 无。
        called by: ToolRegistry.schemas() 为每次 Loop reason 调用批量生成模型可见 schema。
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        """
        初始化进程内 tool 名称索引, 这是 tool 装配与 Loop 执行之间的共享状态容器。

        side effect: 创建空的进程内 registry, 不启动 tool 也不访问外部系统。
        called by: build_registry() 装配生产 tool, deterministic eval 和教学 demo 也会直接构造。
        """
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """
        按 tool.name 注册可执行 Tool, 同名注册会以最新对象覆盖旧对象。

        @param tool: 同时携带模型描述、JSON schema 和本地 callable 的 Tool 对象。
        side effect: 修改 registry 的进程内名称索引, 但不会立即执行 tool。
        called by: build_registry() 依次装配内置、可选 Apple、experimental 和 MCP tool。
        """
        # 名称既是模型 tool call 的路由 key 也是唯一索引, 覆盖语义让后注册 adapter 可以替换实现。
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        """
        导出全部已注册 tool 的模型可见 schema, 保持 registry 的注册顺序。

        @return: Messages API tools 参数列表, 每项都不包含本地 callable。
        side effect: 无。
        called by: run_loop() 在流式和普通模型请求前调用。
        """
        return [t.to_api() for t in self._tools.values()]

    def execute(self, name: str, args: dict[str, Any]) -> str:
        """
        安全执行一次模型请求的 tool call, 把未知名称或运行异常统一转换为模型可观察文本。

        @param ① name: 模型返回的 tool 名称, 用于从 registry 定位本地 callable。
               ② args: 模型生成的 JSON 参数字典, 会作为关键字参数传给 tool.fn。
        @return: tool 的字符串结果或 Error 文本, run_loop() 会将其包装为 tool_result。
        side effect: 可能触发目标 tool 的数据库、文件、网络或系统应用副作用, 但 registry 自身不重试。
        called by: run_loop() 在 act 阶段逐个处理 response 中的 tool_use block。
        """
        # Step 1: 先在执行边界验证名称, 未注册 tool 直接返回协议内错误而不是抛出 KeyError。
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'"

        # Step 2: schema 只向模型声明期望参数, registry 不重复校验, 而是把 kwargs 交给真实实现。
        # 模型仍可能违约, 因此具体 tool 的参数兜底和下面的异常边界都不能省略。
        try:
            return tool.fn(**args)

        # Step 3: tool 异常转换成 tool_result 文本, 让下一轮模型有机会解释或修正参数而不终止 Loop。
        except Exception as exc:  # surface, don't crash — the model can retry
            return f"Error running {name}: {exc}"
