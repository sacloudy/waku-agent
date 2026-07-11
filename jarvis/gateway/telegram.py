"""Telegram gateway — message your laptop from your phone.

Setup (2 minutes, free):
  1. In Telegram, message @BotFather → /newbot → copy the token
  2. Put TELEGRAM_BOT_TOKEN=... in .env
  3. Optionally set TELEGRAM_ALLOWED_USER=<your numeric id> (message
     @userinfobot to get it) so ONLY you can talk to your Waku
  4. make telegram

Long-polling: your laptop calls Telegram's API — no public URL, no webhook,
no server. This is why hobbyist assistants pick Telegram over WhatsApp
(Meta's Cloud API needs business verification and a public HTTPS endpoint).
"""

from __future__ import annotations

import os

from jarvis.app import Jarvis
from jarvis.gateway.cli import _observer  # mirror gate/tool activity on the laptop terminal


def _build_app(token: str, allowed: str):
    """Build the polling app + message handler. Shared by the standalone
    gateway and the background poller `waku dashboard` starts."""
    from telegram import Update
    from telegram.ext import Application, ContextTypes, MessageHandler, filters

    jarvis = Jarvis()

    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if allowed and str(update.effective_user.id) != allowed:
            await update.message.reply_text("This Waku serves someone else. Run your own!")
            return
        print(f"you › {update.message.text}")
        result = jarvis.respond(update.message.text, observer=_observer, source="telegram")
        print(f"waku › {result.reply}")
        await update.message.reply_text(result.reply or "(no reply)")

    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    return app


def main() -> None:
    try:
        import telegram  # noqa: F401
    except ImportError:
        raise SystemExit("Telegram extra not installed: pip install 'launch-jarvis[telegram]'")

    from jarvis.config import load_settings

    token = load_settings().telegram_token
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env (message @BotFather to create a bot).")
    app = _build_app(token, os.getenv("TELEGRAM_ALLOWED_USER", ""))
    print("Waku is listening on Telegram — message your bot. Ctrl-C to stop.")
    app.run_polling()


def start_in_background() -> bool:
    """Start the Telegram poller on a daemon thread — so `waku dashboard` runs
    the browser cockpit AND Telegram from one command. Returns True if started,
    False (quietly) if there's no token or the extra isn't installed. Never
    raises: a gateway problem must not take down the dashboard."""
    from jarvis.config import load_settings

    token = load_settings().telegram_token
    if not token:
        return False
    try:
        import telegram  # noqa: F401
    except ImportError:
        print("(telegram) TELEGRAM_BOT_TOKEN is set but the extra isn't installed — "
              "pip install 'launch-jarvis[telegram]'")
        return False

    import asyncio
    import threading

    allowed = os.getenv("TELEGRAM_ALLOWED_USER", "")

    def run() -> None:
        # its own event loop on this thread; start_polling is non-blocking, then
        # run_forever keeps it alive until the process (a daemon thread) exits.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            app = _build_app(token, allowed)
            loop.run_until_complete(app.initialize())
            loop.run_until_complete(app.start())
            loop.run_until_complete(app.updater.start_polling())
            loop.run_forever()
        except Exception as exc:  # noqa: BLE001 — isolate the dashboard from bot errors
            print(f"(telegram) background poller stopped: {exc}")

    threading.Thread(target=run, daemon=True, name="telegram-poll").start()
    return True


if __name__ == "__main__":
    main()
