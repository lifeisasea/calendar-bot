"""
Работа с Google Календарём и Google Tasks.

Использует token.json (получен через get_google_token.py).
Токен сам обновляется, повторно входить не нужно.
"""
from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]
_BASE = os.path.dirname(__file__)
TOKEN_FILE = os.path.join(_BASE, "token.json")
CREDENTIALS_FILE = os.path.join(_BASE, "credentials.json")


def _ensure_files() -> None:
    """На хостинге секретов-файлов нет — берём их из переменных окружения."""
    cred_env = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    tok_env = os.environ.get("GOOGLE_TOKEN_JSON")
    if cred_env and not os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE, "w") as f:
            f.write(cred_env)
    if tok_env and not os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "w") as f:
            f.write(tok_env)

# Часовой пояс. Значение по умолчанию, но бот при старте подтянет реальный
# пояс из твоего Google Календаря (см. refresh_tz).
TZ = ZoneInfo("Asia/Dubai")


def refresh_tz() -> None:
    """Взять часовой пояс из основного календаря пользователя."""
    global TZ
    try:
        cal = calendar().calendars().get(calendarId="primary").execute()
        tzname = cal.get("timeZone")
        if tzname:
            TZ = ZoneInfo(tzname)
    except Exception:  # noqa: BLE001 — офлайн/ошибка: остаёмся на значении по умолчанию
        pass


def _creds() -> Credentials:
    _ensure_files()
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def calendar():
    return build("calendar", "v3", credentials=_creds(), cache_discovery=False)


def tasks():
    return build("tasks", "v1", credentials=_creds(), cache_discovery=False)


# ---------- Календарь: чтение ----------
def list_events(time_min: dt.datetime, time_max: dt.datetime) -> list[dict]:
    """События основного календаря в интервале [time_min, time_max)."""
    svc = calendar()
    res = svc.events().list(
        calendarId="primary",
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return res.get("items", [])


def day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=TZ)
    end = start + dt.timedelta(days=1)
    return start, end


def format_event(ev: dict) -> str:
    start = ev["start"].get("dateTime")
    if start:  # событие со временем
        t = dt.datetime.fromisoformat(start).astimezone(TZ).strftime("%H:%M")
        return f"🕒 {t} — {ev.get('summary', '(без названия)')}"
    # событие на весь день
    return f"📌 весь день — {ev.get('summary', '(без названия)')}"


def events_for_day(day: dt.date) -> str:
    """Готовый текст со списком событий на конкретный день."""
    start, end = day_bounds(day)
    evs = list_events(start, end)
    if not evs:
        return "свободно ✨"
    return "\n".join(format_event(e) for e in evs)


# ---------- Календарь: запись ----------
# Цвета событий Google по категориям
CATEGORY_COLOR = {
    "личное": "3",    # Grape (фиолетовый)
    "работа": "10",   # Basil (зелёный)
    "срочно": "11",   # Tomato (красный)
}


def create_event(
    summary: str,
    start: dt.datetime,
    end: dt.datetime | None = None,
    all_day: bool = False,
    category: str | None = None,
    description: str | None = None,
    no_reminder: bool = False,
    reminder_minutes: int | None = None,
) -> dict:
    """Создать событие в основном календаре."""
    svc = calendar()
    body: dict = {"summary": summary}
    if all_day:
        body["start"] = {"date": start.date().isoformat()}
        body["end"] = {"date": (start.date() + dt.timedelta(days=1)).isoformat()}
    else:
        if end is None:
            end = start + dt.timedelta(hours=1)
        body["start"] = {"dateTime": start.isoformat(), "timeZone": str(TZ)}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": str(TZ)}
    if description:
        body["description"] = description
    if category and category.lower() in CATEGORY_COLOR:
        body["colorId"] = CATEGORY_COLOR[category.lower()]
    if no_reminder:
        body["reminders"] = {"useDefault": False, "overrides": []}
    elif reminder_minutes is not None:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": reminder_minutes}],
        }
    return svc.events().insert(calendarId="primary", body=body).execute()


