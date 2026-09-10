"""
Транскрибатор — Telegram-бот
Транскрипция голосовых сообщений и роликов (YouTube/Instagram/TikTok)
через AssemblyAI. Два режима: обычная транскрипция и транскрипция
по ролям (диаризация — разметка по спикерам).

Автор: Claude, для Евгения Касикова.
"""

BOT_VERSION = "2026-09-10 v5"

import os
import re
import sys
import json
import time
import shutil
import asyncio
import logging
import tempfile
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler

import assemblyai as aai
import anthropic
import gspread
from google.oauth2.service_account import Credentials as GoogleCredentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build as gapi_build

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    ErrorEvent,
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.exceptions import TelegramBadRequest

# ============================== КОНФИГ ==============================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

# Опционально — без них бот работает, просто без классификации и без Google Docs
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "")

ADMIN_IDS = [1064961867]  # Евгений (@kasikovevgenii)
ALLOWED_USER_IDS = [1064961867]  # белый список пользователей бота

DATA_DIR = "/app/data"
DB_PATH = os.path.join(DATA_DIR, "transcriber_db")
PID_FILE = os.path.join(DATA_DIR, "bot.pid")
LOG_FILE = os.path.join(DATA_DIR, "bot.log")

WORKSHEET_TITLE = "Транскрипты"

MAX_DURATION_MINUTES = 240  # защита от случайных многочасовых ссылок
MAX_VOICE_FILE_MB = 20      # лимит Telegram Bot API на скачивание файла

# Ориентировочная цена AssemblyAI (Universal-2), проверять раз в полгода —
# провайдеры периодически меняют тарифы.
PRICE_PER_HOUR_BASE = 0.15
PRICE_PER_HOUR_DIARIZATION_ADDON = 0.02

SHEET_HEADERS = [
    "Дата", "Источник", "Ссылка", "Название/тема", "Раздел", "Тема",
    "Режим", "Язык", "Кол-во спикеров", "Длительность (мин)", "Кол-во слов",
    "Стоимость ($)", "Текст", "Статус",
]

CATEGORIES = ["Недвижимость/Флиппинг", "Психология", "ИИ/Технологии", "Другое"]

CLASSIFY_MODEL = "claude-haiku-4-5-20251001"
CLASSIFY_SYSTEM_PROMPT = (
    "Ты классифицируешь транскрипцию по теме. Тебе дан фрагмент текста "
    "(может быть начало длинной записи). Определи:\n"
    '1. category — ОБЯЗАТЕЛЬНО один из: "Недвижимость/Флиппинг", "Психология", '
    '"ИИ/Технологии", "Другое"\n'
    "2. topic — короткая фраза на русском языке (3-6 слов), описывающая "
    "конкретную тему записи по смыслу содержания\n\n"
    "Ответь СТРОГО в формате JSON без markdown-разметки, без пояснений, только JSON:\n"
    '{"category": "...", "topic": "..."}'
)

GOOGLE_DOC_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

URL_RE = re.compile(r"https?://\S+")
YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)", re.I)
INSTAGRAM_RE = re.compile(r"instagram\.com", re.I)
TIKTOK_RE = re.compile(r"tiktok\.com", re.I)

WELCOME_TEXT = (
    "🎙 <b>Транскрибатор</b>\n\n"
    "Выберите режим ниже, затем пришлите голосовое сообщение "
    "или ссылку на YouTube / Instagram / TikTok.\n\n"
    "• <b>Обычная</b> — просто текст\n"
    "• <b>По ролям</b> — с разметкой по спикерам "
    "(для интервью, лекций, сессий)"
)

# ============================== ЛОГИ ==============================

os.makedirs(DATA_DIR, exist_ok=True)


class TokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        if BOT_TOKEN:
            msg = msg.replace(BOT_TOKEN, "***TOKEN***")
        record.msg = msg
        record.args = ()
        return True


logger = logging.getLogger("transcriber")
logger.setLevel(logging.INFO)

