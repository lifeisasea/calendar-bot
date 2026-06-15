"""
Telegram бот-календарь.

Шаг 1 ✅ — бот оживает.
Шаг 2 ✅ — читает Google Календарь: «что у меня сегодня / завтра / на неделе».
Дальше: запись событий (Шаг 3) и умная понималка через Claude (Шаг 4).
"""
import datetime as dt
import logging
import os
import socket

# На некоторых хостингах IPv6 «висит» (api.telegram.org становится недоступен).
# Принудительно используем только IPv4.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, *args, **kwargs):
    res = _orig_getaddrinfo(host, *args, **kwargs)
    ipv4 = [r for r in res if r[0] == socket.AF_INET]
    return ipv4 or res


socket.getaddrinfo = _ipv4_only

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import google_client as gc

load_dotenv(override=True)  # значения из .env имеют приоритет над системным окружением
TOKEN = os.environ.get("TELEGRAM_TOKEN")
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID")
SMART = bool(os.environ.get("ANTHROPIC_API_KEY"))  # умный режим, если есть ключ Claude

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("calendar-bot")


def start_health_server() -> None:
    """Маленький HTTP-сервер для проверки «жив ли бот» (нужно хостингу Koyeb)."""
    import http.server
    import threading

    port = int(os.environ.get("PORT", "8000"))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"calendar-bot alive")

        def log_message(self, *args):  # тишина в логах
            pass

    srv = http.server.HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Health-сервер слушает порт %s", port)


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(ALLOWED_USER_ID)


async def deny(update: Update) -> None:
    await update.message.reply_text("Извини, это личный бот 🙈")


WEEKDAYS = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]


async def keepalive(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Самопинг: держим бесплатный Render бодрым, чтобы напоминания шли вовремя."""
    if not PUBLIC_URL:
        return
    import httpx

    base = PUBLIC_URL if PUBLIC_URL.startswith("http") else f"https://{PUBLIC_URL}"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            await c.get(base)
    except Exception:  # noqa: BLE001 — 404/любой ответ ок, нам нужен сам факт запроса
        pass


_fired_reminders: dict = {}  # id повторяющегося срабатывания -> когда отправили (защита от дублей)


async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в минуту: если у напоминания наступило время — пишем в Telegram."""
    if not ALLOWED_USER_ID:
        return
    try:
        now = dt.datetime.now(gc.TZ)
        for r in gc.due_reminders(now):
            if r["recurring"] and r["id"] in _fired_reminders:
                continue  # это срабатывание уже отправляли
            await context.bot.send_message(
                chat_id=int(ALLOWED_USER_ID),
                text=f"⏰ Напоминание: {r['text']}",
            )
            log.info("Отправлено напоминание: %s", r["text"])
            if r["recurring"]:
                _fired_reminders[r["id"]] = now.timestamp()
            else:
                gc.delete_reminder(r["id"])  # разовое — убираем, чтобы не повторялось
        # чистим старые записи защиты от дублей
        cutoff = now.timestamp() - 3600
        for k in [k for k, v in _fired_reminders.items() if v < cutoff]:
            del _fired_reminders[k]
    except Exception:  # noqa: BLE001
        log.exception("Ошибка проверки напоминаний")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    context.chat_data["history"] = []
    await update.message.reply_text("Память диалога очищена 🧹 Начнём с чистого листа.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    context.chat_data["history"] = []
    await update.message.reply_text(
        "Привет! 🤖 Я твой бот-календарь.\n\n"
        "Уже умею показывать твои планы. Спроси меня:\n"
        "• «что у меня сегодня?»\n"
        "• «что завтра?»\n"
        "• «что на неделе?»\n\n"
        "Скоро научусь ещё и записывать встречи прямо отсюда."
    )


def week_summary(start_day: dt.date) -> str:
    lines = []
    for i in range(7):
        day = start_day + dt.timedelta(days=i)
        evs = gc.events_for_day(day)
        if evs == "свободно ✨":
            continue
        name = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][day.weekday()]
        lines.append(f"*{name} {day.day}.{day.month:02d}*\n{evs}")
    if not lines:
        return "На ближайшую неделю всё свободно ✨"
    return "\n\n".join(lines)


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)
    raw = update.message.text or ""
    text = raw.lower()
    today = dt.datetime.now(gc.TZ).date()
    log.info("Запрос: %s", raw)

    # Умный режим: всё отдаём Claude (с памятью диалога)
    if SMART:
        await context.bot.send_chat_action(update.effective_chat.id, "typing")
        try:
            import asyncio
            import nlu
            history = context.chat_data.setdefault("history", [])
            history.append({"role": "user", "content": raw})
            reply = await asyncio.to_thread(nlu.answer, list(history))
            history.append({"role": "assistant", "content": reply})
            # помним только последние ~16 реплик
            if len(history) > 16:
                del history[:-16]
            await update.message.reply_text(reply)
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка умного режима")
            await update.message.reply_text(f"Ой, что-то сломалось 😕\n{e}")
        return

    try:
        if "послезавтра" in text:
            day = today + dt.timedelta(days=2)
            await update.message.reply_text(f"📅 Послезавтра:\n{gc.events_for_day(day)}")
        elif "завтра" in text:
            day = today + dt.timedelta(days=1)
            await update.message.reply_text(f"📅 Завтра:\n{gc.events_for_day(day)}")
        elif "сегодня" in text or "сейчас" in text:
            await update.message.reply_text(f"📅 Сегодня:\n{gc.events_for_day(today)}")
        elif "недел" in text:
            await update.message.reply_text(
                f"🗓 На неделе:\n\n{week_summary(today)}", parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "Пока я понимаю вопросы про расписание. Попробуй:\n"
                "• «что сегодня?»\n• «что завтра?»\n• «что на неделе?»\n\n"
                "(умные фразы и запись встреч добавим на следующих шагах)"
            )
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка при обращении к календарю")
        await update.message.reply_text(
            "Ой, не получилось достучаться до календаря 😕\n"
            f"Техническая деталь: {e}"
        )


# Публичный адрес для webhook (на хостинге). Render сам отдаёт RENDER_EXTERNAL_URL.
PUBLIC_URL = (
    os.environ.get("WEBHOOK_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or os.environ.get("KOYEB_PUBLIC_DOMAIN")
)
PORT = int(os.environ.get("PORT", "8000"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "tg-calendar-hook")


def main() -> None:
    if not TOKEN:
        raise SystemExit("❌ Нет TELEGRAM_TOKEN в .env")
    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .get_updates_read_timeout(40)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    if app.job_queue:
        app.job_queue.run_repeating(check_reminders, interval=60, first=15)
        log.info("Проверка напоминаний включена (каждые 60 сек)")
        if PUBLIC_URL:
            app.job_queue.run_repeating(keepalive, interval=600, first=60)
            log.info("Самопинг включён (каждые 10 мин)")
    gc.refresh_tz()
    try:
        moved = gc.rollover_tasks()
        if moved:
            log.info("Перенесено просроченных задач на сегодня: %s", moved)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось перенести задачи при старте")
    log.info("Часовой пояс: %s. Умный режим: %s", gc.TZ, "вкл" if SMART else "выкл")

    if PUBLIC_URL:
        base = PUBLIC_URL if PUBLIC_URL.startswith("http") else f"https://{PUBLIC_URL}"
        url = f"{base}/{WEBHOOK_SECRET}"
        log.info("Режим webhook: %s (порт %s)", url, PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_SECRET,
            webhook_url=url,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        start_health_server()
        log.info("Режим polling. Жду сообщений в Telegram…")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
