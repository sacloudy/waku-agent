"""
测试对象: waku.loop.models.OpenAICompatClient 和 _OpenAIStream。
测试内容: 展示 Anthropic content blocks 如何转换成 OpenAI messages, 以及 stream 分片如何重组为 Loop 可消费的统一响应。
测试方法: 绕过会创建真实 SDK client 的构造函数, 直接向纯转换方法提供内存对象并断言每个协议字段。
前置依赖: 无需 API key、server、数据库或网络; 只依赖仓库开发环境中的 pytest。
启动/准备命令: 无需。
运行方式: .venv/bin/python -m pytest -o addopts= learning/test/test_models_protocol_flow.py -q
"""

from types import SimpleNamespace

from waku.loop.models import OpenAICompatClient, _OpenAIStream


def _adapter_without_sdk() -> OpenAICompatClient:
    # helper 目的: _to_openai 只做纯数据转换, 因此不运行会实例化 openai.OpenAI 的 __init__。
    # 外部边界: 这个对象不会发送请求, 不读取 key, 也不接触文件系统。
    return OpenAICompatClient.__new__(OpenAICompatClient)


class _StreamingClient:
    # helper 目的: 按真实迭代顺序返回内存 chunks, 让 text_stream 自己完成跨 chunk 聚合。
    # 外部边界: _call 不创建 SDK client、不读取 key, 也不访问网络或文件系统。
    def __init__(self, chunks):
        self.chunks = chunks
        self.last_call = None

    def _call(self, kwargs, **extra):
        self.last_call = (kwargs, extra)
        return iter(self.chunks)


def test_to_openai_preserves_text_tool_call_and_tool_result_roles():
    # 验证目标: 一轮 Anthropic 形状的 assistant tool_use 和 user tool_result 会变成 OpenAI 的两类专用 message。
    # 调用路径: 直接调用 adapter._to_openai, 不经过 Provider SDK 或 Agent Loop。
    # 关键断言: tool call id 在请求与结果之间保持一致, 证明下一轮模型能关联执行结果。
    adapter = _adapter_without_sdk()
    assistant_blocks = [
        SimpleNamespace(type="text", text="我先查看日历。"),
        SimpleNamespace(
            type="tool_use",
            id="call-7",
            name="list_events",
            input={"start": "2026-07-15"},
        ),
    ]
    messages = [
        {"role": "assistant", "content": assistant_blocks},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-7",
                    "content": "No events found.",
                }
            ],
        },
    ]

    converted = adapter._to_openai(
        model="demo-model",
        system="system context",
        messages=messages,
        max_tokens=256,
        tools=[
            {
                "name": "list_events",
                "description": "Read calendar events",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    )

    assert converted["messages"][0] == {"role": "system", "content": "system context"}
    assistant = converted["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "我先查看日历。"
    assert assistant["tool_calls"][0]["id"] == "call-7"
    assert assistant["tool_calls"][0]["function"]["name"] == "list_events"
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"start": "2026-07-15"}'
    assert converted["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call-7",
        "content": "No events found.",
    }
    assert converted["tools"][0]["function"]["parameters"] == {
        "type": "object",
        "properties": {},
    }


def test_stream_reassembles_interleaved_tool_argument_fragments_and_usage():
    # 验证目标: 多个 tool 的 argument 分片交错到达时, text_stream 会按 index 分槽并保留 usage。
    # 调用路径: 内存 fake chunks -> text_stream 聚合 -> get_final_message 完成 Anthropic 协议转换。
    # 关键断言: 两组 JSON 都独立还原且 stop_reason 为 tool_use, 证明并行 tool call 不会串线。
    chunks = [
        SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content="正在",
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call-9",
                                function=SimpleNamespace(
                                    name="create_event", arguments='{"title":"De'
                                ),
                            ),
                            SimpleNamespace(
                                index=1,
                                id="call-10",
                                function=SimpleNamespace(
                                    name="save_note", arguments='{"subject":"pro'
                                ),
                            ),
                        ],
                    )
                )
            ],
        ),
        SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content="安排",
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,
                                function=SimpleNamespace(
                                    name=None, arguments='mo","start":"2026-07-16'
                                ),
                            ),
                            SimpleNamespace(
                                index=1,
                                id=None,
                                function=SimpleNamespace(
                                    name=None, arguments='ject","content":"ready'
                                ),
                            ),
                        ],
                    )
                )
            ],
        ),
        SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,
                                function=SimpleNamespace(name=None, arguments='T09:00"}'),
                            ),
                            SimpleNamespace(
                                index=1,
                                id=None,
                                function=SimpleNamespace(name=None, arguments='"}'),
                            ),
                        ],
                    )
                )
            ],
        ),
        SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=5),
            choices=[],
        ),
    ]
    client = _StreamingClient(chunks)
    stream = _OpenAIStream(client=client, kwargs={"model": "demo-model"})

    assert list(stream.text_stream) == ["正在", "安排"]
    response = stream.get_final_message()

    assert response.stop_reason == "tool_use"
    assert response.content[0].type == "text" and response.content[0].text == "正在安排"
    assert response.content[1].type == "tool_use"
    assert response.content[1].id == "call-9"
    assert response.content[1].input == {
        "title": "Demo",
        "start": "2026-07-16T09:00",
    }
    assert response.content[2].type == "tool_use"
    assert response.content[2].id == "call-10"
    assert response.content[2].input == {"subject": "project", "content": "ready"}
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 5
    assert client.last_call == (
        {"model": "demo-model"},
        {"stream": True, "stream_options": {"include_usage": True}},
    )
