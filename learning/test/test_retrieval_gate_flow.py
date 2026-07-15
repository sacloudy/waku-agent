"""
测试对象: waku.memory.retrieval_gate.should_retrieve。
测试内容: 展示 skip、retrieve 和解析失败时 fail-open 三种决策语义。
测试方法: 使用只返回内存 content block 的 fake client, 检查返回 tuple 和发送给小模型的最小请求。
前置依赖: 无需 API key、server、数据库或网络; 只依赖仓库开发环境中的 pytest。
启动/准备命令: 无需。
运行方式: .venv/bin/python -m pytest -o addopts= learning/test/test_retrieval_gate_flow.py -q
"""

from types import SimpleNamespace

from waku.memory.retrieval_gate import should_retrieve


class _ScriptedMessages:
    # helper 目的: 记录 gate 发送的请求并返回固定文本, 让每个状态分支可重复观察。
    # 外部边界: create 只构造内存响应, 不访问任何 Provider 或本地状态。
    def __init__(self, text: str):
        self.text = text
        self.last_request = None

    def create(self, **kwargs):
        self.last_request = kwargs
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])


def _client_returning(text: str):
    # helper 目的: 用一行构造 fake client 和可检查的 messages 记录器, 避免每个测试重复装配。
    # 外部边界: 只创建内存对象, 不读取本地状态、不访问任何 Provider 或网络。
    messages = _ScriptedMessages(text)
    return SimpleNamespace(messages=messages), messages


def test_gate_skips_memory_for_self_contained_message():
    # 验证目标: 小模型给出 retrieve=false 时, gate 明确返回 skip 语义和空 query。
    # 调用路径: fake client -> should_retrieve -> JSON 提取, 不进入任何 memory store。
    # 关键断言: 返回值为 False 且请求使用 small_model, 说明这一步只是检索前决策。
    client, messages = _client_returning(
        'prefix {"retrieve": false, "query": "", "reason": "self contained"} suffix'
    )

    decision = should_retrieve(client, "small-demo", "what is 2+2?")

    assert decision == (False, "", "self contained")
    assert messages.last_request["model"] == "small-demo"
    assert "what is 2+2?" in messages.last_request["messages"][0]["content"]


def test_gate_returns_rewritten_query_for_personal_memory():
    # 验证目标: 需要个人历史时, gate 把原消息改写成 store 可消费的检索 query。
    # 调用路径: fake client -> should_retrieve -> retrieve=true 分支。
    # 关键断言: query 与 reason 原样返回, 证明 Memory facade 能据此检索 facts 和 episodes。
    client, _ = _client_returning(
        '{"retrieve": true, "query": "Alex meeting", "reason": "personal plan"}'
    )

    decision = should_retrieve(client, "small-demo", "when do I meet Alex?")

    assert decision == (True, "Alex meeting", "personal plan")


def test_gate_fails_open_when_model_output_is_not_json():
    # 验证目标: gate 自身输出不可解析时仍允许检索, 避免一次小模型故障让长期 memory 永久不可见。
    # 调用路径: fake client 返回非法 JSON -> should_retrieve except 分支。
    # 关键断言: retrieve=True 且回退到原消息, 说明失败只增加检索成本而不会静默丢失上下文。
    client, _ = _client_returning("not-json")

    retrieve, query, reason = should_retrieve(client, "small-demo", "remember my project")

    assert retrieve is True
    assert query == "remember my project"
    assert reason == "gate failed open (ValueError)"
