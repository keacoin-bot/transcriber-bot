"""
Транскрибатор — Telegram-бот
Транскрипция голосовых сообщений и роликов (YouTube/Instagram/TikTok)
через AssemblyAI. Два режима: обычная транскрипция и транскрипция
по ролям (диаризация — разметка по спикерам).

Автор: Claude, для Евгения Касикова.
"""

BOT_VERSION = "2026-09-03 v1"

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
import gspread
from google.oauth2.service_account import Credentials as GoogleCredentials

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
)
from aiogram.exceptions import TelegramBadRequest

# ============================== КОНФИГ ==============================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

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
    "Дата", "Источник", "Ссылка", "Название/тема", "Режим",
    "Язык", "Кол-во спикеров", "Длительность (мин)", "Кол-во слов",
    "Стоимость ($)", "Текст", "Статус",
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
            await bot.send_message(admin_id, text)
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


# ============================== ОТПРАВКА ДЛИННОГО ТЕКСТА ==============================


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
    else:
        for chunk in split_text(text):
            await message.answer(chunk, parse_mode=None)

    row = [
        now_msk().strftime("%Y-%m-%d %H:%M"),
        source_type,
        link or "",
        title or "",
        "по ролям" if roles_mode else "обычная",
        language or "не определён",
        n_speakers if roles_mode else "",
        duration_min,
        word_count,
        cost,
        text,
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


async def save_error_row(source_type: str, link: str, title: str, roles_mode: bool, error_text: str):
    row = [
        now_msk().strftime("%Y-%m-%d %H:%M"),
        source_type,
        link or "",
        title or "",
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
            await status_msg.edit_text(f"⚠️ {e}")
        except TelegramBadRequest:
            pass
        await save_error_row(source_type, url, title, roles_mode, str(e))
    except Exception as e:
        logger.exception("Ошибка обработки ссылки")
        await notify_admin(f"Транскрибатор: ошибка (ссылка {url}), user={user_id}: {e}")
        try:
            await status_msg.edit_text(f"❌ Не получилось скачать или распознать: {str(e)[:300]}")
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

    logger.info(f"Транскрибатор запущен, версия {BOT_VERSION}")
    await notify_admin(f"🤖 Транскрибатор запущен, версия {BOT_VERSION}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