def update_event(
    event_id: str,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    summary: str | None = None,
    category: str | None = None,
    all_day: bool = False,
    no_reminder: bool | None = None,
    reminder_minutes: int | None = None,
) -> dict:
    """Изменить существующее событие (патч — меняем только переданные поля)."""
    svc = calendar()
    body: dict = {}
    if summary is not None:
        body["summary"] = summary
    if start is not None and not all_day:
        # если не задан новый конец — сохраняем прежнюю длительность
        if end is None:
            ev = svc.events().get(calendarId="primary", eventId=event_id).execute()
            os_ = ev["start"].get("dateTime")
            oe = ev["end"].get("dateTime")
            if os_ and oe:
                dur = dt.datetime.fromisoformat(oe) - dt.datetime.fromisoformat(os_)
                end = start + dur
        body["start"] = {"dateTime": start.isoformat(), "timeZone": str(TZ)}
        if end is not None:
            body["end"] = {"dateTime": end.isoformat(), "timeZone": str(TZ)}
    elif start is not None and all_day:
        body["start"] = {"date": start.date().isoformat()}
        body["end"] = {"date": ((end or start).date() + dt.timedelta(days=1)).isoformat()}
    elif end is not None:
        body["end"] = {"dateTime": end.isoformat(), "timeZone": str(TZ)}
    if category and category.lower() in CATEGORY_COLOR:
        body["colorId"] = CATEGORY_COLOR[category.lower()]
    if no_reminder:
        body["reminders"] = {"useDefault": False, "overrides": []}
    elif reminder_minutes is not None:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": reminder_minutes}],
        }
    return svc.events().patch(calendarId="primary", eventId=event_id, body=body).execute()


def delete_event(event_id: str) -> None:
    calendar().events().delete(calendarId="primary", eventId=event_id).execute()


def search_events(query: str, time_min: dt.datetime, time_max: dt.datetime) -> list[dict]:
    """Поиск событий по тексту названия в интервале."""
    svc = calendar()
    res = svc.events().list(
        calendarId="primary",
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        q=query,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return res.get("items", [])


def event_times(ev: dict) -> tuple[str, str]:
    """Вернуть (начало, конец) события в ISO-строках для удобства модели."""
    s = ev["start"].get("dateTime") or ev["start"].get("date")
    e = ev["end"].get("dateTime") or ev["end"].get("date")
    return s, e


# ---------- Google Tasks: задачи ----------
TASKLIST = "@default"  # основной список задач


def _due_date(t: dict) -> dt.date | None:
    due = t.get("due")
    return dt.date.fromisoformat(due[:10]) if due else None


def is_urgent(t: dict) -> bool:
    return t.get("title", "").startswith("🔥")


def list_raw_tasks(show_completed: bool = False) -> list[dict]:
    res = tasks().tasks().list(
        tasklist=TASKLIST, showCompleted=show_completed, showHidden=False, maxResults=100
    ).execute()
    return res.get("items", [])


def rollover_tasks() -> int:
    """Невыполненные задачи с прошедшим сроком переносим на сегодня."""
    today = dt.datetime.now(TZ).date()
    svc = tasks()
    changed = 0
    for t in list_raw_tasks():
        if t.get("status") == "completed":
            continue
        d = _due_date(t)
        if d and d < today:
            svc.tasks().patch(
                tasklist=TASKLIST, task=t["id"],
                body={"due": f"{today.isoformat()}T00:00:00.000Z"},
            ).execute()
            changed += 1
    return changed


def tasks_view(scope: str = "today") -> list[dict]:
    """scope: today | overdue | backlog | all — список невыполненных задач."""
    today = dt.datetime.now(TZ).date()
    items = [t for t in list_raw_tasks() if t.get("status") != "completed"]
    if scope == "backlog":
        items = [t for t in items if not _due_date(t)]
    elif scope == "today":
        items = [t for t in items if _due_date(t) and _due_date(t) <= today]
    elif scope == "overdue":
        items = [t for t in items if _due_date(t) and _due_date(t) < today]
    items.sort(key=lambda t: (not is_urgent(t), _due_date(t) or dt.date.max))
    return items


def add_task(title: str, due: dt.date | None = None, urgent: bool = False) -> dict:
    if urgent and not title.startswith("🔥"):
        title = "🔥 " + title
    body: dict = {"title": title}
    if due:
        body["due"] = f"{due.isoformat()}T00:00:00.000Z"
    return tasks().tasks().insert(tasklist=TASKLIST, body=body).execute()


def complete_task(task_id: str) -> dict:
    return tasks().tasks().patch(
        tasklist=TASKLIST, task=task_id, body={"status": "completed"}
    ).execute()


def delete_task(task_id: str) -> None:
    tasks().tasks().delete(tasklist=TASKLIST, task=task_id).execute()


if __name__ == "__main__":
    # Быстрая проверка доступа
    today = dt.datetime.now(TZ).date()
    print("Сегодня:", today)
    print(events_for_day(today))
    print("\nЗавтра:")
    print(events_for_day(today + dt.timedelta(days=1)))
