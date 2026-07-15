"""One SQLite file (state.db) holds everything Waku remembers and does.

This mirrors the Hermes approach on the whiteboard: SQLite + FTS5, no server.
Open it yourself anytime:  sqlite3 .waku/state.db '.tables'
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# 这是本地持久化的数据契约: 业务表保存可读真相, FTS5 虚表和 trigger 只负责检索索引。
# connect() 每次启动都会重放这段幂等 DDL, 因此新库创建和旧库补齐共用同一个入口。
SCHEMA = """
-- Flagship-task artifact: events the calendar tool creates. The deterministic
-- eval asserts directly on rows in this table ("did the meeting trigger?").
CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    start TEXT NOT NULL,           -- ISO 8601
    "end" TEXT,
    attendees TEXT DEFAULT '',     -- comma-separated
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

-- Semantic memory: durable facts about you, your people, your projects.
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,         -- who/what the fact is about, e.g. 'alex'
    content TEXT NOT NULL,         -- the fact itself
    source TEXT DEFAULT 'user',    -- 'user' (told directly) or 'consolidation'
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    subject, content, content=facts, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, subject, content) VALUES (new.id, new.subject, new.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, content) VALUES ('delete', old.id, old.subject, old.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, content) VALUES ('delete', old.id, old.subject, old.content);
    INSERT INTO facts_fts(rowid, subject, content) VALUES (new.id, new.subject, new.content);
END;

-- Episodic memory: dated things that happened (past chats, distilled).
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    happened_at TEXT NOT NULL,     -- ISO 8601 date of the episode
    summary TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    summary, content=episodes, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, summary) VALUES (new.id, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, summary) VALUES ('delete', old.id, old.summary);
END;

-- Raw chat log ("save the messages" box). Consolidation reads from here.
-- session_id tags each row with which conversation it belongs to, so the
-- dashboard can offer "New chat" and switch between past sessions (like a
-- chat app). Everything shares this one table — sessions are just a label.
CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,            -- 'user' | 'assistant'
    content TEXT NOT NULL,
    consolidated INTEGER DEFAULT 0,
    session_id TEXT DEFAULT 'default',
    created_at TEXT DEFAULT (datetime('now'))
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """为旧版 state.db 执行可重复运行的增量列迁移, 补齐当前 chat_log 契约。

    @param conn: 已执行基础 SCHEMA 的 SQLite 连接, 迁移会直接作用于该连接。
    side effect: 可能执行 ALTER TABLE 并提交事务, 已是新结构时不写入。
    called by: connect() 在建表和建索引完成后调用。
    """
    # Step 1: 先读取真实表结构, 因为 SQLite 不支持 ADD COLUMN IF NOT EXISTS。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_log)").fetchall()}

    # Step 2: 旧库缺少 session_id 时补列并立即提交, 让会话切换可以安全查询该字段。
    if "session_id" not in cols:
        conn.execute("ALTER TABLE chat_log ADD COLUMN session_id TEXT DEFAULT 'default'")
        conn.commit()

    # Step 3: source 是后加入的 gateway 来源标签, 独立判断保证跨多个历史版本升级仍然幂等。
    if "source" not in cols:
        # which gateway a message came in through (cli / voice / telegram / dashboard)
        conn.execute("ALTER TABLE chat_log ADD COLUMN source TEXT DEFAULT 'cli'")
        conn.commit()


def connect(home: Path, check_same_thread: bool = True) -> sqlite3.Connection:
    """创建并初始化 Waku 的 SQLite 连接, 将目录、schema 和旧库迁移收敛到同一入口。

    @param ① home: WAKU_HOME 路径, state.db 会创建在该目录下。
           ② check_same_thread: 是否启用 sqlite3 的线程归属检查, dashboard 会显式关闭。
    @return: 已配置 Row factory、busy timeout、完整 schema 和迁移结果的连接。
    side effect: 可能创建或升级 state.db, 并执行 DDL 与迁移提交。
    called by: Waku.__init__() 装配运行时, dashboard 的读写 API 也会复用该入口。
    """
    # Step 1: 打开唯一的本地状态文件, 线程策略由 gateway 的并发模型决定。
    # check_same_thread=False lets the dashboard's threaded HTTP server reuse
    # one agent connection across worker threads (guarded by a lock). busy_timeout
    # avoids "database is locked" when the dashboard reads while a chat writes.
    conn = sqlite3.connect(home / "state.db", check_same_thread=check_same_thread)

    # Step 2: 统一查询结果形状并容忍短暂写锁, 让上层按字段名读取且不自行配置连接。
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")

    # Step 3: 先确保当前 schema 存在, 再对历史数据库执行只增不减的列迁移。
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn
