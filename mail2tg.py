#!/usr/bin/env python3
"""
Пересылка новых писем из Яндекс Почты в Telegram-канал.

Скрипт подключается к ящику по IMAP (только чтение — письма не помечаются
прочитанными), находит письма, пришедшие после прошлого запуска, и публикует
их в канал через Telegram Bot API. Вложения отправляются отдельными файлами.

Номер последнего обработанного письма хранится в state.json.

Переменные окружения (в GitHub — Settings → Secrets and variables → Actions):
  IMAP_USER      — адрес почты, например example@yandex.ru
  IMAP_PASSWORD  — пароль приложения Яндекса (не основной пароль!)
  TG_TOKEN       — токен бота от @BotFather
  TG_CHAT_ID     — ID канала (-100...) или @username публичного канала
Необязательные:
  IMAP_HOST      — по умолчанию imap.yandex.ru
  IMAP_FOLDER    — по умолчанию INBOX
  TZ_NAME        — часовой пояс для даты письма, по умолчанию Europe/Samara
  MAX_PER_RUN    — сколько писем максимум за один запуск, по умолчанию 20
  BACKFILL_SINCE — дата (2026-09-01 или 01.09.2026): переслать все письма,
                   пришедшие начиная с этого дня. Задаётся при ручном запуске.

ВАЖНО: в публичном репозитории логи Actions видны всем, поэтому скрипт
не печатает в лог ни темы, ни адреса, ни текст писем — только счётчики.
"""

import email
import email.policy
import email.utils
import html
import imaplib
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

STATE_FILE = Path(__file__).with_name("state.json")
TG_TEXT_LIMIT = 4096          # лимит Telegram на одно сообщение
BODY_LIMIT = 3300             # сколько символов текста письма показывать
TG_FILE_LIMIT = 50 * 1024 * 1024  # лимит Bot API на отправку файла


# ---------------------------------------------------------------- настройки

def env(name, default=None, required=True):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Не задана переменная окружения {name}")
    return value


IMAP_HOST = env("IMAP_HOST", "imap.yandex.ru")
IMAP_USER = env("IMAP_USER")
IMAP_PASSWORD = env("IMAP_PASSWORD")
IMAP_FOLDER = env("IMAP_FOLDER", "INBOX")
TG_TOKEN = env("TG_TOKEN")
TG_CHAT_ID = env("TG_CHAT_ID")
TZ = ZoneInfo(env("TZ_NAME", "Europe/Samara"))
MAX_PER_RUN = int(env("MAX_PER_RUN", required=False) or "20")
BACKFILL_SINCE = (os.environ.get("BACKFILL_SINCE") or "").strip()

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"


# ---------------------------------------------------------------- состояние

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- Telegram

def tg_call(method, data=None, files=None):
    """Вызов Bot API с повтором при ограничении частоты (429)."""
    for attempt in range(5):
        resp = requests.post(f"{TG_API}/{method}", data=data, files=files, timeout=60)
        payload = resp.json()
        if payload.get("ok"):
            return payload["result"]
        if resp.status_code == 429:
            wait = payload.get("parameters", {}).get("retry_after", 5)
            time.sleep(wait + 1)
            continue
        raise RuntimeError(f"Telegram {method}: {payload.get('description')}")
    raise RuntimeError(f"Telegram {method}: слишком много повторов")


def send_text(text, reply_to=None):
    data = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_to:
        data["reply_to_message_id"] = reply_to
    return tg_call("sendMessage", data)


def send_file(filename, content, reply_to=None):
    data = {"chat_id": TG_CHAT_ID}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    return tg_call("sendDocument", data, files={"document": (filename, content)})


# ---------------------------------------------------------------- разбор письма

class _HTMLToText(HTMLParser):
    BLOCK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(raw):
    parser = _HTMLToText()
    parser.feed(raw)
    return "".join(parser.parts)


def clean_text(text):
    text = text.replace("\r", "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def get_body(msg):
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        text = html_to_text(text)
    return clean_text(text)


def get_attachments(msg):
    result = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        # inline-картинки (логотипы в подписях) пропускаем
        if not filename or disposition == "inline":
            continue
        content = part.get_payload(decode=True) or b""
        result.append((filename, content))
    return result


def format_date(msg):
    try:
        dt = email.utils.parsedate_to_datetime(msg["Date"])
        if dt.tzinfo is None:
            return dt.strftime("%d.%m.%Y %H:%M")
        return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""


def build_message(msg):
    subject = str(msg.get("Subject") or "(без темы)").strip()
    sender = str(msg.get("From") or "").strip()
    date = format_date(msg)
    body = get_body(msg)

    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT].rstrip() + "\n…\n[письмо обрезано, полный текст — в почте]"

    lines = [f"📧 <b>{html.escape(subject)}</b>"]
    if sender:
        lines.append(f"От: {html.escape(sender)}")
    if date:
        lines.append(f"Дата: {date}")
    text = "\n".join(lines)
    if body:
        text += "\n\n" + html.escape(body)
    return text[:TG_TEXT_LIMIT]