_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=8 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_file_handler.addFilter(TokenFilter())
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_console_handler.addFilter(TokenFilter())
logger.addHandler(_console_handler)

# ============================== ОДИН ЭКЗЕМПЛЯР ==============================


def ensure_single_instance():
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 9)
            logger.info(f"Погашен старый процесс PID={old_pid}")
            time.sleep(1)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


# ============================== ВРЕМЯ (МСК) ==============================


def now_msk() -> datetime:
    msk = timezone(timedelta(hours=3))
    return datetime.now(timezone.utc).astimezone(msk).replace(tzinfo=None)


# ============================== БОТ ==============================

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

if ASSEMBLYAI_API_KEY:
    aai.settings.api_key = ASSEMBLYAI_API_KEY

_user_locks: dict[int, asyncio.Lock] = {}


def get_user_lock(user_id: int) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())


# ============================== notify_admin ==============================


async def notify_admin(text: str):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode=None)
        except Exception as e:
            logger.error(f"notify_admin error (id={admin_id}): {e}")


# ============================== ДОСТУП ==============================


def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS


# ============================== РЕЖИМ ПОЛЬЗОВАТЕЛЯ (shelve) ==============================


def get_user_mode(user_id: int) -> str:
    import shelve
    with shelve.open(DB_PATH) as db:
        return db.get(f"mode_{user_id}", "normal")


def set_user_mode(user_id: int, mode: str):
    import shelve
    with shelve.open(DB_PATH) as db:
        db[f"mode_{user_id}"] = mode


def mode_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    normal_mark = "✅ " if current_mode == "normal" else ""
    roles_mark = "✅ " if current_mode == "roles" else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{normal_mark}🎙 Обычная", callback_data="mode_normal")],
            [InlineKeyboardButton(text=f"{roles_mark}🗣 По ролям (спикеры)", callback_data="mode_roles")],
        ]
    )


# ============================== GOOGLE SHEETS ==============================

_sheet_cache = {"ws": None}


def _get_worksheet():
    if _sheet_cache["ws"] is not None:
        return _sheet_cache["ws"]
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = GoogleCredentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(WORKSHEET_TITLE)
    except gspread.WorksheetNotFound:
        ws = sh.sheet1
        ws.update_title(WORKSHEET_TITLE)
    _ensure_headers(ws)
    _sheet_cache["ws"] = ws
    return ws


def _ensure_headers(ws):
    first_row = ws.row_values(1)
    if first_row != SHEET_HEADERS:
        ws.update("A1", [SHEET_HEADERS])


def _append_row_sync(row: list):
    ws = _get_worksheet()
    ws.append_row(row, value_input_option="USER_ENTERED")


async def append_transcript_row(row: list):
    await asyncio.to_thread(_append_row_sync, row)


# ============================== yt-dlp: СКАЧИВАНИЕ ==============================


def detect_source(url: str) -> str:
    if YOUTUBE_RE.search(url):
        return "YouTube"
    if INSTAGRAM_RE.search(url):
        return "Instagram"
    if TIKTOK_RE.search(url):
        return "TikTok"
    return "Ссылка"


