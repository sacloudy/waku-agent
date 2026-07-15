"""Model access — five providers, one loop, zero framework.

The loop speaks one dialect: Anthropic's Messages shape (system/messages/tools
in, content blocks out). Providers plug in two ways:

  anthropic wire format (native)     → Anthropic, Kimi/Moonshot, GLM/Z.ai
  openai wire format (thin adapter)  → OpenAI, Google Gemini

Pick with WAKU_PROVIDER=anthropic|openai|gemini|kimi|glm and set that
provider's API key in .env. Override the model ids with WAKU_MODEL /
WAKU_SMALL_MODEL if the defaults below age out — they're just strings.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from types import SimpleNamespace

from waku.config import Settings


@dataclass(frozen=True)
class Provider:
    """记录一个 provider 的 wire format、凭证来源和默认模型。"""

    kind: str        # 'anthropic' or 'openai' — the wire format
    key_env: str     # which env var holds the key
    base_url: str | None
    model: str       # default main model (the loop)
    small_model: str  # default cheap model (retrieval gate + consolidation)


PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider("anthropic", "ANTHROPIC_API_KEY", None,
                          "claude-sonnet-5", "claude-haiku-4-5-20251001"),
    "openai":    Provider("openai", "OPENAI_API_KEY", None,
                          "gpt-5.6", "gpt-5.6-luna"),
    "gemini":    Provider("openai", "GEMINI_API_KEY",
                          "https://generativelanguage.googleapis.com/v1beta/openai/",
                          "gemini-3.5-flash", "gemini-3.1-flash-lite"),
    "kimi":      Provider("anthropic", "MOONSHOT_API_KEY", "https://api.moonshot.ai/anthropic",
                          "kimi-k2.7", "kimi-k2.7"),
    "glm":       Provider("anthropic", "ZHIPU_API_KEY", "https://api.z.ai/api/anthropic",
                          "glm-5.2", "glm-5-turbo"),
}


def get_client(settings: Settings):
    """
    根据 Settings 选择 provider, 并构造 Agent loop 统一使用的 Messages client。

    @param settings: 启动时读取的配置, 其中 provider 决定凭证、默认模型和 wire format。
    @return: 暴露 messages.create() 的 Anthropic 形状 client, 供 Waku 和辅助模型调用。
    side effect: 读取环境变量, 回填 settings 的模型 id, 并创建远端 API client。
    called by: Waku.__init__() 装配主 client, AnthropicJudge.__init__() 装配 judge client。
    """
    # Step 1: 先把业务 provider 名称解析为 wire format 配置, 未知名称在网络请求前立即失败。
    provider = PROVIDERS.get(settings.provider)
    if provider is None:
        raise SystemExit(f"Unknown WAKU_PROVIDER '{settings.provider}'. "
                         f"Pick one of: {', '.join(PROVIDERS)}")

    # Step 2: 显式 WAKU_API_KEY 优先于 provider 专属环境变量, 便于兼容自定义 endpoint。
    api_key = settings.api_key or os.getenv(provider.key_env, "")
    if not api_key:
        raise SystemExit(
            f"No API key for provider '{settings.provider}'. "
            f"Set {provider.key_env} in .env (see .env.example)."
        )

    # 调用方后续只读取 Settings, 因此默认模型在 client 装配阶段一次性写回同一个对象。
    settings.model = settings.model or provider.model
    settings.small_model = settings.small_model or provider.small_model
    base_url = settings.base_url or provider.base_url

    # Step 3: 两种 SDK 最终都被收敛成 Anthropic Messages 形状, Agent loop 不感知 provider 差异。
    # timeout 是远端调用的最后一道时限, 避免 gateway 看起来永久卡死。
    timeout = float(os.getenv("WAKU_LLM_TIMEOUT", "120"))

    if provider.kind == "anthropic":
        import anthropic

        kwargs: dict = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        return anthropic.Anthropic(**kwargs)
    return OpenAICompatClient(api_key=api_key, base_url=base_url, timeout=timeout)


class OpenAICompatClient:
    """把 OpenAI chat.completions 双向适配成 Agent loop 期望的 Anthropic Messages 形状。"""

    def __init__(self, api_key: str, base_url: str | None = None, timeout: float = 120.0):
        """
        初始化底层 OpenAI SDK, 并暴露与 Anthropic client 相同的 messages 入口。

        @param ① api_key: OpenAI-compatible endpoint 的凭证。
               ② base_url: 可选兼容 endpoint, Gemini 等 provider 会在这里注入地址。
               ③ timeout: 单次远端调用的超时秒数。
        side effect: 创建 OpenAI SDK client, 但此时不发起网络请求。
        called by: get_client() 在 provider.kind 为 openai 时调用。
        """
        import openai

        self._client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        # Agent loop 只认识 messages.create/stream, 这里用 facade 隐藏 chat.completions 命名差异。
        self.messages = SimpleNamespace(create=self._create, stream=self._stream)

    def _to_openai(self, *, model, messages, max_tokens, system=None, tools=None) -> dict:
        """
        把 Anthropic 形状的 system、messages 和 tool schema 转成 OpenAI 请求参数。

        @param ① model: 当前主模型 id。
               ② messages: Agent loop 累积的 Anthropic 形状消息。
               ③ max_tokens: 调用方允许生成的最大 token 数。
               ④ system: 独立的 system prompt, OpenAI 协议会把它变成首条消息。
               ⑤ tools: ToolRegistry 导出的 Anthropic tool schema 列表。
        @return: 可直接传给 chat.completions.create() 的 keyword 参数字典。
        side effect: 无。
        called by: _create() 处理非流式调用, _stream() 处理流式调用。
        """
        oai_messages = []
        # Step 1: Anthropic 的顶层 system 参数在 OpenAI 协议中必须成为 role=system 的消息。
        if system:
            oai_messages.append({"role": "system", "content": system})

        # Step 2: 逐条翻译本轮 working memory, 并保留 tool call id 以便结果能正确配对。
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                oai_messages.append({"role": message["role"], "content": content})
            elif message["role"] == "assistant":
                # Anthropic 把 text/tool_use 放在同一组 content blocks, OpenAI 则拆成 content/tool_calls。
                text = "".join(b.text for b in content if getattr(b, "type", "") == "text")
                calls = [
                    {"id": b.id, "type": "function",
                     "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                    for b in content if getattr(b, "type", "") == "tool_use"
                ]
                entry: dict = {"role": "assistant", "content": text or None}
                if calls:
                    entry["tool_calls"] = calls
                oai_messages.append(entry)
            else:
                # Anthropic 用一条 user 消息承载多个 tool_result, OpenAI 要求每个结果各占一条 tool 消息。
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        oai_messages.append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": block["content"],
                        })

        # Step 3: 最后转换 tool schema 外壳, 内部 JSON Schema 原样保留给 provider 校验参数。
        kwargs: dict = {"model": model, "messages": oai_messages,
                        "max_completion_tokens": max_tokens}
        if tools:
            kwargs["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in tools
            ]
        return kwargs

    def _call(self, kwargs: dict, **extra):
        """
        调用 OpenAI-compatible endpoint, 并兼容只接受旧 max_tokens 字段的服务。

        @param ① kwargs: _to_openai() 生成的标准请求参数。
               ② extra: stream 和 stream_options 等调用模式参数。
        @return: 底层 chat.completions.create() 的响应或 stream 迭代器。
        side effect: 发起远端模型请求, 首次异常时可能用旧字段再请求一次。
        called by: _create() 发起非流式请求, _OpenAIStream.text_stream 发起流式请求。
        """
        # Step 1: 优先使用新字段 max_completion_tokens, 与当前 OpenAI SDK 语义保持一致。
        try:
            return self._client.chat.completions.create(**kwargs, **extra)
        except Exception:
            # Step 2: 兼容旧 endpoint 的字段名。这里复制 kwargs, 避免污染后续重试或调试快照。
            k = dict(kwargs)
            k["max_tokens"] = k.pop("max_completion_tokens", None)
            return self._client.chat.completions.create(**k, **extra)

    def _create(self, *, model, messages, max_tokens, system=None, tools=None):
        """
        执行一次非流式 OpenAI 请求, 再把响应还原为 Anthropic content blocks。

        @param ① model: 当前主模型 id。
               ② messages: Agent loop 的 Anthropic 形状 working memory。
               ③ max_tokens: 最大输出 token 数。
               ④ system: 可选 system prompt。
               ⑤ tools: 可选 Anthropic tool schema 列表。
        @return: 含 stop_reason、usage 和 content blocks 的 Anthropic 形状响应。
        side effect: 通过 _call() 发起一次或兼容重试后的远端模型请求。
        called by: run_loop() 和 retrieval/consolidation 在调用 messages.create() 时进入。
        """
        # Step 1: 先完成入站协议转换并调用 OpenAI endpoint。
        response = self._call(self._to_openai(
            model=model, messages=messages, max_tokens=max_tokens, system=system, tools=tools))
        choice = response.choices[0].message

        # Step 2: 把文本和 function tool_calls 还原成 loop 可直接扫描的 Anthropic blocks。
        blocks = []
        if choice.content:
            blocks.append(SimpleNamespace(type="text", text=choice.content))
        for call in choice.tool_calls or []:
            blocks.append(SimpleNamespace(
                type="tool_use", id=call.id, name=call.function.name,
                input=json.loads(call.function.arguments or "{}"),
            ))
        # Step 3: 统一 stop_reason 和 token 字段名, 让 loop/tracer 不需要 provider 分支。
        usage = getattr(response, "usage", None)
        return SimpleNamespace(
            stop_reason="tool_use" if choice.tool_calls else "end_turn",
            usage=SimpleNamespace(
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
            ),
            content=blocks,
        )

    def _stream(self, *, model, messages, max_tokens, system=None, tools=None):
        """
        为 OpenAI 流式请求创建 Anthropic messages.stream() 形状的上下文对象。

        @param ① model: 当前主模型 id。
               ② messages: Agent loop 的 Anthropic 形状 working memory。
               ③ max_tokens: 最大输出 token 数。
               ④ system: 可选 system prompt。
               ⑤ tools: 可选 Anthropic tool schema 列表。
        @return: 延迟发起网络请求的 _OpenAIStream 上下文对象。
        side effect: 无, 真正的网络请求在遍历 text_stream 时发生。
        called by: run_loop() 在 stream=True 且 client 支持 stream 时调用。
        """
        # 这里只冻结翻译后的请求参数, 保持 with messages.stream(...) 的惰性语义。
        kwargs = self._to_openai(
            model=model, messages=messages, max_tokens=max_tokens, system=system, tools=tools)
        return _OpenAIStream(self, kwargs)


class _OpenAIStream:
    """模拟 Anthropic stream 上下文, 并跨 chunk 保存文本、tool 参数片段和 usage。"""

    def __init__(self, client: OpenAICompatClient, kwargs: dict):
        """
        保存流式调用所需依赖, 并初始化跨 chunk 的组装状态。

        @param ① client: 提供 _call() 远端边界的 OpenAICompatClient。
               ② kwargs: 已转换成 OpenAI 形状的请求参数。
        side effect: 初始化本地聚合容器, 不发起网络请求。
        called by: OpenAICompatClient._stream() 创建每次独立的流式会话。
        """
        self._client = client
        self._kwargs = kwargs
        self._text: list[str] = []
        self._tools: dict[int, dict] = {}   # index → {id, name, args}
        self._usage = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        """
        消费 OpenAI chunks, 即时产出文本 delta 并累计最终响应所需状态。

        @return: 文本 delta 的 generator, 遍历结束后内部保存完整文本、tool 和 usage。
        side effect: 发起流式远端请求, 并持续更新 _text、_tools、_usage。
        called by: run_loop() 的流式分支在 with messages.stream() 内迭代。
        """
        # Step 1: 请求 usage 一并出现在 stream 尾部, 为最终 Anthropic 响应保留 token 统计。
        stream = self._client._call(
            self._kwargs, stream=True, stream_options={"include_usage": True})

        # Step 2: 文本可以直接逐片 yield, tool call 则必须按 index 跨 chunk 聚合后才能解析。
        for chunk in stream:
            if getattr(chunk, "usage", None):
                self._usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                self._text.append(delta.content)
                yield delta.content
            for tc in (getattr(delta, "tool_calls", None) or []):
                # 同一 tool 的 JSON arguments 往往被拆成多段字符串。按 tc.index 找槽位再顺序追加,
                # 否则多个并行 tool call 会串线, 或在半段 JSON 上过早 json.loads()。
                slot = self._tools.setdefault(tc.index, {"id": None, "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    slot["args"] += tc.function.arguments

    def get_final_message(self):
        """
        把已消费完的 stream 状态收束成 Agent loop 需要的 Anthropic 响应。

        @return: 含完整 text/tool_use blocks、stop_reason 和 usage 的响应对象。
        side effect: 解析累计的 tool JSON 参数, 非法 JSON 会向 run_loop 抛出异常。
        called by: run_loop() 在 text_stream 遍历结束后读取最终消息。
        """
        # Step 1: 先把已经向 gateway 发出的文本 delta 合并回最终 text block。
        blocks = []
        text = "".join(self._text)
        if text:
            blocks.append(SimpleNamespace(type="text", text=text))
        # Step 2: 此时 arguments 已收齐, 才能安全解析 JSON 并恢复 Anthropic tool_use block。
        for slot in self._tools.values():
            blocks.append(SimpleNamespace(
                type="tool_use", id=slot["id"], name=slot["name"],
                input=json.loads(slot["args"] or "{}")))
        # Step 3: tool 是否存在决定 stop_reason, token 名称则统一成 tracer 使用的字段。
        usage = self._usage
        return SimpleNamespace(
            stop_reason="tool_use" if self._tools else "end_turn",
            usage=SimpleNamespace(
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0)),
            content=blocks,
        )
