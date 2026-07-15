"""A DeepEval judge model backed by the same provider client Waku itself uses.

DeepEval calls generate() with an optional pydantic schema when it wants
structured verdicts; we ask the model for JSON and validate it back.
"""

from __future__ import annotations

import json

from deepeval.models import DeepEvalBaseLLM

from waku.config import load_settings
from waku.loop.models import get_client


class AnthropicJudge(DeepEvalBaseLLM):
    def __init__(self, model: str | None = None):
        """
        复用当前 Waku provider 配置创建 Judge client, 默认选择 small_model 控制评测成本。

        @param model: 可选 Judge model id, 为空时使用 get_client() 回填的 settings.small_model。
        side effect: 读取 .env 与 provider 环境变量并创建远端 SDK client, 此时不发起请求。
        called by: test_response_quality.py 的 module-scoped geval_metrics fixture 创建 Judge 时调用。
        """
        # Step 1: 与产品 runtime 共用 Settings/get_client, Judge 不维护第二套 provider 协议适配。
        self.settings = load_settings()
        self.client = get_client(self.settings)
        # Step 2: 优先尊重测试显式 model, 否则使用 provider 的低成本默认模型。
        self.model = model or self.settings.small_model

    def load_model(self):
        """
        向 DeepEval 暴露底层 Messages client, 保持 BaseLLM 接口兼容。

        @return: 由 get_client() 创建且支持 messages.create() 的 provider client。
        side effect: 无。
        called by: DeepEval 在需要访问自定义模型对象时调用。
        """
        return self.client

    def generate(self, prompt: str, schema=None):
        """
        把 DeepEval prompt 发送到 Waku provider, 并按需解析为 schema 实例。

        @param ① prompt: GEval 根据 criteria 与 LLMTestCase 生成的 Judge 请求。
               ② schema: DeepEval 可选的 Pydantic 输出 schema, 用于结构化 verdict。
        @return: schema 存在时返回验证后的模型实例, 否则返回纯文本 Judge 输出。
        side effect: 发起一次真实模型请求, JSON 缺失或不合法时会抛出解析异常。
        called by: DeepEval GEval 计算 score 与 reason 时调用。
        """
        # Step 1: schema 模式把 JSON 约束追加到 prompt, 但仍依赖模型按文本协议遵守格式。
        if schema is not None:
            prompt += (
                "\n\nReply with ONLY a JSON object matching this schema, no prose:\n"
                + json.dumps(schema.model_json_schema())
            )
        # Step 2: client 已统一为 Anthropic Messages 形状, provider wire format 差异在 models.py 内消化。
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        # Step 3: 只拼接 text blocks。结构化模式再截取最外层 JSON object 并交给 Pydantic 验证。
        text = "".join(b.text for b in response.content if b.type == "text")
        if schema is not None:
            return schema.model_validate_json(text[text.index("{") : text.rindex("}") + 1])
        return text

    async def a_generate(self, prompt: str, schema=None):
        """
        提供 DeepEval 所需 async 接口, 当前实现直接复用同步 generate()。

        @param ① prompt: GEval 生成的 Judge 请求。
               ② schema: 可选 Pydantic 输出 schema。
        @return: 与 generate() 相同的文本或 schema 实例。
        side effect: 在当前 event loop 线程内同步发起远端请求, 不提供真正异步 I/O。
        called by: DeepEval 选择异步模型接口时调用。
        """
        return self.generate(prompt, schema)

    def get_model_name(self):
        """
        返回包含实际 model id 的 Judge 名称, 供 DeepEval 日志与结果标识使用。

        @return: 形如 AnthropicJudge(model-id) 的可读名称。
        side effect: 无。
        called by: DeepEval 输出 metric 执行信息时调用。
        """
        return f"AnthropicJudge({self.model})"