def _ytdlp_extract_info_only(url: str) -> dict:
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _ytdlp_download(url: str, out_dir: str) -> str:
    import yt_dlp
    out_template = os.path.join(out_dir, "%(id)s.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = [f for f in os.listdir(out_dir) if os.path.isfile(os.path.join(out_dir, f))]
    if not files:
        raise RuntimeError("yt-dlp не вернул файл")
    return os.path.join(out_dir, files[0])


def download_audio_via_ytdlp_guarded(url: str, out_dir: str):
    """Возвращает (путь_к_файлу, длительность_сек, название)."""
    info = _ytdlp_extract_info_only(url)
    duration = info.get("duration") or 0
    title = info.get("title") or ""
    if duration and duration > MAX_DURATION_MINUTES * 60:
        raise ValueError(
            f"Ролик длиннее {MAX_DURATION_MINUTES} минут "
            f"({round(duration / 60)} мин) — не обрабатываю, слишком долго и дорого."
        )
    path = _ytdlp_download(url, out_dir)
    return path, duration, title


# ============================== ASSEMBLYAI: ТРАНСКРИПЦИЯ ==============================


def _transcribe_sync(path: str, roles_mode: bool):
    config = aai.TranscriptionConfig(
        language_detection=True,
        speaker_labels=roles_mode,
    )
    transcript = aai.Transcriber(config=config).transcribe(path)
    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI: {transcript.error}")
    return transcript


async def transcribe_audio(path: str, roles_mode: bool):
    """Возвращает (текст, язык, кол-во_спикеров)."""
    transcript = await asyncio.wait_for(
        asyncio.to_thread(_transcribe_sync, path, roles_mode),
        timeout=1800,  # 30 минут — защита от зависшего запроса
    )

    language = None
    try:
        language = transcript.json_response.get("language_code")
    except Exception:
        language = None

    n_speakers = 0
    if roles_mode and transcript.utterances:
        speakers = sorted(set(u.speaker for u in transcript.utterances))
        n_speakers = len(speakers)
        text = "\n\n".join(f"Спикер {u.speaker}: {u.text}" for u in transcript.utterances)
    else:
        text = transcript.text or ""

    return text, language, n_speakers


# ============================== CLAUDE: КЛАССИФИКАЦИЯ ПО РАЗДЕЛУ/ТЕМЕ ==============================


def _classify_sync(text_excerpt: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
    response = client.messages.create(
        model=CLASSIFY_MODEL,
        max_tokens=150,
        system=CLASSIFY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text_excerpt}],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    data = json.loads(raw)
    category = data.get("category", "Другое")
    if category not in CATEGORIES:
        category = "Другое"
    topic = str(data.get("topic", ""))[:200]
    return {"category": category, "topic": topic}


async def classify_transcript(text: str) -> dict:
    if not text or not ANTHROPIC_API_KEY:
        return {"category": "", "topic": ""}
    excerpt = text[:4000]
    try:
        return await asyncio.wait_for(asyncio.to_thread(_classify_sync, excerpt), timeout=60)
    except Exception as e:
        logger.error(f"Ошибка классификации: {e}")
        return {"category": "Другое", "topic": ""}


# ============================== GOOGLE DOCS: АРХИВ ДЛИННЫХ ЗАПИСЕЙ ==============================

_docs_cache = {"service": None, "doc_id": None}


def _get_docs_service():
    if _docs_cache["service"] is not None:
        return _docs_cache["service"]
    creds = OAuthCredentials(
        token=None,
        refresh_token=GOOGLE_OAUTH_REFRESH_TOKEN,
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=GOOGLE_DOC_SCOPES,
    )
    service = gapi_build("docs", "v1", credentials=creds, cache_discovery=False)
    _docs_cache["service"] = service
    return service


def _get_master_doc_id_sync(service) -> str:
    if _docs_cache["doc_id"]:
        return _docs_cache["doc_id"]
    import shelve
    with shelve.open(DB_PATH) as db:
        doc_id = db.get("master_doc_id")
    if not doc_id:
        doc = service.documents().create(body={"title": "Транскрибатор — Архив"}).execute()
        doc_id = doc["documentId"]
        with shelve.open(DB_PATH) as db:
            db["master_doc_id"] = doc_id
    _docs_cache["doc_id"] = doc_id
    return doc_id


def _find_tab_id_by_title_sync(service, doc_id: str, title: str):
    """Ищет id вкладки по её точному названию (берёт последнее совпадение —
    считаем, что новая вкладка добавляется в конец списка). None, если не нашёл."""
    try:
        doc = service.documents().get(documentId=doc_id, includeTabsContent=True).execute()
        matched_id = None
        for tab in doc.get("tabs", []):
            props = tab.get("tabProperties", {})
            if props.get("title", "").strip() == title.strip():
                matched_id = props.get("tabId")
        return matched_id
    except Exception as e:
        logger.warning(f"Не удалось найти tabId по названию: {e}")
        return None


def _try_create_tab_sync(service, doc_id: str, tab_title: str):
    """Пробует создать отдельную вкладку под запись. None, если API это не поддержало.

    Формат ответа на addDocumentTab — недокументированная часть API, ему не доверяем.
    Надёжнее: создать вкладку, затем отдельно перечитать документ и найти её id
    по названию, которое сами только что задали (тот же приём, что и для заголовков)."""
    try:
        body = {"requests": [{"addDocumentTab": {"tabProperties": {"title": tab_title}}}]}
        service.documents().batchUpdate(documentId=doc_id, body=body).execute()
    except Exception as e:
        logger.info(f"Создание вкладки не удалось (использую заголовок в общем документе): {e}")
        return None

    tab_id = _find_tab_id_by_title_sync(service, doc_id, tab_title)
    if not tab_id:
        logger.warning(
            "Вкладка создана, но не удалось найти её id по названию — "
            "использую заголовок в общем документе"
        )
    return tab_id


def _find_heading_id_sync(service, doc_id: str, heading_text: str):
    """Ищет headingId только что вставленного заголовка. None, если не удалось найти."""
    try:
        doc = service.documents().get(documentId=doc_id).execute()
        for element in doc.get("body", {}).get("content", []):
            paragraph = element.get("paragraph")
            if not paragraph:
                continue
            heading_id = paragraph.get("paragraphStyle", {}).get("headingId")
            if not heading_id:
                continue
            text = "".join(
                run.get("textRun", {}).get("content", "")
                for run in paragraph.get("elements", [])
            ).strip()
            if text == heading_text.strip():
                return heading_id
        return None
    except Exception as e:
        logger.warning(f"Не удалось найти headingId: {e}")
        return None


def _add_entry_to_master_doc_sync(title: str, source_type: str, roles_mode: bool, text: str) -> str:
    service = _get_docs_service()
    doc_id = _get_master_doc_id_sync(service)
    heading_text = (
        f"{now_msk().strftime('%d.%m.%Y %H:%M')} — {title or source_type} "
        f"({'по ролям' if roles_mode else 'обычная'})"
    )
    # Дата+время в названии вкладки — чтобы не совпадало с более старой вкладкой
    # с таким же названием (иначе поиск по названию может найти не ту вкладку)
    tab_title = f"{(title or source_type)[:60]} · {now_msk().strftime('%d.%m %H:%M')}"[:80]

    tab_id = _try_create_tab_sync(service, doc_id, tab_title)
    if tab_id:
        requests = [{
            "insertText": {
                "location": {"tabId": tab_id, "index": 1},
                "text": f"{heading_text}\n{text}",
            }
        }]
        service.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
        return f"https://docs.google.com/document/d/{doc_id}/edit?tab={tab_id}"

    # Запасной путь — заголовок в общем документе
    doc = service.documents().get(documentId=doc_id).execute()
    content = doc.get("body", {}).get("content", [])
    end_index = content[-1].get("endIndex", 1) if content else 1
    insert_index = max(end_index - 1, 1)

    requests = [
        {"insertText": {"location": {"index": insert_index}, "text": f"{heading_text}\n{text}\n\n"}},
        {
            "updateParagraphStyle": {
                "range": {"startIndex": insert_index, "endIndex": insert_index + len(heading_text)},
                "paragraphStyle": {"namedStyleType": "HEADING_2"},
                "fields": "namedStyleType",
            }
        },
    ]
    service.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()

    heading_id = _find_heading_id_sync(service, doc_id, heading_text)
    if heading_id:
        return f"https://docs.google.com/document/d/{doc_id}/edit#heading={heading_id}"
    return f"https://docs.google.com/document/d/{doc_id}/edit"


async def add_entry_to_master_doc(title: str, source_type: str, roles_mode: bool, text: str):
    if not (GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REFRESH_TOKEN):
        return None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_add_entry_to_master_doc_sync, title, source_type, roles_mode, text),
            timeout=120,
        )
    except Exception as e:
        logger.error(f"Ошибка создания записи в Google Docs: {e}")
        await notify_admin(f"Транскрибатор: не удалось создать запись в Google Docs: {e}")
        return None


