"""create_event — the flagship tool. "Did the meeting trigger?" is THE
deterministic eval: it either wrote the right row or it didn't.

Where events land:
  always      state.db (the eval asserts here) + calendar.ics (importable file)
  opt-in      Apple Calendar, in a dedicated "Waku" calendar, via AppleScript —
              set WAKU_APPLE_CALENDAR=1. First use makes macOS ask permission
              for your terminal to control Calendar; approve once.

The tool's return string always says exactly where the event went — the model
relays it, so Waku never over-claims what happened.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from waku.tools.registry import Tool

APPLE_CALENDAR_NAME = "Waku"


def _write_ics(home: Path, title: str, start: str, end: str, attendees: str) -> None:
    """
    把已通过幂等检查的事件追加到本地 calendar.ics 导出视图。

    @param ① home: Waku runtime 目录, 决定 calendar.ics 的落盘位置。
           ② title: 已写入 SQLite 的事件标题。
           ③ start: 已归一化到分钟的 ISO 开始时间。
           ④ end: 已归一化到分钟的 ISO 结束时间。
           ⑤ attendees: 原样写入 DESCRIPTION 的参与者文本。
    side effect: 读取并整体重写 calendar.ics, 保持文件末尾只有一个 END:VCALENDAR。
    called by: make_tool() 创建的 create_event 在 SQLite commit 成功后调用。
    """
    ics_path = home / "calendar.ics"

    def dt(s: str) -> str:
        return s.replace("-", "").replace(":", "") + ("00" if len(s) == 16 else "")

    # Step 1: 先构造独立 VEVENT, ISO 时间在这里转换成 ICS 的紧凑格式。
    event = (
        "BEGIN:VEVENT\n"
        f"SUMMARY:{title}\n"
        f"DTSTART:{dt(start)}\n"
        f"DTEND:{dt(end)}\n"
        f"DESCRIPTION:attendees: {attendees}\n"
        "END:VEVENT\n"
    )
    # Step 2: 去掉旧文件尾标记后追加事件, 再一次性恢复 VCALENDAR 尾标记。
    if ics_path.exists():
        body = ics_path.read_text().replace("END:VCALENDAR\n", "")
    else:
        body = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//waku-agent//EN\n"
    ics_path.write_text(body + event + "END:VCALENDAR\n")


def _applescript_date(var: str, iso: str) -> str:
    """
    把 ISO 时间拆成不依赖系统 locale 的 AppleScript date 赋值语句。

    @param ① var: AppleScript 中接收 date 的变量名。
           ② iso: 可由 datetime.fromisoformat() 解析的时间字符串。
    @return: 逐字段设置年月日时分秒的 AppleScript 片段。
    side effect: 无。
    called by: sync_to_apple_calendar() 分别构造 startDate 和 endDate。
    """
    d = datetime.fromisoformat(iso)
    # 先把 day 固定到 1 再设置 month/year, 防止当前日期为 31 日时跨入错误月份。
    return (
        f"set {var} to current date\nset day of {var} to 1\n"
        f"set year of {var} to {d.year}\nset month of {var} to {d.month}\n"
        f"set day of {var} to {d.day}\nset hours of {var} to {d.hour}\n"
        f"set minutes of {var} to {d.minute}\nset seconds of {var} to 0\n"
    )


def sync_to_apple_calendar(title: str, start: str, end: str, notes: str = "") -> str:
    """
    在本地写入完成后尝试同步 Calendar.app, 并返回可直接交给模型的真实结果说明。

    @param ① title: 事件标题, 进入 AppleScript 前会移除反斜杠并替换双引号。
           ② start: 已归一化的 ISO 开始时间。
           ③ end: 已归一化的 ISO 结束时间。
           ④ notes: 可选事件描述, 使用与标题相同的转义策略。
    @return: 成功、跳过、超时或失败的可读文本, 不会声称本地事件丢失。
    side effect: 在 macOS 上运行 osascript, 可能创建 calendar 并写入真实 Calendar.app。
    called by: create_event 在 WAKU_APPLE_CALENDAR 启用且本地两层写入成功后调用。
    """
    # 非 macOS 不进入 subprocess 边界, 但仍返回明确状态供 tool 输出如实说明。
    if sys.platform != "darwin":
        return "Apple Calendar sync skipped (not macOS)."

    # Step 1: 先清理会破坏 AppleScript string literal 的字符, 再拼接日期与事件脚本。
    safe_title = title.replace("\\", "").replace('"', "'")
    safe_notes = notes.replace("\\", "").replace('"', "'")
    # 优先使用专属 Waku calendar。某些 iCloud-only 账户不允许 AppleScript 新建 calendar,
    # 因此失败后回退到第一个 writable calendar, 并在返回值中报告真实落点。
    script = (
        _applescript_date("startDate", start)
        + _applescript_date("endDate", end)
        + f'''
tell application "Calendar"
  if not (exists calendar "{APPLE_CALENDAR_NAME}") then
    try
      make new calendar with properties {{name:"{APPLE_CALENDAR_NAME}"}}
      delay 1
    end try
  end if
  if exists calendar "{APPLE_CALENDAR_NAME}" then
    set targetCal to calendar "{APPLE_CALENDAR_NAME}"
  else
    set targetCal to first calendar whose writable is true
  end if
  tell targetCal
    make new event with properties {{summary:"{safe_title}", start date:startDate, end date:endDate, description:"{safe_notes}"}}
  end tell
  return name of targetCal
end tell'''
    )
    # Step 2: 从这里开始产生真实系统副作用。超时和权限错误只影响第三层同步,
    # create_event 已完成的 SQLite 与 ICS 写入不会回滚。
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired:
        return (
            "Apple Calendar sync timed out — this usually means macOS is showing a "
            "permission dialog ('would like to add to your Calendar'). The event is safe "
            "in the local calendar; approve the dialog and ask me to create it again."
        )
    except OSError as exc:
        return f"Apple Calendar sync FAILED ({exc}) — the event is still in the local calendar."
    # Step 3: osascript 的非零退出码也被归一化为 tool 可观察文本, 避免模型过度承诺。
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[:120]
        return (
            f"Apple Calendar sync FAILED ({detail}) — the event is still in the local "
            "calendar. If this is a permissions error, allow your terminal to control "
            "Calendar in System Settings > Privacy & Security > Automation."
        )
    used = (result.stdout or "").strip() or APPLE_CALENDAR_NAME
    return f"Also added to Apple Calendar (calendar '{used}')."


def make_tool(conn: sqlite3.Connection, home: Path, apple_calendar: bool = False) -> Tool:
    """
    构造 create_event Tool, 并把 SQLite、runtime home 和 Apple 同步开关闭包进去。

    @param ① conn: calendar_events 表所在的共享 SQLite 连接。
           ② home: ICS 文件和本地状态所在的 runtime 目录。
           ③ apple_calendar: 是否在本地写入后继续同步 Calendar.app。
    @return: 可注册到 ToolRegistry 的 create_event Tool。
    side effect: 仅创建闭包和 schema, 此时不写数据库、文件或 Calendar.app。
    called by: build_registry() 装配核心 tool, demo_seed 直接取得 fn 造演示数据。
    """
    def create_event(title: str = "", start: str = "", end: str = "", attendees: str = "", notes: str = "") -> str:
        """
        校验并幂等创建事件, 顺序执行 SQLite、ICS 和可选 Apple Calendar 三层副作用。

        @param ① title: 模型生成的事件标题, 也是幂等 key 的一部分。
               ② start: 模型生成的 ISO 开始时间, 会截断到分钟并参与幂等判断。
               ③ end: 可选 ISO 结束时间, 为空时默认 start 加一小时。
               ④ attendees: 可选参与者文本, 写入数据库、ICS 和最终说明。
               ⑤ notes: 可选事件备注, 写入数据库并传给 Apple Calendar。
        @return: 可供模型观察的创建、重复或参数修复说明。
        side effect: 首次事件会 commit SQLite、重写 ICS, 并可能调用真实 Calendar.app。
        called by: ToolRegistry.execute() 在模型请求 create_event 时调用。
        """
        # Step 1: 模型可能发出空或部分 tool call, 这里返回可修复文本而不是抛出 TypeError。
        if not title or not start:
            return ("create_event needs at least a title and a start time "
                    "(ISO 8601, e.g. 2026-07-14T09:00). Please call it again with both.")
        if not end:
            # 未给 end 时用一小时默认值, 让后续三层写入共享同一个确定区间。
            from datetime import timedelta
            end = (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat(timespec="minutes")

        # Step 2: 先把秒级差异归一化到分钟, 再用精确 title + start 作为幂等 key。
        # 命中时在任何写入前返回, 因而 SQLite、ICS 和 Apple Calendar 都不会重复执行。
        start = start[:16]  # normalize 2026-07-11T17:00:00 → 2026-07-11T17:00
        end = end[:16]
        existing = conn.execute(
            "SELECT id FROM calendar_events WHERE title = ? AND start = ?", (title, start)
        ).fetchone()
        if existing:
            return f"Event '{title}' at {start} already exists (not duplicated)."

        # Step 3: SQLite 是可查询的本地事实源。先 commit 再导出文件, 后续失败不会撤销这条记录。
        conn.execute(
            'INSERT INTO calendar_events (title, start, "end", attendees, notes) VALUES (?,?,?,?,?)',
            (title, start, end, attendees, notes),
        )
        conn.commit()
        # Step 4: ICS 是始终生成的人类可携带视图, 它复用已经归一化并持久化的事件字段。
        _write_ics(home, title, start, end, attendees)

        # Step 5: Apple Calendar 是 opt-in 第三层。返回文本精确区分未启用、成功和失败。
        where = f"Saved to the local calendar ({home / 'calendar.ics'})."
        if apple_calendar:
            where += " " + sync_to_apple_calendar(title, start, end, notes)
        else:
            where += (
                " Not synced to any calendar app (enable with WAKU_APPLE_CALENDAR=1, "
                f"or import manually: open {home / 'calendar.ics'})."
            )
        return (
            f"Event created: '{title}' {start} → {end}"
            + (f" with {attendees}" if attendees else "")
            + f". {where}"
        )

    return Tool(
        name="create_event",
        description=(
            "Create a calendar event on the user's local calendar. Use whenever the user "
            "wants to schedule, book, or plan something at a specific time."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short event title"},
                "start": {"type": "string", "description": "Start time, ISO 8601, e.g. 2026-07-14T09:00"},
                "end": {"type": "string", "description": "End time, ISO 8601. Defaults to start + 1h."},
                "attendees": {"type": "string", "description": "Comma-separated names/emails"},
                "notes": {"type": "string", "description": "Optional context for the event"},
            },
            "required": ["title", "start"],
        },
        fn=create_event,
    )


def make_list_tool(conn: sqlite3.Connection) -> Tool:
    """
    构造 list_events Tool, 为 create_event 的 SQLite 写入提供可查询读侧。

    @param conn: calendar_events 表所在的共享 SQLite 连接。
    @return: 可注册到 ToolRegistry 的 list_events Tool。
    side effect: 仅创建闭包和 schema, 此时不执行查询。
    called by: build_registry() 装配核心 tool, dashboard.tools_info() 展示 tool catalog。
    """
    def list_events(start: str = "", end: str = "", limit: int = 20) -> str:
        """
        根据可选日期窗口查询本地事件, 并格式化成模型可直接回答用户的文本。

        @param ① start: 可选起始日期或时间, 日期部分作为包含式下界。
               ② end: 可选结束日期或时间, 日期部分扩展到当天 23:59 作为包含式上界。
               ③ limit: 最大返回条数, 被限制在 1 到 100 之间。
        @return: 有序事件列表或明确的无结果说明。
        side effect: 执行只读 SQLite 查询, 不修改 calendar 状态。
        called by: ToolRegistry.execute() 在模型请求 list_events 时调用。
        """
        # Step 1: 只为实际提供的边界追加条件, 保持无参数时能列出全部本地事件。
        query = 'SELECT title, start, "end", attendees FROM calendar_events'
        clauses, params = [], []
        if start:
            clauses.append("start >= ?")
            params.append(start[:10])                 # inclusive from the start of that day
        if end:
            clauses.append("start <= ?")
            params.append(end[:10] + "T23:59")        # inclusive through the end of that day
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        # Step 2: limit 在进入 SQL 前收敛到安全范围, 结果始终按开始时间排序。
        query += " ORDER BY start LIMIT ?"
        params.append(max(1, min(int(limit or 20), 100)))
        rows = conn.execute(query, params).fetchall()
        # Step 3: 查询结果转换为稳定文本协议, loop 不需要理解 sqlite3.Row。
        if not rows:
            window = f" between {start} and {end}" if (start or end) else ""
            return f"No events found{window}. (This reads the local calendar in .waku/state.db.)"
        lines = ["Events on the local calendar:"]
        for r in rows:
            who = f" with {r['attendees']}" if r["attendees"] else ""
            lines.append(f"- {r['title']}: {r['start']} → {r['end']}{who}")
        return "\n".join(lines)

    return Tool(
        name="list_events",
        description=(
            "Read the user's calendar: list events, optionally within a date range. "
            "Use whenever the user asks what's on their calendar / schedule for a day, "
            "week, yesterday, etc. Dates are ISO (e.g. 2026-07-10); omit both to list "
            "everything upcoming. For 'yesterday'/'today' resolve the date from the "
            "current time given in your system prompt."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "earliest date to include, ISO (e.g. 2026-07-10)"},
                "end": {"type": "string", "description": "latest date to include, ISO (e.g. 2026-07-10)"},
                "limit": {"type": "integer", "description": "max events to return (default 20)"},
            },
            "required": [],
        },
        fn=list_events,
    )