def forward(raw_bytes):
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    sent = send_text(build_message(msg))
    reply_to = sent["message_id"]

    for filename, content in get_attachments(msg):
        if len(content) > TG_FILE_LIMIT:
            send_text(
                f"📎 Файл «{html.escape(filename)}» больше 50 МБ — откройте его в почте.",
                reply_to=reply_to,
            )
        else:
            send_file(filename, content, reply_to=reply_to)
        time.sleep(1)


# ---------------------------------------------------------------- IMAP

def folder_status(imap):
    typ, data = imap.status(IMAP_FOLDER, "(UIDVALIDITY UIDNEXT)")
    if typ != "OK":
        raise RuntimeError("Не удалось получить статус папки")
    line = data[0].decode()
    uidvalidity = int(re.search(r"UIDVALIDITY (\d+)", line).group(1))
    uidnext = int(re.search(r"UIDNEXT (\d+)", line).group(1))
    return uidvalidity, uidnext


IMAP_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def parse_date(value):
    """Принимает 2026-09-01 или 01.09.2026, возвращает дату для IMAP (01-Sep-2026)."""
    value = value.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value) or None
    if m:
        year, month, day = (int(x) for x in m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", value)
        if not m:
            sys.exit("Дата должна быть в формате 2026-09-01 или 01.09.2026")
        day, month, year = (int(x) for x in m.groups())
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        sys.exit("Некорректная дата")
    return f"{day:02d}-{IMAP_MONTHS[month - 1]}-{year}"


def find_start_uid(imap, since):
    imap.select(IMAP_FOLDER, readonly=True)
    typ, data = imap.uid("search", None, "SINCE", parse_date(since))
    uids = [int(u) for u in data[0].split()] if typ == "OK" else []
    return min(uids) if uids else None


def main():
    state = load_state()

    imap = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    try:
        imap.login(IMAP_USER, IMAP_PASSWORD)
        uidvalidity, uidnext = folder_status(imap)

        # Ручной запуск с датой: перематываем позицию назад, к первому
        # письму, пришедшему в этот день или позже.
        if BACKFILL_SINCE:
            start_uid = find_start_uid(imap, BACKFILL_SINCE)
            if start_uid is None:
                print("С указанной даты писем нет.")
                state = {"uidvalidity": uidvalidity, "last_uid": uidnext - 1}
                save_state(state)
                return
            state = {"uidvalidity": uidvalidity, "last_uid": start_uid - 1}
            save_state(state)
            print("Позиция перемотана к указанной дате.")
            send_text(f"📬 Публикую письма, пришедшие начиная с {html.escape(BACKFILL_SINCE)}.")

        # Первый запуск (или Яндекс пересоздал нумерацию писем):
        # запоминаем текущую позицию и старые письма не пересылаем.
        if state.get("uidvalidity") != uidvalidity:
            save_state({"uidvalidity": uidvalidity, "last_uid": uidnext - 1})
            print("Первый запуск: позиция сохранена, старые письма не пересылаются.")
            send_text("✅ Бот подключён. Новые письма будут появляться здесь.")
            return

        last_uid = int(state.get("last_uid", 0))
        if uidnext - 1 <= last_uid:
            print("Новых писем нет.")
            return

        imap.select(IMAP_FOLDER, readonly=True)
        typ, data = imap.uid("search", None, f"UID {last_uid + 1}:*")
        uids = sorted(int(u) for u in data[0].split() if int(u) > last_uid)
        if not uids:
            print("Новых писем нет.")
            return

        batch = uids[:MAX_PER_RUN]
        sent = 0
        for uid in batch:
            typ, fetched = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
            raw = next((item[1] for item in fetched if isinstance(item, tuple)), None)
            if raw:
                forward(raw)
                sent += 1
            # сохраняем после каждого письма, чтобы при сбое не было дублей
            state["last_uid"] = uid
            save_state(state)
            time.sleep(1)

        rest = len(uids) - len(batch)
        print(f"Переслано писем: {sent}." + (f" Осталось на следующий запуск: {rest}." if rest else ""))
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