# ============================== ОТПРАВКА ДЛИННОГО ТЕКСТА ==============================


def make_txt_filename(title: str, source_type: str) -> str:
    base = (title or source_type or "transcript").strip()
    base = re.sub(r'[\\/:*?"<>|]+', "", base)
    base = base[:80].strip() or "transcript"
    return f"{base}.txt"


def split_text(text: str, limit: int = 3500) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    current = ""
    for paragraph in text.split("\n"):
        candidate = f"{current}\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        while len(paragraph) > limit:
            parts.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = paragraph
    if current:
        parts.append(current)
    return parts


# ============================== ОБРАБОТКА РЕЗУЛЬТАТА ==============================


async def finalize_result(
    message: Message,
    status_msg: Message,
    text: str,
    language: str | None,
    n_speakers: int,
    duration_seconds: float,
    roles_mode: bool,
    source_type: str,
    link: str,
    title: str,
):
    word_count = len(text.split()) if text else 0
    duration_min = round(duration_seconds / 60, 1) if duration_seconds else 0
    rate_per_hour = PRICE_PER_HOUR_BASE + (
        PRICE_PER_HOUR_DIARIZATION_ADDON if roles_mode else 0
    )
    cost = round((duration_seconds / 3600) * rate_per_hour, 4) if duration_seconds else 0.0

    summary = (
        f"✅ Готово: {duration_min} мин, {word_count} слов, "
        f"язык: {language or 'не определён'}"
    )
    if roles_mode:
        summary += f", спикеров: {n_speakers}"

    try:
        await status_msg.edit_text(summary)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"edit_text error: {e}")

    if not text:
        await message.answer("Текст пустой — возможно, в записи нет речи.")
        classification = {"category": "", "topic": ""}
        sheet_text = ""
    else:
        classification = await classify_transcript(text)
        parts = split_text(text)
        if len(parts) == 1:
            await message.answer(parts[0], parse_mode=None)
            sheet_text = text
        else:
            # Не помещается в одно сообщение Telegram — шлём файлом, а не простынёй сообщений
            filename = make_txt_filename(title, source_type)
            tmp_txt_dir = tempfile.mkdtemp(prefix="trb_txt_")
            try:
                file_path = os.path.join(tmp_txt_dir, filename)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(text)
                await message.answer_document(FSInputFile(file_path, filename=filename))
            except Exception as e:
                logger.error(f"Ошибка отправки txt-файла: {e}")
                # запасной путь — всё же отправить частями, чтобы текст не потерялся
                for chunk in parts:
                    await message.answer(chunk, parse_mode=None)
            finally:
                shutil.rmtree(tmp_txt_dir, ignore_errors=True)

            doc_link = await add_entry_to_master_doc(title, source_type, roles_mode, text)
            if doc_link:
                await message.answer(f"📄 Полный текст также здесь: {doc_link}", parse_mode=None)
                sheet_text = doc_link
            else:
                # Google Docs не настроен или не сработал — подстраховка от лимита ячейки Sheets (50000 симв.)
                sheet_text = text[:49500] + "\n\n[...обрезано, лимит ячейки Google Sheets — полный текст в файле выше]"

    row = [
        now_msk().strftime("%Y-%m-%d %H:%M"),
        source_type,
        link or "",
        title or "",
        classification.get("category", ""),
        classification.get("topic", ""),
        "по ролям" if roles_mode else "обычная",
        language or "не определён",
        n_speakers if roles_mode else "",
        duration_min,
        word_count,
        cost,
        sheet_text,
        "готово",
    ]
    try:
        await append_transcript_row(row)
    except Exception as e:
        logger.error(f"Ошибка записи в Sheets: {e}")
        await notify_admin(f"Транскрибатор: не удалось записать в Google Sheets: {e}")
        await message.answer(
            "⚠️ Текст готов, но не получилось сохранить в таблицу — админ уведомлён."
        )

    current_mode = get_user_mode(message.from_user.id)
    await message.answer(
        "Выберите режим для следующей записи:",
        reply_markup=mode_keyboard(current_mode),
    )


