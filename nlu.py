"""
Умная понималка через Claude API.

Принимает обычную фразу на русском и сам решает:
читать календарь или создать событие — через «инструменты» (tool use).
"""
from __future__ import annotations

import datetime as dt
import os

from anthropic import Anthropic

import google_client as gc

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
_client: Anthropic | None = None


def client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic()  # ключ берётся из ANTHROPIC_API_KEY
    return _client


TOOLS = [
    {
        "name": "list_events",
        "description": "Показать события календаря за период (включительно). Даты в формате ГГГГ-ММ-ДД.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Начало периода, ГГГГ-ММ-ДД"},
                "date_to": {"type": "string", "description": "Конец периода включительно, ГГГГ-ММ-ДД"},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "find_events",
        "description": (
            "Найти события по тексту в названии за период (например, найти концерт, "
            "встречу, рейс). Возвращает названия, время начала и конца. "
            "Используй, когда нужно опереться на уже существующее событие."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Слово или фраза из названия"},
                "date_from": {"type": "string", "description": "Начало периода, ГГГГ-ММ-ДД"},
                "date_to": {"type": "string", "description": "Конец периода включительно, ГГГГ-ММ-ДД"},
            },
            "required": ["query", "date_from", "date_to"],
        },
    },
    {
        "name": "update_event",
        "description": (
            "Изменить существующее событие (перенести время/дату, переименовать, сменить категорию). "
            "Сначала найди событие через find_events и возьми его event_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID события из find_events"},
                "new_date": {"type": "string", "description": "Новая дата ГГГГ-ММ-ДД (если переносим)"},
                "new_start_time": {"type": "string", "description": "Новое время начала ЧЧ:ММ"},
                "new_end_time": {"type": "string", "description": "Новое время конца ЧЧ:ММ"},
                "new_title": {"type": "string", "description": "Новое название"},
                "category": {"type": "string", "enum": ["личное", "работа", "срочно"]},
                "no_reminder": {"type": "boolean"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "delete_event",
        "description": "Удалить событие. Сначала найди его через find_events и возьми event_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID события из find_events"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "add_reminder",
        "description": (
            "Поставить напоминание, чтобы БОТ сам написал в Telegram в указанное время "
            "(для просьб «напомни мне …»). Это не всплывашка календаря, а сообщение от бота."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "О чём напомнить"},
                "date": {"type": "string", "description": "Дата ГГГГ-ММ-ДД"},
                "time": {"type": "string", "description": "Время ЧЧ:ММ"},
            },
            "required": ["text", "date", "time"],
        },
    },
    {
        "name": "add_recurring_reminder",
        "description": (
            "Повторяющееся напоминание от бота в Telegram: «каждый час», «каждый день в 8:00», "
            "«по будням в 18:00», «каждый понедельник в 10:00»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "О чём напоминать"},
                "freq": {"type": "string", "enum": ["hourly", "daily", "weekly"]},
                "time": {"type": "string", "description": "Время ЧЧ:ММ (для daily/weekly; для hourly — минута, напр. 00:00)"},
                "hour_from": {"type": "integer", "description": "Для hourly: с какого часа (по умолч. 9)"},
                "hour_to": {"type": "integer", "description": "Для hourly: по какой час (по умолч. 21)"},
                "weekdays": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]},
                    "description": "Для weekly: дни недели",
                },
            },
            "required": ["text", "freq"],
        },
    },
    {
        "name": "cancel_reminder",
        "description": "Отключить напоминание по тексту («перестань напоминать про воду») или ВСЕ сразу (query='все').",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Слово/фраза из напоминания, или 'все' чтобы удалить все"}},
            "required": ["query"],
        },
    },
    {
        "name": "add_task",
        "description": (
            "Добавить задачу (дело с галочкой, не событие со временем). "
            "Без срока — попадает в бэклог. Можно отметить срочной."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Что нужно сделать"},
                "due_date": {"type": "string", "description": "Срок ГГГГ-ММ-ДД (если есть; иначе бэклог)"},
                "urgent": {"type": "boolean", "description": "true — срочная задача 🔥"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_tasks",
        "description": "Показать задачи. scope: today (на сегодня + просроченные), backlog (без срока), overdue, all.",
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["today", "backlog", "overdue", "all"]},
            },
            "required": ["scope"],
        },
    },
    {
        "name": "complete_task",
        "description": "Отметить задачу выполненной. Сначала найди её через list_tasks и возьми task_id.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "ID задачи из list_tasks"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "delete_task",
        "description": "Удалить задачу. Сначала найди её через list_tasks и возьми task_id.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "ID задачи из list_tasks"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "create_event",
        "description": (
            "Создать событие в календаре. Можно задать время начала и либо длительность, "
            "либо явное время конца. Поддерживает отключение напоминаний."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название события"},
                "date": {"type": "string", "description": "Дата, ГГГГ-ММ-ДД"},
                "start_time": {"type": "string", "description": "Время начала ЧЧ:ММ"},
                "end_time": {"type": "string", "description": "Время конца ЧЧ:ММ (необязательно)"},
                "duration_minutes": {"type": "integer", "description": "Длительность в минутах, если не задан end_time (по умолчанию 60)"},
                "all_day": {"type": "boolean", "description": "true, если событие на весь день"},
                "category": {
                    "type": "string",
                    "enum": ["личное", "работа", "срочно"],
                    "description": "Категория для цвета события",
                },
                "no_reminder": {"type": "boolean", "description": "true — без напоминания"},
                "reminder_minutes": {"type": "integer", "description": "За сколько минут напомнить (если нужно конкретное напоминание)"},
            },
            "required": ["title", "date"],
        },
    },
]


