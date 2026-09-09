"""
Одноразовый скрипт для получения refresh-токена Google (Authorization Code Flow).

Device Flow (код+ссылка) не поддерживает доступ к Google Docs/Drive —
Google ограничивает его простыми сервисами. Поэтому используем обычный
способ авторизации, с ручным копированием кода из адресной строки браузера.

Терминал bothost показывает вывод только ПОСЛЕ завершения команды —
поэтому скрипт разбит на ДВА коротких запуска:

Шаг 1:
    python3 oauth_setup.py start
    -> покажет ссылку. Открой её в браузере (с телефона или компьютера),
       войди под своим Google-аккаунтом, разреши доступ.
       Браузер попробует перейти на несуществующую страницу
       (http://localhost:8080/...) и покажет ошибку типа
       "не удаётся получить доступ к сайту" — это нормально и ожидаемо.
       Нужно скопировать ПОЛНЫЙ адрес из адресной строки браузера в
       этот момент (там будет виден код).

Шаг 2:
    python3 oauth_setup.py finish "ВСТАВЬ_СЮДА_СКОПИРОВАННЫЙ_АДРЕС"
    -> напечатает refresh_token.

Берёт Client ID и Client Secret из переменных окружения:
    GOOGLE_OAUTH_CLIENT_ID
    GOOGLE_OAUTH_CLIENT_SECRET
(должны быть от OAuth-клиента типа "Web application" с зарегистрированным
redirect URI: http://localhost:8080/callback)

Никаких сторонних библиотек не требует — только стандартная библиотека Python.
"""

import os
import sys
import json
import urllib.request
import urllib.parse
import urllib.error

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/documents https://www.googleapis.com/auth/drive.file"
REDIRECT_URI = "http://localhost:8080/callback"

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

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    print("\n" + "=" * 60)
    print("Открой эту ссылку в браузере (с телефона или компьютера):\n")
    print(url)
    print("\n" + "=" * 60)
    print("\n1. Войди под своим Google-аккаунтом, разреши доступ")
    print("   (появится предупреждение \"Google не проверил это приложение\" —")
    print("    это нормально, жми продолжить)")
    print("2. Браузер попытается перейти на несуществующую страницу и покажет")
    print("   ошибку — это ожидаемо, ничего не сломалось")
    print("3. Скопируй ПОЛНЫЙ адрес из адресной строки браузера в этот момент")
    print("4. Запусти:")
    print('   python3 oauth_setup.py finish "ВСТАВЬ_СКОПИРОВАННЫЙ_АДРЕС"')


def extract_code(pasted: str) -> str:
    """Достаёт code= из вставленного адреса или принимает голый код как есть."""
    if "code=" in pasted:
        parsed = urllib.parse.urlparse(pasted)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs:
            return qs["code"][0]
    return pasted.strip()


def cmd_finish(pasted: str):
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Не заданы переменные окружения GOOGLE_OAUTH_CLIENT_ID / "
              "GOOGLE_OAUTH_CLIENT_SECRET.")
        return

    code = extract_code(pasted)
    if not code:
        print("Не нашёл code= в том, что вставили. Скопируй полный адрес "
              "из браузера ещё раз.")
        return

    token_resp = post_form(TOKEN_URL, {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    })

    error = token_resp.get("error")
    if error:
        print(f"Ошибка: {token_resp}")
        print("\nЧастая причина — код уже был использован или устарел "
              "(живёт недолго). Запусти заново: python3 oauth_setup.py start")
        return

    refresh_token = token_resp.get("refresh_token")
    if not refresh_token:
        print("Google не вернул refresh_token. Возможно, доступ уже "
              "выдавался раньше без запроса нового refresh_token — "
              "отзови его в Google-аккаунте (myaccount.google.com/permissions) "
              "и начни заново: python3 oauth_setup.py start")
        return

    print("✅ Готово! Вот твой refresh_token:\n")
    print(refresh_token)
    print("\nСкопируй это значение в переменную окружения "
          "GOOGLE_OAUTH_REFRESH_TOKEN на bothost.")


def cmd_check():
    """Диагностика: пробует обновить токен напрямую у Google, печатает сырой ответ."""
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Не заданы GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET.")
        return
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "")
    if not refresh_token:
        print("Не задан GOOGLE_OAUTH_REFRESH_TOKEN.")
        return

    print(f"CLIENT_ID: {CLIENT_ID}")
    print(f"CLIENT_ID длина: {len(CLIENT_ID)}")
    print(f"CLIENT_SECRET длина: {len(CLIENT_SECRET)}")
    print(f"REFRESH_TOKEN длина: {len(refresh_token)}")
    print(f"REFRESH_TOKEN начало/конец: {refresh_token[:15]}...{refresh_token[-15:]}")
    print()

    resp = post_form(TOKEN_URL, {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    print("Сырой ответ Google:")
    print(json.dumps(resp, indent=2, ensure_ascii=False))


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("start", "finish", "check"):
        print("Использование:")
        print("    python3 oauth_setup.py start")
        print('    python3 oauth_setup.py finish "адрес_из_браузера"')
        print("    python3 oauth_setup.py check   — диагностика текущего токена")
        return
    if sys.argv[1] == "start":
        cmd_start()
    elif sys.argv[1] == "check":
        cmd_check()
    else:
        if len(sys.argv) < 3:
            print('Нужно вставить адрес: python3 oauth_setup.py finish "адрес_из_браузера"')
            return
        cmd_finish(sys.argv[2])


if __name__ == "__main__":
    main()
