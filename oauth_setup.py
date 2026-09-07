"""
Одноразовый скрипт для получения refresh-токена Google (Device Flow).

Терминал bothost показывает вывод только ПОСЛЕ завершения команды —
поэтому скрипт разбит на ДВА коротких запуска вместо одного долгого:

Шаг 1:
    python3 oauth_setup.py start
    -> покажет ссылку и код. Открой ссылку в браузере (с телефона или
       компьютера), войди под своим Google-аккаунтом, введи код, подтверди.

Шаг 2 (после того как подтвердил в браузере):
    python3 oauth_setup.py finish
    -> напечатает refresh_token. Если ещё не успел подтвердить в браузере —
       просто подожди несколько секунд и запусти finish ещё раз.

Берёт Client ID и Client Secret из переменных окружения:
    GOOGLE_OAUTH_CLIENT_ID
    GOOGLE_OAUTH_CLIENT_SECRET
(их нужно завести на bothost ДО запуска этого скрипта)

Никаких сторонних библиотек не требует — только стандартная библиотека Python.
"""

import os
import sys
import json
import urllib.request
import urllib.parse
import urllib.error

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/documents https://www.googleapis.com/auth/drive.file"

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
STATE_FILE = os.path.join(DATA_DIR, "oauth_device_flow.json")

CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")


def post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        try:
            return json.loads(error_body)
        except json.JSONDecodeError:
            raise RuntimeError(f"HTTP {e.code}: {error_body}")


def cmd_start():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Не заданы переменные окружения GOOGLE_OAUTH_CLIENT_ID / "
              "GOOGLE_OAUTH_CLIENT_SECRET. Добавь их на bothost и перезапусти бота.")
        return

    print("Запрашиваю код у Google...")
    device_resp = post_form(DEVICE_CODE_URL, {
        "client_id": CLIENT_ID,
        "scope": SCOPES,
    })

    if "error" in device_resp:
        print(f"Ошибка: {device_resp}")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"device_code": device_resp["device_code"]}, f)

    user_code = device_resp["user_code"]
    verification_url = device_resp.get("verification_url") or device_resp.get("verification_uri")

    print("\n" + "=" * 60)
    print(f"1. Открой на телефоне или компьютере: {verification_url}")
    print(f"2. Введи код: {user_code}")
    print("3. Войди под своим Google-аккаунтом и разреши доступ")
    print("   (появится предупреждение \"Google не проверил это приложение\" —")
    print("    это нормально для личного использования, жми продолжить)")
    print("=" * 60)
    print("\nПосле подтверждения в браузере — запусти:")
    print("    python3 oauth_setup.py finish")


def cmd_finish():
    if not os.path.exists(STATE_FILE):
        print("Сначала запусти: python3 oauth_setup.py start")
        return

    with open(STATE_FILE) as f:
        state = json.load(f)
    device_code = state["device_code"]

    token_resp = post_form(TOKEN_URL, {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    })

    error = token_resp.get("error")
    if error == "authorization_pending":
        print("Ещё не подтверждено в браузере. Подожди немного и запусти "
              "finish ещё раз.")
    elif error == "slow_down":
        print("Google просит подождать подольше между попытками. "
              "Подожди 10-15 секунд и запусти finish снова.")
    elif error == "expired_token":
        print("Код истёк (не успел подтвердить вовремя). Начни заново: "
              "python3 oauth_setup.py start")
        os.remove(STATE_FILE)
    elif error == "access_denied":
        print("Доступ отклонён в браузере.")
        os.remove(STATE_FILE)
    elif error:
        print(f"Ошибка: {token_resp}")
    else:
        refresh_token = token_resp.get("refresh_token")
        if not refresh_token:
            print("Google не вернул refresh_token. Возможно, доступ уже "
                  "выдавался раньше — отзови его в Google-аккаунте "
                  "(myaccount.google.com/permissions) и начни заново: "
                  "python3 oauth_setup.py start")
            return
        print("✅ Готово! Вот твой refresh_token:\n")
        print(refresh_token)
        print("\nСкопируй это значение в переменную окружения "
              "GOOGLE_OAUTH_REFRESH_TOKEN на bothost.")
        os.remove(STATE_FILE)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("start", "finish"):
        print("Использование:")
        print("    python3 oauth_setup.py start   — начать (покажет ссылку и код)")
        print("    python3 oauth_setup.py finish  — завершить (после подтверждения в браузере)")
        return
    if sys.argv[1] == "start":
        cmd_start()
    else:
        cmd_finish()


if __name__ == "__main__":
    main()
