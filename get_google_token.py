"""
Одноразовый скрипт: получить доступ бота к твоему Google.

Что делает:
  1. Берёт credentials.json (его ты скачаешь из Google Cloud).
  2. Открывает браузер, где ты входишь в свой Google и разрешаешь доступ
     к Календарю и Задачам.
  3. Сохраняет token.json — дальше бот пользуется им сам, повторно входить не нужно.

Запуск:  ./venv/bin/python get_google_token.py
"""
import os

from google_auth_oauthlib.flow import InstalledAppFlow

# Доступ: Календарь (чтение+запись) и Google Tasks (чтение+запись)
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"


def main() -> None:
    if not os.path.exists(CREDENTIALS_FILE):
        raise SystemExit(
            f"❌ Нет файла {CREDENTIALS_FILE}.\n"
            "Скачай его из Google Cloud Console (OAuth client → Desktop app) "
            "и положи в папку calendar-bot."
        )
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"\n✅ Готово! Доступ получен, сохранил в {TOKEN_FILE}.")
    print("Теперь бот сможет читать и менять твой Google Календарь и Задачи.")


if __name__ == "__main__":
    main()
