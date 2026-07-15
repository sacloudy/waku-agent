"""Ephemeral Agent Run — assembles working memory for each turn.

The inner box on the whiteboard: everything here is rebuilt per run and thrown
away. What persists lives in waku/memory. Working memory =

    system prompt (SOUL.md)            ← who Waku is
  + durable facts & episodes           ← what Waku remembers (gated!)
  + current chat history               ← this conversation
  + the user's new message
"""

from __future__ import annotations


from waku.config import Settings

DEFAULT_SOUL = """\
You are Waku, a personal assistant running locally on your user's laptop.
You are concise, warm, and proactive. You remember what your user tells you.

Rules:
- When the user wants to schedule something, use create_event. Resolve relative
  dates and times ("next Tuesday", "in 30 minutes") to ISO timestamps yourself;
  the current date and time are given below — trust them, never ask the user
  what time it is.
- When the user asks what's on their calendar (a day, a week, "yesterday"), use
  list_events — you CAN read the calendar, not just write to it.
- When the user shares something durable about a person, project, or preference,
  use save_note to remember it.
- When asked to message someone, use send_message (it drafts to a local outbox).
- If memory context is provided below, trust it — it came from your own store.
- Call each tool at most once per request. Your history shows [tools used: ...]
  lines for past turns — if a tool already ran, do NOT run it again; answer
  from that record instead.
- Be honest about where things live. Every tool's output states exactly where
  its artifact landed (local calendar file, Apple Calendar, memory database at
  .waku/state.db) — relay that truthfully, and never claim something synced
  anywhere the tool output doesn't say.
- You can manage your own memory: use manage_memory to correct or forget facts,
  update_soul to save a standing preference the user gives you, and create_skill
  to save a repeatable workflow the user teaches you (only after they say yes).
"""


def load_soul(settings: Settings) -> str:
    """
    读取可编辑的 SOUL.md persona, 这是 system prompt 进入 Session 前的文件边界。

    @param settings: 当前运行配置, 其中 home 决定 SOUL.md 的实际位置。
    @return: SOUL.md 文本, 会作为 build_system() 的第一段 system prompt。
    side effect: 文件不存在时会把 DEFAULT_SOUL 写入本地状态目录。
    called by: Session.build_system() 在每个 Agent turn 开始时调用。
    """
    # Step 1: 始终从 WAKU_HOME 对应目录定位 persona, 避免 gateway 各自维护一份身份配置。
    soul_path = settings.home / "SOUL.md"

    # Step 2: 首次运行才写默认模板, 后续只读取用户已经编辑过的文件。
    if not soul_path.exists():
        soul_path.write_text(DEFAULT_SOUL)
    return soul_path.read_text()


