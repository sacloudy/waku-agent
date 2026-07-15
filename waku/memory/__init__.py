"""Memory facade — the three pillars behind one small interface.

    procedural  SKILL.md files      how to act
    semantic    facts table (FTS5)  what is durably true
    episodic    episodes table      what happened, when

Plus the two agents that manage them:
    retrieval_gate   decides IF a turn needs memory   (hero moment #1)
    consolidation    distills chats into facts, every N exchanges
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import anthropic

from waku.config import Settings
from waku.memory import consolidation, retrieval_gate
from waku.memory.episodic.store import SqliteEpisodeStore
from waku.memory.procedural.loader import SkillLoader
from waku.memory.semantic.store import SqliteFactStore

REPO_SKILLS = Path(__file__).resolve().parents[2] / "skills"


class Memory:
    """把 semantic、episodic、procedural memory 聚合成 Waku.respond() 使用的统一边界。"""

    def __init__(self, conn: sqlite3.Connection, settings: Settings, client: anthropic.Anthropic):
        """装配三类长期记忆及其共享依赖, 建立单个 Waku 实例的 memory facade。

        @param ① conn: state.db 连接, 承载 chat、episode 和默认 fact store。
               ② settings: 当前运行配置, 决定 semantic store、skill 路径和巩固阈值。
               ③ client: Anthropic shape 的模型 client, retrieval gate 与 consolidation 共用。
        side effect: 扫描 repo/home skill 目录, Supabase 模式还会初始化远端 client。
        called by: Waku.__init__() 在 tool、Session 和 loop 装配之前调用。
        """
        self.conn = conn
        self.settings = settings
        self.client = client

        # Step 1: semantic memory 可按配置替换实现, 但 facade 对上层保持 facts 接口不变。
        self.facts = self._make_fact_store(conn, settings)

        # Step 2: episode 和 procedural memory 仍保持 local-first, 分别绑定 SQLite 与 SKILL.md。
        self.episodes = SqliteEpisodeStore(conn)
        self.skills = SkillLoader([REPO_SKILLS, settings.home / "skills"])

    @staticmethod
    def _make_fact_store(conn, settings):
        """根据 semantic_store 配置选择 fact store, 隔离 SQLite 与 Supabase 实现差异。

        @param ① conn: SQLite 连接, 默认 SqliteFactStore 直接复用。
               ② settings: 含 semantic_store 与 retrieval_top_k 的运行配置。
        @return: 提供 add() 与 search() 的 semantic fact store。
        side effect: Supabase 分支会导入可选依赖并创建 Supabase、OpenAI client。
        called by: Memory.__init__() 在 facade 初始化期间调用。
        """
        # 只替换 semantic facts 的存取实现, chat log、episodes 和 session 仍留在 state.db。
        if settings.semantic_store == "supabase":
            from waku.memory.semantic.supabase_store import SupabaseFactStore

            return SupabaseFactStore(settings)
        return SqliteFactStore(conn)

    # ---- retrieval (gated — see retrieval_gate.py for why)
    def gated_retrieve(self, message: str, notify=None) -> str:
        """先让 retrieval gate 判断是否值得查长期记忆, 再按需合并 fact 与 episode 文本。

        @param ① message: 当前用户消息, 同时是 gate 的判定输入和失败时的回退查询词。
               ② notify: 可选 Observer, 用于把 skip/retrieve 决策发送给 gateway 与 tracing。
        @return: 可直接拼进 system prompt 的多行记忆文本, skip 或无结果时为空串。
        side effect: 调用 small model, 有 notify 时两种决策都会发送 gate event, 仅 retrieve 分支查询两个 store。
        called by: Session.build_system() 为每个新 turn 组装 working memory 时调用。
        """
        # Step 1: gate 只产出是否检索、查询词和原因, 不在判定阶段访问任何 memory store。
        retrieve, query, reason = retrieval_gate.should_retrieve(
            self.client, self.settings.small_model, message
        )

        # Step 2: 先发布决策事件, dashboard 和 trace 因而能解释本次为何查或不查记忆。
        if notify:
            notify("gate", {"decision": "retrieve" if retrieve else "skip", "reason": reason})

        # skip 是正常的性能与相关性分支, 不是检索失败。此时不会触碰 facts 或 episodes。
        if not retrieve:
            return ""

        # Step 3: retrieve 分支复用 gate 生成的 query, 按各自 top-k 合并两类长期记忆。
        found = self.facts.search(query, self.settings.retrieval_top_k)
        found += self.episodes.search(query, top_k=3)
        return "\n".join(found)

    # ---- procedural
    def matching_skills(self, message: str) -> str:
        """按消息关键词选择 procedural memory, 只把命中的 SKILL.md body 注入 prompt。

        @param message: 当前用户消息, SkillLoader 用它与 name、description 做关键词重合匹配。
        @return: 带 skill 标题的 Markdown 指令块, 没有命中时为空串。
        side effect: SKILL.md 的 mtime 变化时可能触发 SkillLoader 重新扫描目录。
        called by: Session.build_system() 在长期记忆检索之后调用。
        """
        matched = self.skills.match(message)
        return "\n\n".join(f"### {s.name}\n{s.body}" for s in matched)

    # ---- write paths
    def log_chat(self, user_message: str, reply: str, session_id: str = "default",
                 source: str = "cli") -> None:
        """把一次 exchange 写成相邻的 user/assistant 两行, 供 session 历史与 consolidation 共用。

        @param ① user_message: 本轮用户原文。
               ② reply: assistant 回复, 可能附带压缩后的 tool 使用记录。
               ③ session_id: 会话标签, dashboard 切换历史时以此分组。
               ④ source: 消息来源 gateway, 用于统一 inbox 展示。
        side effect: 向 chat_log 插入两行并提交 SQLite 事务。
        called by: Session.add_exchange() 在 loop 得到最终回复后调用。
        """
        # Step 1: user 与 assistant 必须成对、同 session/source 写入, 巩固阈值按两行一个 exchange 计算。
        self.conn.execute(
            "INSERT INTO chat_log (role, content, session_id, source) VALUES ('user', ?, ?, ?)",
            (user_message, session_id, source),
        )
        self.conn.execute(
            "INSERT INTO chat_log (role, content, session_id, source) VALUES ('assistant', ?, ?, ?)",
            (reply, session_id, source),
        )

        # Step 2: 在 return 前提交, 保证 dashboard 和下一次 consolidation 立即看见完整 exchange。
        self.conn.commit()

    # ---- sessions (for the dashboard's chat history + "New chat")
    def session_history(self, session_id: str) -> list[tuple[str, str]]:
        """按写入顺序把一个 session 的 chat_log 行重组为 user/assistant exchange。

        @param session_id: 需要恢复的会话标签。
        @return: 按时间顺序排列的 (user_message, assistant_reply) 元组列表。
        side effect: 只读查询 state.db, 不修改 Session 当前状态。
        called by: Session.switch() 恢复 working history, dashboard session API 生成切换响应。
        """
        # Step 1: 只取目标 session, 使用自增 id 保留真实对话顺序。
        rows = self.conn.execute(
            "SELECT role, content FROM chat_log WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()

        # Step 2: user 行开启 pending exchange, 下一条 assistant 行才形成可恢复的完整 pair。
        pairs, pending = [], None
        for r in rows:
            if r["role"] == "user":
                pending = r["content"]
            elif pending is not None:
                pairs.append((pending, r["content"]))
                pending = None
        return pairs

    def list_sessions(self) -> list[dict]:
        """汇总 chat_log 中的会话标签, 为会话选择器生成标题、数量与时间信息。

        @return: 按最近活动倒序排列的 session 摘要字典列表。
        side effect: 只读查询 state.db, 每个 session 额外读取首条 user 消息作为标题。
        called by: 需要展示 Waku 会话历史的上层接口调用。
        """
        rows = self.conn.execute(
            """SELECT session_id,
                      COUNT(*) AS messages,
                      MIN(created_at) AS started_at,
                      MAX(created_at) AS last_at
               FROM chat_log GROUP BY session_id ORDER BY last_at DESC"""
        ).fetchall()
        out = []
        for r in rows:
            first = self.conn.execute(
                "SELECT content FROM chat_log WHERE session_id = ? AND role = 'user' ORDER BY id LIMIT 1",
                (r["session_id"],),
            ).fetchone()
            out.append({
                "id": r["session_id"],
                "title": (first["content"][:60] if first else "(empty)"),
                "messages": r["messages"],
                "started_at": r["started_at"],
                "last_at": r["last_at"],
            })
        return out

    def export_markdown(self) -> None:
        """把 SQLite 中的 facts 与 episodes 导出为可读 MEMORY.md, 保持本地记忆可检查。

        side effect: 查询 state.db 并覆盖 WAKU_HOME/MEMORY.md, 该文件只是生成视图。
        called by: Waku.respond() 在 chat 持久化与可能的 consolidation 之后调用。
        """
        # Step 1: 从 SQLite source of truth 读取稳定排序的数据, 不把 Markdown 反向当作存储输入。
        facts = self.conn.execute(
            "SELECT subject, content FROM facts ORDER BY subject, id"
        ).fetchall()
        eps = self.conn.execute(
            "SELECT happened_at, summary FROM episodes ORDER BY happened_at DESC, id DESC"
        ).fetchall()

        # Step 2: 组装人类可读视图, facts 按 subject 排序而 episodes 以最近发生优先。
        lines = [
            "# Waku memory",
            "",
            "_A human-readable mirror of what Waku remembers. The source of truth is "
            "`state.db` (the `facts` and `episodes` tables, keyword-searchable via FTS5); "
            "this file is regenerated after every turn._",
            "",
            f"## Facts — semantic memory ({len(facts)})",
            "",
        ]
        lines += [f"- **{f['subject']}** — {f['content']}" for f in facts] or ["_none yet_"]
        lines += ["", f"## Episodes — episodic memory ({len(eps)})", ""]
        lines += [f"- **{e['happened_at']}** — {e['summary']}" for e in eps] or ["_none yet_"]

        # Step 3: 整体覆盖而非追加, 避免重复条目并保证文件始终反映当前 SQLite 快照。
        (self.settings.home / "MEMORY.md").write_text("\n".join(lines) + "\n")

    def maybe_consolidate(self, notify=None) -> None:
        """触发按阈值批量巩固, 并在确有新 fact 时发布 consolidation event。

        @param notify: 可选 Observer, 仅在 new_facts 大于零时接收巩固结果。
        side effect: 可能调用 small model、写 facts/episodes、更新 chat_log 并发送事件。
        called by: Waku.respond() 在每次 exchange 已写入 chat_log 后调用。
        """
        # Step 1: consolidation 模块独立判断是否到期, 未到阈值或解析失败都返回 0。
        new_facts = consolidation.consolidate_if_due(
            self.conn,
            self.client,
            self.settings.small_model,
            self.settings.consolidate_every,
            self.facts,
            self.episodes,
        )

        # Step 2: 只有真正返回非零计数时才发事件, 避免把未到期误报成一次巩固。
        if new_facts and notify:
            notify("consolidation", {"new_facts": new_facts})
