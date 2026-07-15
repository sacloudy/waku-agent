"""LLM-AS-JUDGE EVAL — "was the response good?" This is NOT a unit test.

A judge model scores qualities no assertion can check: helpfulness, whether
Waku actually used what it remembered, tone. Scores are 0–1 percentages
with a threshold, not 0/1 truths — never confuse the two (that confusion is
exactly what the deterministic suite next door exists to prevent).

Requires ANTHROPIC_API_KEY: the judge is a real model call.
"""

from __future__ import annotations

import pytest

from evals.helpers import HAS_KEY, make_waku

pytestmark = pytest.mark.skipif(not HAS_KEY, reason="LLM-as-judge needs ANTHROPIC_API_KEY")


@pytest.fixture(scope="module")
def geval_metrics():
    """
    为整个 Judge module 构造共享的 Helpfulness 与 MemoryUse GEval 指标。

    @return: 两个绑定同一 AnthropicJudge、threshold 均为 0.6 的 GEval metric。
    side effect: 创建真实 provider client, 具体远端调用延迟到 assert_test() 评分阶段。
    called by: 本文件两个 quality test 通过 pytest fixture 注入时调用一次。
    """
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCaseParams

    from evals.judge.anthropic_judge import AnthropicJudge

    # Step 1: 两个 metric 共享同一个 Judge client, 避免每条 case 重复装配 provider。
    judge = AnthropicJudge()
    # Step 2: Helpfulness 只观察用户输入和实际回答, 用于评价直接性、确认信息和语气。
    helpful = GEval(
        name="Helpfulness",
        criteria=(
            "The assistant reply should directly address the user's request, confirm any "
            "action taken (what/when/who), and be concise and warm."
        ),
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        model=judge,
        threshold=0.6,
    )
    # Step 3: MemoryUse 额外接收显式 retrieval_context, 判断回答是否利用给定记忆事实。
    uses_memory = GEval(
        name="MemoryUse",
        criteria=(
            "Given the retrieval context (the user's stored memories), the reply should "
            "correctly incorporate relevant remembered facts instead of ignoring them."
        ),
        evaluation_params=[
            LLMTestCaseParams.INPUT,
            LLMTestCaseParams.ACTUAL_OUTPUT,
            LLMTestCaseParams.RETRIEVAL_CONTEXT,
        ],
        model=judge,
        threshold=0.6,
    )
    return helpful, uses_memory


def test_scheduling_reply_is_helpful(tmp_path, geval_metrics):
    """
    让真实 Waku 处理预约请求, 再由 Helpfulness metric 评价最终回答是否达到阈值。

    @param ① tmp_path: pytest 提供的隔离 runtime 目录。
           ② geval_metrics: module fixture 返回的 Helpfulness 与 MemoryUse metrics。
    side effect: 发起 Waku 生成与 Judge 评分的真实模型调用, 并在 tmp_path 写 runtime state。
    called by: pytest 在 ANTHROPIC_API_KEY 可见时执行 Judge suite。
    """
    from deepeval import assert_test
    from deepeval.test_case import LLMTestCase

    helpful, _ = geval_metrics
    # Step 1: 先运行真实产品链路得到 actual_output, Judge 不替代被测模型。
    app = make_waku(tmp_path / "home")
    user_message = "Schedule a coffee with Alex next Tuesday at 9am"
    result = app.respond(user_message)

    # Step 2: DeepEval 把 input 与 actual_output 交给 Helpfulness, score 低于 0.6 即 pytest 失败。
    assert_test(LLMTestCase(input=user_message, actual_output=result.reply), [helpful])


def test_reply_uses_remembered_preference(tmp_path, geval_metrics):
    """
    预置一条 semantic fact, 再评价真实回答是否利用给定的 memory context。

    @param ① tmp_path: pytest 提供的隔离 runtime 目录。
           ② geval_metrics: module fixture 返回的 Helpfulness 与 MemoryUse metrics。
    side effect: 写入隔离 semantic memory, 发起 Waku 生成与 Judge 评分的真实模型调用。
    called by: pytest 在 ANTHROPIC_API_KEY 可见时执行 Judge suite。
    """
    from deepeval import assert_test
    from deepeval.test_case import LLMTestCase

    _, uses_memory = geval_metrics
    # Step 1: fact 写入真实 semantic store, Waku 是否检索并使用它仍由产品链路决定。
    app = make_waku(tmp_path / "home")
    app.memory.facts.add("alex", "Alex prefers morning meetings")
    user_message = "Book a catch-up with Alex on Friday"
    result = app.respond(user_message)

    # Step 2: retrieval_context 是传给 Judge 的期望事实, 不是从 Waku trace 自动提取的命中证据。
    assert_test(
        LLMTestCase(
            input=user_message,
            actual_output=result.reply,
            retrieval_context=["Alex prefers morning meetings"],
        ),
        [uses_memory],
    )