class Session:
    """Holds one conversation: the chat history plus the recipe for the
    system prompt. One Session per gateway connection."""

    def __init__(self, settings: Settings, memory=None, session_id: str = "default"):
        """
        初始化一个 gateway 会话的工作记忆容器, 持有 session id、历史和 Memory facade 引用。

        @param ① settings: 当前运行配置, build_system() 会从中读取 home 等上下文。
               ② memory: 可选 Memory facade, 为 None 时 Session 仍可构造纯本地 system prompt。
               ③ session_id: chat_log 使用的会话标签, 默认值用于未显式分流的入口。
        side effect: 仅初始化进程内状态, 不读取或写入持久存储。
        called by: Waku 装配阶段创建主 Session, deterministic eval 也会直接构造轻量实例。
        """
        self.settings = settings
        self.memory = memory  # waku.memory.Memory (None until Phase-2 wiring)
        self.session_id = session_id
        self.history: list[dict] = []

    def build_system(self, user_message: str, notify=None) -> str:
        """
        为当前用户消息组装 system prompt, 依次加入 persona、当前时间、按需记忆和匹配的 skill。

        @param ① user_message: 本轮用户文本, retrieval gate 与 skill matcher 都以它作为查询输入。
               ② notify: 可选事件观察者, Memory 会通过它报告 gate 判定。
        @return: 完整 system prompt 字符串, 会直接交给 run_loop() 的模型请求。
        side effect: 可能首次创建 SOUL.md, 并可能调用小模型、SQLite/Supabase 检索与 skill 文件扫描。
        called by: Waku.respond() 在构造本轮 messages 之前调用。
        """
        from datetime import datetime

        # Step 1: 使用本机带时区时间, 让模型能直接解析相对时间而不需要向用户追问当前时刻。
        now = datetime.now().astimezone()

        # Step 2: persona 与当前时间始终存在, 即使没有 Memory facade 也能形成最小可运行 system prompt。
        parts = [load_soul(self.settings),
                 f"\nRight now it is {now:%A, %Y-%m-%d %H:%M} ({now:%Z}, UTC{now:%z})."]

        if self.memory is not None:
            # Step 3: 先经过 retrieval gate 再检索 durable memory, 避免无关事实增加延迟并偏置回答。
            retrieved = self.memory.gated_retrieve(user_message, notify=notify)
            if retrieved:
                parts.append("\nRelevant memory:\n" + retrieved)

            # Step 4: procedural skill 与事实/情景记忆分开匹配, 命中后作为独立指令段注入。
            skills = self.memory.matching_skills(user_message)
            if skills:
                parts.append("\nRelevant skill instructions:\n" + skills)

        # Step 5: 保持各上下文块的顺序稳定, 让 provider adapter 接收到一致的 system 语义。
        return "\n".join(parts)

    def add_exchange(self, user_message: str, reply: str, tool_calls: list | None = None,
                     source: str = "cli") -> None:
        """
        把完成的一轮写入进程内历史和可选 chat_log, 并把真实 tool 结果折叠进 assistant 记录。

        @param ① user_message: 本轮用户原始文本。
               ② reply: Loop 生成的最终回复文本。
               ③ tool_calls: 本轮实际执行的 tool event 列表, 用于形成防重复执行摘要。
               ④ source: gateway 来源标签, 随持久记录写入 chat_log。
        side effect: 修改 Session.history, 并在 Memory 可用时向 SQLite chat_log 写入两行记录。
        called by: Waku.respond() 在 run_loop() 返回后调用。
        """
        # Step 1: 先把 tool 执行证据合并进 assistant 记录, 后续模型才能知道副作用已经发生。
        record = reply
        if tool_calls:
            summary = "; ".join(f"{c['tool']}({c['args']}) -> {c['output']}" for c in tool_calls)
            record = f"{reply}\n[tools used: {summary}]"

        # Step 2: 进程内 history 使用模型可直接消费的 user/assistant 消息形状, 供下一轮复制。
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": record})

        # Step 3: 持久写入复用同一个 record, 保证 Session 切换恢复后仍保留 tool 执行事实。
        if self.memory is not None:
            self.memory.log_chat(user_message, record, session_id=self.session_id, source=source)

    # ---- session lifecycle (the "New chat" / history feature)
    # A session is just a tag on chat_log rows. Starting a new one clears working
    # memory; switching reloads a past conversation's history so replies have
    # context. Consolidation still reads ALL unconsolidated rows regardless.
    def start_new(self, session_id: str) -> None:
        """
        切换到一个全新的 session id, 同时清空当前进程内对话历史。

        @param session_id: 新会话标签, 后续 add_exchange() 会用它标记 chat_log 行。
        side effect: 更新 Session.session_id 并清空 Session.history, 不删除任何持久聊天记录。
        called by: Dashboard 的 new conversation action 创建新会话时调用。
        """
        # 新会话只重置工作记忆指针, 旧 chat_log 仍可通过 switch() 恢复。
        self.session_id = session_id
        self.history = []

    def switch(self, session_id: str) -> None:
        """
        切换到已有 session 并从 Memory 恢复成对出现的 user/assistant 工作历史。

        @param session_id: 目标会话标签, 用于查询对应 chat_log 记录。
        side effect: 更新 Session.session_id、清空当前 history, 并可能从 SQLite 读取后重建历史。
        called by: Dashboard 的 switch conversation action 在用户打开旧会话时调用。
        """
        # Step 1: 先清空当前工作历史, 避免两个 session 的上下文在模型请求中混合。
        self.session_id = session_id
        self.history = []

        # Step 2: 没有 Memory facade 的轻量 Session 无持久来源, 此时保持空历史即可。
        if self.memory is None:
            return

        # Step 3: Memory 已把 chat_log 归并成 exchange 对, 这里再还原为模型消息序列。
        for user_msg, reply in self.memory.session_history(session_id):
            self.history.append({"role": "user", "content": user_msg})
            self.history.append({"role": "assistant", "content": reply})