def _run_list_events(args: dict) -> str:
    d_from = dt.date.fromisoformat(args["date_from"])
    d_to = dt.date.fromisoformat(args["date_to"])
    parts = []
    day = d_from
    while day <= d_to:
        evs = gc.events_for_day(day)
        name = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][day.weekday()]
        parts.append(f"{name} {day.isoformat()}: {evs}")
        day += dt.timedelta(days=1)
    return "\n".join(parts)


def _run_find_events(args: dict) -> str:
    d_from = dt.datetime.combine(dt.date.fromisoformat(args["date_from"]), dt.time.min, tzinfo=gc.TZ)
    d_to = dt.datetime.combine(dt.date.fromisoformat(args["date_to"]) + dt.timedelta(days=1), dt.time.min, tzinfo=gc.TZ)
    evs = gc.search_events(args["query"], d_from, d_to)
    if not evs:
        return f"Ничего не нашёл по запросу «{args['query']}» в этом периоде."
    lines = []
    for e in evs:
        s, en = gc.event_times(e)
        lines.append(f"id={e['id']} | «{e.get('summary', '(без названия)')}»: начало {s}, конец {en}")
    return "\n".join(lines)


def _run_update_event(args: dict) -> str:
    date = dt.date.fromisoformat(args["new_date"]) if args.get("new_date") else None
    start = end = None
    if args.get("new_start_time"):
        if not date:
            return "Нужна и дата (new_date), если меняешь время."
        hh, mm = map(int, args["new_start_time"].split(":"))
        start = dt.datetime.combine(date, dt.time(hh, mm), tzinfo=gc.TZ)
    if args.get("new_end_time") and date:
        eh, em = map(int, args["new_end_time"].split(":"))
        end = dt.datetime.combine(date, dt.time(eh, em), tzinfo=gc.TZ)
        if start and end <= start:
            end += dt.timedelta(days=1)
    gc.update_event(
        args["event_id"], start=start, end=end,
        summary=args.get("new_title"), category=args.get("category"),
        no_reminder=args.get("no_reminder"),
    )
    return "Событие обновлено ✅"


def _run_delete_event(args: dict) -> str:
    gc.delete_event(args["event_id"])
    return "Событие удалено ✅"


def _fmt_task(t: dict) -> str:
    d = gc._due_date(t)
    today = dt.datetime.now(gc.TZ).date()
    if not d:
        due = "без срока"
    elif d == today:
        due = "сегодня"
    else:
        due = d.isoformat()
    return f"id={t['id']} | {t.get('title', '(без названия)')} (срок: {due})"