async def save_error_row(source_type: str, link: str, title: str, roles_mode: bool, error_text: str):
    row = [
        now_msk().strftime("%Y-%m-%d %H:%M"),
        source_type,
        link or "",
        title or "",
        "", "",
        "по ролям" if roles_mode else "обычная",
        "", "", "", "", "",
        f"ОШИБКА: {error_text[:500]}",
        "ошибка",
    ]
    try:
        await append_transcript_row(row)
    except Exception as e:
        logger.error(f"Не удалось записать строку ошибки в Sheets: {e}")


# ============================== ОБРАБОТКА ГОЛОСОВОГО ==============================


async def process_voice(message: Message):
    user_id = message.from_user.id
    mode = get_user_mode(user_id)
    roles_mode = mode == "roles"
    voice = message.voice

    if voice.file_size and voice.file_size > MAX_VOICE_FILE_MB * 1024 * 1024:
        await message.answer(
            f"Файл больше {MAX_VOICE_FILE_MB} МБ — Telegram не даёт боту его скачать."
        )
        return

    status_msg = await message.answer("⏳ Скачиваю и распознаю...")
    tmp_dir = tempfile.mkdtemp(prefix="trb_")
    title = message.caption or ""
    try:
        local_path = os.path.join(tmp_dir, "voice.oga")
        await bot.download(voice, destination=local_path)
        duration = voice.duration or 0

        text, language, n_speakers = await transcribe_audio(local_path, roles_mode)
        await finalize_result(
            message, status_msg, text, language, n_speakers, duration,
            roles_mode, source_type="голосовое", link="", title=title,
        )
    except Exception as e:
        logger.exception("Ошибка обработки голосового")
        await notify_admin(f"Транскрибатор: ошибка (голосовое), user={user_id}: {e}")
        try:
            await status_msg.edit_text("❌ Не получилось распознать. Админ уже уведомлён.")
        except TelegramBadRequest:
            pass
        await save_error_row("голосовое", "", title, roles_mode, str(e))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@dp.message(F.voice)
