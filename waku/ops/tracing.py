"""Trace — one trace per run (the LLM-Ops box, first step).

Two outputs from the same events:

1. JSONL, always on: every turn appends readable lines to
   .waku/traces/<date>.jsonl. A trace is just "what happened, in order" —
   open the file and read your agent's mind. Zero dependencies.

2. OpenTelemetry spans, when OTEL_EXPORTER_OTLP_ENDPOINT is set: the same
   events as a span tree any OTel backend can render. For a local dashboard:

       pip install 'waku-agent[tracing]'
       phoenix serve                                # localhost:6006
       OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 python -m waku

   Langfuse cloud speaks OTel too — point the endpoint + auth headers there
   instead. The instrumentation below doesn't know or care which.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone

from waku.config import Settings


def _now() -> str:
    """生成统一的 UTC 毫秒时间戳, 供所有 trace 与 usage record 排序。

    @return: ISO 8601 UTC 时间字符串。
    side effect: 读取当前系统时钟。
    called by: Tracer._write() 与 Tracer._record_usage() 写入记录前调用。
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Tracer:
    """同时承担 loop Observer、JSONL trace、usage ledger 与可选 OTel export。"""

    def __init__(self, settings: Settings):
        """为一个 Waku 实例初始化当天 trace 文件路径与可选 OTel tracer。

        @param settings: 当前运行配置, 提供 WAKU_HOME、provider、model 与 OTel endpoint。
        side effect: 可能初始化全局 OTel provider, 此时尚不写 trace 文件。
        called by: Waku.__init__() 在 Session 与 tools 装配完成后调用。
        """
        self.settings = settings

        # Step 1: JSONL 按本地日期分文件, 实际 record 内仍统一写 UTC 时间戳。
        self.path = settings.home / "traces" / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"

        # Step 2: OTel 是可选镜像输出, 初始化失败不会关闭始终可用的 JSONL trace。
        self._otel_tracer = self._init_otel(settings)
        self._span_ctx = None

    def _init_otel(self, settings: Settings):
        """按配置建立 OTel exporter 与 tracer, 未配置或缺依赖时保持 JSONL-only。

        @param settings: 含 otel_endpoint 的运行配置。
        @return: 可创建 span 的 OTel tracer, 功能关闭或依赖缺失时为 None。
        side effect: 可能注册进程级 TracerProvider 与后台 BatchSpanProcessor。
        called by: Tracer.__init__() 在实例初始化期间调用。
        """
        # endpoint 为空代表明确关闭 OTel, 不为可选能力支付导入和后台线程成本。
        if not settings.otel_endpoint:
            return None
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(resource=Resource.create({"service.name": "waku-agent"}))
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint, insecure=True))
            )

            # provider 是进程级注册, instance 保留引用以便每个 turn 主动 flush。
            trace.set_tracer_provider(provider)
            self._otel_provider = provider
            return trace.get_tracer("waku")
        except ImportError:
            # 缺少 optional extra 时只降级 OTel, JSONL 路径仍保持完整可观测性。
            print("(tracing) OTEL endpoint set but opentelemetry not installed — "
                  "pip install 'waku-agent[tracing]'. JSONL tracing still on.")
            return None

    def _write(self, record: dict) -> None:
        """为事件补统一时间戳并追加到当天 JSONL, 形成 dashboard 的原始事件流。

        @param record: 尚未带 ts 的事件字典, type 与业务字段由调用方提供。
        side effect: 原地补写 record["ts"] 并追加写 trace 文件。
        called by: event()、turn() 与 end_turn() 记录 loop 生命周期时调用。
        """
        # Step 1: 在真正落盘前统一打点, 避免各 Observer 产生不可比较的时间格式。
        record["ts"] = _now()

        # Step 2: 一行一个独立 JSON 对象, 即使进程中断也能保留此前已完成的事件。
        with self.path.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _record_usage(self, event: dict) -> None:
        """把一次 LLM 调用的 token 用量追加到永久 ledger, 与可重置 trace 分开保存。

        @param event: loop 发出的 llm event, usage 中包含 input/output token 数。
        side effect: 向 WAKU_HOME/usage.jsonl 追加 provider、model 与 token record。
        called by: event() 处理 kind="llm" 时调用。
        """
        usage = event.get("usage", {})
        record = {"ts": _now(), "provider": self.settings.provider,
                  "model": self.settings.model or "", "kind": "loop",
                  "in": usage.get("in", 0), "out": usage.get("out", 0)}
        with (self.settings.home / "usage.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")

    # ---- the Observer: called by the loop for every llm/tool/gate/... event
    def event(self, kind: str, event: dict) -> None:
        """消费统一 Observer event, 分流高频 text delta、usage、JSONL 与 OTel span。

        @param ① kind: 事件类型, 如 text、llm、tool、gate 或 consolidation。
               ② event: 与 kind 对应的结构化 payload。
        side effect: 可能写 trace/usage 文件并创建 OTel child span, text delta 不落盘。
        called by: compose() 生成的 fanout 在 Waku.respond() 主链路中调用。
        """
        # text delta 只服务 dashboard 的实时 SSE。逐 token 落盘会放大 trace 且重复最终 reply,
        # 因此这里有意跳过, 完整回复仍由 end_turn() 的 turn_end record 保存。
        if kind == "text":
            return  # streaming token deltas are for the live UI, not the trace

        # LLM token 是永久成本账本的唯一写入触发点, gate/tool 等事件不应污染 usage。
        if kind == "llm":
            self._record_usage(event)

        # 所有非 text event 先进入 JSONL source, OTel 只是同一事件的可选镜像。
        self._write({"type": kind, **event})
        if self._otel_tracer and self._span_ctx is not None:
            with self._otel_tracer.start_as_current_span(
                f"{kind}.{event.get('tool', event.get('decision', ''))}".rstrip("."),
                attributes={
                    "openinference.span.kind": {"llm": "LLM", "tool": "TOOL"}.get(kind, "CHAIN"),
                    **{f"waku.{k}": json.dumps(v, default=str) for k, v in event.items()},
                },
            ):
                pass

    # ---- one run = one root span + turn_start/turn_end JSONL markers
    @contextmanager
    def turn(self, user_message: str):
        """包围一次 agent turn, 写开始标记并在 OTel 启用时维护 root span 上下文。

        @param user_message: 当前 turn 的用户原文, 会写入 trace 与 root span attribute。
        @return: context manager, with 块内 yield 当前 Tracer 实例。
        side effect: 写 turn_start, 可能创建 OTel root span 并暂存 _span_ctx。
        called by: Waku.respond() 在 build_system、loop 与 memory 收尾外层调用。
        """
        # Step 1: turn_start 无条件先落 JSONL, 即使后续挂起也能被 dashboard 识别为未完成 turn。
        self._write({"type": "turn_start", "user_message": user_message})
        if self._otel_tracer:
            # Step 2: OTel 模式在 with 生命周期内暴露 root span, event() 才能挂接 child span。
            with self._otel_tracer.start_as_current_span(
                "agent_run",
                attributes={"openinference.span.kind": "AGENT", "waku.user_message": user_message},
            ) as span:
                self._span_ctx = span
                try:
                    yield self
                finally:
                    # Step 3: 无论主链成功还是抛错都清空上下文, 防止下一 turn 误挂到旧 root span。
                    self._span_ctx = None
        else:
            # JSONL-only 模式保持相同 context manager 契约, 上层无需区分 tracing backend。
            yield self

    def end_turn(self, reply: str, iterations: int) -> None:
        """记录一次正常完成 turn 的最终回复与迭代数, 并尽力刷新 OTel exporter。

        @param ① reply: loop 最终返回给 gateway 的文本。
               ② iterations: 本 turn 实际执行的模型迭代次数。
        side effect: 写 turn_end JSONL, OTel 启用时同步等待最多两秒 flush。
        called by: Waku.respond() 离开 tracer.turn() 上下文后调用。
        """
        self._write({"type": "turn_end", "reply": reply, "iterations": iterations})
        if getattr(self, "_otel_provider", None):
            # flush per turn: the trace should survive even a killed process
            self._otel_provider.force_flush(timeout_millis=2000)


def compose(*observers) -> callable:
    """把 gateway Observer 与 Tracer Observer 合成一个稳定 fanout, 隔离 loop 与展示层。

    @param observers: 任意数量的可选 Observer callable, None 会被过滤。
    @return: 按注册顺序向所有 active Observer 转发同一事件的 callable。
    side effect: 返回的 fanout 被调用时会触发各 Observer 自身的展示或持久化副作用。
    called by: Waku.respond() 在每个 turn 开始时组合 gateway observer 与 tracer.event。
    """
    active = [o for o in observers if o]

    def fanout(kind: str, event: dict) -> None:
        # 同一 payload 依次交给 active Observer, loop 不需要知道 UI 与 tracing 的存在。
        for obs in active:
            obs(kind, event)

    return fanout