def _run_add_reminder(args: dict) -> str:
    d = dt.date.fromisoformat(args["date"])
    hh, mm = map(int, args["time"].split(":"))
    when = dt.datetime.combine(d, dt.time(hh, mm), tzinfo=gc.TZ)
    gc.create_reminder(args["text"], when)
    return f"Напоминание поставлено на {args['date']} {args['time']} ⏰"


def _run_add_recurring_reminder(args: dict) -> str:
    n = gc.create_recurring_reminder(
        args["text"],
        args["freq"],
        time=args.get("time", "09:00"),
        hour_from=args.get("hour_from", 9),
        hour_to=args.get("hour_to", 21),
        weekdays=args.get("weekdays"),
    )
    return f"Повторяющееся напоминание поставлено ({args['freq']}, правил: {n}) 🔁"


def _run_cancel_reminder(args: dict) -> str:
    q = (args.get("query") or "").strip()
    # «удали все напоминания» — чистим всё
    if not q or q.lower() in {"все", "всё", "all", "все напоминания", "всё напоминания"}:
        n = gc.cancel_reminders(None)
        return f"Удалил все напоминания: {n} ✅" if n else "Напоминаний и так нет 👌"
    n = gc.cancel_reminders(q)
    return f"Отключено напоминаний: {n} ✅" if n else "Не нашёл такого напоминания."


def _run_add_task(args: dict) -> str:
    due = dt.date.fromisoformat(args["due_date"]) if args.get("due_date") else None
    gc.add_task(args["title"], due=due, urgent=args.get("urgent", False))
    where = "в бэклог" if due is None else f"на {due.isoformat()}"
    return f"Задача добавлена {where} ✅"


def _run_list_tasks(args: dict) -> str:
    gc.rollover_tasks()
    items = gc.tasks_view(args.get("scope", "today"))
    if not items:
        return "Задач нет 🎉"
    return "\n".join(_fmt_task(t) for t in items)


def _run_complete_task(args: dict) -> str:
    gc.complete_task(args["task_id"])
    return "Отметил выполненной ✅"


def _run_delete_task(args: dict) -> str:
    gc.delete_task(args["task_id"])
    return "Задача удалена ✅"


def _run_create_event(args: dict) -> str:
    date = dt.date.fromisoformat(args["date"])
    all_day = args.get("all_day", False) or not args.get("start_time")
    if all_day:
        start = dt.datetime.combine(date, dt.time.min, tzinfo=gc.TZ)
        gc.create_event(
            args["title"], start, all_day=True, category=args.get("category"),
            no_reminder=args.get("no_reminder", False),
            reminder_minutes=args.get("reminder_minutes"),
        )
        return f"Создано (весь день): {args['title']} на {date.isoformat()}"
    hh, mm = map(int, args["start_time"].split(":"))
    start = dt.datetime.combine(date, dt.time(hh, mm), tzinfo=gc.TZ)
    if args.get("end_time"):
        eh, em = map(int, args["end_time"].split(":"))
        end = dt.datetime.combine(date, dt.time(eh, em), tzinfo=gc.TZ)
        if end <= start:  # конец на следующий день
            end += dt.timedelta(days=1)
    else:
        end = start + dt.timedelta(minutes=args.get("duration_minutes", 60))
    gc.create_event(
        args["title"], start, end, category=args.get("category"),
        no_reminder=args.get("no_reminder", False),
        reminder_minutes=args.get("reminder_minutes"),
    )
    return f"Создано: {args['title']} {date.isoformat()} {args['start_time']}–{end.strftime('%H:%M')}"


def _execute(name: str, args: dict) -> str:
    if name == "list_events":
        return _run_list_events(args)
    if name == "find_events":
        return _run_find_events(args)
    if name == "create_event":
        return _run_create_event(args)
    if name == "update_event":
        return _run_update_event(args)
    if name == "delete_event":
        return _run_delete_event(args)
    if name == "add_reminder":
        return _run_add_reminder(args)
    if name == "add_recurring_reminder":
        return _run_add_recurring_reminder(args)
    if name == "cancel_reminder":
        return _run_cancel_reminder(args)
    if name == "add_task":
        return _run_add_task(args)
    if name == "list_tasks":
        return _run_list_tasks(args)
    if name == "complete_task":
        return _run_complete_task(args)
    if name == "delete_task":
        return _run_delete_task(args)
    return f"Неизвестный инструмент: {name}"