async def handle_voice(message: Message):
    user_id = message.from_user.id
    if not is_allowed(user_id):
        await message.answer("Доступ закрыт.")
        return
    lock = get_user_lock(user_id)
    if lock.locked():
        await message.answer("⏳ Ещё обрабатываю предыдущий файл, подождите.")
        return
    async with lock:
        await process_voice(message)


# ============================== ОБРАБОТКА ССЫЛКИ ==============================


async def process_link(message: Message, url: str):
    user_id = message.from_user.id
    mode = get_user_mode(user_id)
    roles_mode = mode == "roles"
    source_type = detect_source(url)

    status_msg = await message.answer(f"⏳ Скачиваю аудио с {source_type}...")
    tmp_dir = tempfile.mkdtemp(prefix="trb_")
    title = ""
    try:
        local_path, duration, title = await asyncio.to_thread(
            download_audio_via_ytdlp_guarded, url, tmp_dir
        )
        try:
            await status_msg.edit_text("⏳ Распознаю текст...")
        except TelegramBadRequest:
            pass

        text, language, n_speakers = await transcribe_audio(local_path, roles_mode)
        await finalize_result(
            message, status_msg, text, language, n_speakers, duration,
            roles_mode, source_type, link=url, title=title,
        )
    except ValueError as e:
        # предсказуемая ошибка (например, слишком длинный ролик) — без нотификации админу
        try:
            await status_msg.edit_text(f"⚠️ {e}", parse_mode=None)
        except TelegramBadRequest:
            pass
        await save_error_row(source_type, url, title, roles_mode, str(e))
    except Exception as e:
        logger.exception("Ошибка обработки ссылки")
        await notify_admin(f"Транскрибатор: ошибка (ссылка {url}), user={user_id}: {e}")
        try:
            await status_msg.edit_text(f"❌ Не получилось скачать или распознать: {str(e)[:300]}", parse_mode=None)
        except TelegramBadRequest:
            pass
        await save_error_row(source_type, url, title, roles_mode, str(e))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================== ХЭНДЛЕРЫ ==============================


