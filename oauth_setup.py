"""
Одноразовый скрипт для получения refresh-токена Google (Device Flow).

Запускать ОДИН РАЗ через терминал на bothost (иконка >_ на карточке бота).
Ничего никуда не деплоить, просто запустить и следовать подсказкам.

Как использовать:
    python3 oauth_setup.py

Скрипт спросит Client ID и Client Secret (из Google Cloud Console,
раздел Credentials, тип "TVs and Limited Input devices" или "Desktop app").
Дальше покажет ссылку и код — их нужно один раз открыть/ввести в браузере
(с телефона или компьютера, не обязательно на том же устройстве).
После подтверждения скрипт напечатает refresh_token — его нужно
скопировать в переменную окружения GOOGLE_OAUTH_REFRESH_TOKEN на bothost.

Никаких сторонних библиотек не требует — только стандартная библиотека Python.
"""

import json
import time
import urllib.request
import urllib.parse
import urllib.error

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/documents https://www.googleapis.com/auth/drive.file"


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


def main():
    print("=== Получение refresh-токена Google (для Google Docs) ===\n")
    client_id = input("Вставь Client ID: ").strip()
    client_secret = input("Вставь Client Secret: ").strip()

    if not client_id or not client_secret:
        print("Client ID и Client Secret обязательны. Прерываю.")
        return

    print("\nЗапрашиваю код у Google...")
    device_resp = post_form(DEVICE_CODE_URL, {
        "client_id": client_id,
        "scope": SCOPES,
    })

    if "error" in device_resp:
        print(f"Ошибка: {device_resp}")
        return

    device_code = device_resp["device_code"]
    user_code = device_resp["user_code"]
    verification_url = device_resp.get("verification_url") or device_resp.get("verification_uri")
    interval = device_resp.get("interval", 5)
    expires_in = device_resp.get("expires_in", 1800)

    print("\n" + "=" * 60)
    print(f"1. Открой на телефоне или компьютере: {verification_url}")
    print(f"2. Введи код: {user_code}")
    print("3. Войди под своим Google-аккаунтом и разреши доступ")
    print("   (появится предупреждение \"Google не проверил это приложение\" —")
    print("    это нормально для личного использования, жми продолжить)")
    print("=" * 60)
    print("\nЖду подтверждения...")

    waited = 0
    while waited < expires_in:
        time.sleep(interval)
        waited += interval

        token_resp = post_form(TOKEN_URL, {
            "client_id": client_id,
            "client_secret": client_secret,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })

        error = token_resp.get("error")
        if error == "authorization_pending":
            continue
        elif error == "slow_down":
            interval += 5
            continue
        elif error == "expired_token":
            print("\nКод истёк, запусти скрипт заново.")
            return
        elif error == "access_denied":
            print("\nДоступ отклонён.")
            return
        elif error:
            print(f"\nОшибка: {token_resp}")
            return
        else:
            refresh_token = token_resp.get("refresh_token")
            if not refresh_token:
                print("\nGoogle не вернул refresh_token. Возможно, доступ уже "
                      "выдавался раньше — отзови его в Google-аккаунте "
                      "(myaccount.google.com/permissions) и запусти скрипт заново.")
                return
            print("\n✅ Готово! Вот твой refresh_token:\n")
            print(refresh_token)
            print("\nСкопируй это значение в переменную окружения "
                  "GOOGLE_OAUTH_REFRESH_TOKEN на bothost.")
            return

    print("\nВремя ожидания истекло, запусти скрипт заново.")


if __name__ == "__main__":
    main()