def answer(history) -> str:
    """
    Главная функция: история диалога → ответ бота.

    history — список реплик вида {"role": "user"/"assistant", "content": "текст"}.
    Последняя реплика — свежее сообщение пользователя.
    """
    now = dt.datetime.now(gc.TZ)
    system = (
        "Ты — дружелюбный помощник-календарь в Telegram. Общаешься на русском, коротко и по делу, "
        "можно с лёгким юмором и эмодзи. "
        f"Сейчас {now.strftime('%A, %Y-%m-%d %H:%M')}, часовой пояс {gc.TZ}. "
        "Понимай относительные даты («завтра», «в пятницу», «через неделю») от текущей даты. "
        "Если пользователь спрашивает про планы — используй list_events и перескажи результат человечно. "
        "Если просит что-то запланировать на конкретное время (встреча, созвон, рейс) — create_event. "
        "Если это дело-галочка без точного времени («купить молоко», «позвонить маме», «не забыть…», "
        "«сделать до пятницы») — это ЗАДАЧА: используй add_task. Дело без даты идёт в бэклог. "
        "Срочные дела помечай urgent=true. Чтобы показать дела — list_tasks; отметить сделанным — "
        "complete_task; убрать — delete_task. Невыполненные задачи сами переносятся на сегодня. "
        "Если просят «напомни мне …» (чтобы бот сам написал в Telegram в нужный момент) — "
        "используй add_reminder. Если же нужна всплывашка-напоминание перед уже создаваемым "
        "событием — это reminder_minutes у create_event. "
        "Повторяющиеся напоминания («каждый час», «каждый день в 8», «по будням в 18:00») — "
        "add_recurring_reminder (для hourly можно задать диапазон часов hour_from/hour_to). "
        "Отключить напоминание — cancel_reminder. "
        "ВАЖНО: у обычных событий НЕ ставь reminder_minutes, если пользователь явно не попросил "
        "напоминание/уведомление у этого события. По умолчанию событие создаётся без уведомлений Google. "
        "Если просьба опирается на уже существующее событие («перед концертом», «после рейса», "
        "«найди сам») — сначала найди его через find_events, возьми его время, и только потом считай. "
        "При поиске бери окно в несколько дней вокруг названной даты (события ночью могут попадать "
        "на следующую дату) и пробуй ключевые слова и по-русски, и по-английски "
        "(концерт→concert/jazz/night и т.п.). "
        "Например, «дорога 1.5ч перед и после концерта» = два события по 90 минут: одно заканчивается "
        "к началу концерта, другое начинается в конце концерта. "
        "Можешь делать несколько действий подряд (найти, затем создать пару событий). "
        "Если просят «без напоминания» — ставь no_reminder=true. "
        "Уточняй коротким вопросом, только если данных реально не хватает и их негде взять. "
        "После создания/изменения подтверди коротко и по делу, что и когда сделал. "
        "Помни контекст предыдущих сообщений диалога."
    )
    # история уже в нужном формате; убираем ведущие реплики ассистента (API требует начинать с user)
    msgs = list(history)
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    messages = [{"role": m["role"], "content": m["content"]} for m in msgs]

    for _ in range(8):  # ограничение на число шагов
        resp = client().messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    try:
                        out = _execute(block.name, block.input)
                    except Exception as e:  # noqa: BLE001
                        out = f"Ошибка инструмента: {e}"
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": out,
                    })
            messages.append({"role": "user", "content": results})
            continue
        # обычный текстовый ответ
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    return "Что-то я задумался 🤔 Попробуй переформулировать?"


if __name__ == "__main__":
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else "что у меня сегодня?"
    print(answer([{"role": "user", "content": text}]))