@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    if not is_allowed(user_id):
        await message.answer("Доступ закрыт.")
        logger.warning(f"Попытка доступа: user_id={user_id} username={message.from_user.username}")
        return
    mode = get_user_mode(user_id)
    await message.answer(WELCOME_TEXT, reply_markup=mode_keyboard(mode))


@dp.message(Command("version"))
async def cmd_version(message: Message):
    if not is_allowed(message.from_user.id):
        return
    await message.answer(f"Версия: {BOT_VERSION}")


@dp.callback_query(F.data.in_({"mode_normal", "mode_roles"}))
async def cb_mode(cb: CallbackQuery):
    if not is_allowed(cb.from_user.id):
        await cb.answer("Доступ закрыт", show_alert=True)
        return
    mode = "normal" if cb.data == "mode_normal" else "roles"
    set_user_mode(cb.from_user.id, mode)
    try:
        await cb.message.edit_text(WELCOME_TEXT, reply_markup=mode_keyboard(mode))
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"cb_mode edit_text error: {e}")
    await cb.answer(f"Режим: {'обычная' if mode == 'normal' else 'по ролям'}")


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
    user_id = message.from_user.id
    if not is_allowed(user_id):
        await message.answer("Доступ закрыт.")
        return
    match = URL_RE.search(message.text)
    if not match:
        await message.answer(
            "Не понял. Пришлите голосовое сообщение или ссылку "
            "на YouTube / Instagram / TikTok."
        )
        return
    url = match.group(0)
    lock = get_user_lock(user_id)
    if lock.locked():
        await message.answer("⏳ Ещё обрабатываю предыдущий файл, подождите.")
        return
    async with lock:
        await process_link(message, url)


@dp.message()
async def catch_all(message: Message):
    if not is_allowed(message.from_user.id):
        return
    await message.answer(
        "Пришлите голосовое сообщение или ссылку на YouTube / Instagram / TikTok."
    )


@dp.errors()
async def global_error_handler(event: ErrorEvent):
    logger.error(f"Необработанная ошибка: {event.exception}")
    return True


# ============================== СТАРТ ==============================


async def main():
    ensure_single_instance()

    if shutil.which("ffmpeg") is None:
        logger.warning("ffmpeg не найден в PATH — скачивание некоторых ссылок может не работать")

    missing = [
        name
        for name, val in [
            ("BOT_TOKEN", BOT_TOKEN),
            ("ASSEMBLYAI_API_KEY", ASSEMBLYAI_API_KEY),
            ("GOOGLE_CREDENTIALS", GOOGLE_CREDENTIALS),
            ("GOOGLE_SHEET_ID", GOOGLE_SHEET_ID),
        ]
        if not val
    ]
    if missing:
        logger.error(f"Не заданы переменные окружения: {', '.join(missing)}")
        sys.exit(1)

    optional_missing = []
    if not ANTHROPIC_API_KEY:
        optional_missing.append("ANTHROPIC_API_KEY (классификация по разделам отключена)")
    if not (GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REFRESH_TOKEN):
        optional_missing.append("GOOGLE_OAUTH_* (создание Google Docs отключено)")
    if optional_missing:
        logger.warning("Опциональные функции отключены: " + "; ".join(optional_missing))

    logger.info(f"Транскрибатор запущен, версия {BOT_VERSION}")
    await notify_admin(f"🤖 Транскрибатор запущен, версия {BOT_VERSION}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
