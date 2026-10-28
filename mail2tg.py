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

# Невидимые символы, которыми рассылки делают отступы и прячут «прехедер»
INVISIBLE_RE = re.compile("[­͏؜ᅟᅠ឴឵᠎"
                          "​-‏‪-‮⁠-⁯⠀ㅤ﻿ﾠ]")
SKIP_TAGS = {"script", "style", "head", "title"}
BLOCK_TAGS = {"br", "p", "div", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "ul", "ol", "section", "header", "footer", "center", "hr"}


def strip_invisible(text):
    return INVISIBLE_RE.sub("", text.replace("\xa0", " "))


class _HTMLToTelegram(HTMLParser):
    """Превращает HTML письма в текст для Telegram: строки + ссылки <a>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []      # уже экранированные куски
        self.skip = 0
        self.links = []      # стек: (href, индекс начала в parts)

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in ("td", "th"):
            self.parts.append(" ")
        elif tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if not re.match(r"(https?://|mailto:)", href, re.I):
                href = ""
            self.links.append((href, len(self.parts)))

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK_TAGS or tag == "li":
            self.parts.append("\n")
        elif tag == "a" and self.links:
            href, start = self.links.pop()
            inner = re.sub(r"\s+", " ", "".join(self.parts[start:])).strip()
            del self.parts[start:]
            if href and inner and "<a " not in inner:
                self.parts.append(f' <a href="{html.escape(href, quote=True)}">{inner}</a> ')
            elif inner:
                self.parts.append(f" {inner} ")

    def handle_data(self, data):
        if self.skip:
            return
        text = re.sub(r"\s+", " ", strip_invisible(data))
        if text:
            self.parts.append(html.escape(text, quote=False))


def tidy_lines(text):
    """Убирает пустые и «пустотные» строки, приклеивает огрызки вроде «₽»."""
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        visible = re.sub(r"<[^>]+>", "", line).strip()
        if not visible or not re.search(r"\w", visible):
            if visible in ("•",) or not visible:
                continue
        if lines and len(visible) <= 2 and "<a " not in line:
            lines[-1] += " " + line
        else:
            lines.append(line)
    return lines


def html_body_lines(raw):
    parser = _HTMLToTelegram()
    parser.feed(raw)
    parser.close()
    return tidy_lines("".join(parser.parts))


def plain_body_lines(raw):
    text = strip_invisible(raw.replace("\r", ""))
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    lines = [html.escape(re.sub(r"[ \t]+", " ", l).rstrip(), quote=False)
             for l in text.strip().split("\n")]
    return lines


def part_text(part):
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def get_body_lines(msg):
    """Строки текста письма в формате Telegram HTML (уже экранированы)."""
    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        lines = html_body_lines(part_text(html_part))
        if lines:
            return lines
    plain_part = msg.get_body(preferencelist=("plain",))
    if plain_part is not None:
        return plain_body_lines(part_text(plain_part))
    return []


def limit_lines(lines, limit):
    """Обрезает текст по целым строкам, не ломая HTML-теги."""
    out, total = [], 0
    for line in lines:
        if total + len(line) + 1 > limit:
            room = limit - total
            plain = html.unescape(re.sub(r"<[^>]+>", "", line))
            if room > 80:
                out.append(html.escape(plain[:room - 20].rstrip(), quote=False) + "…")
            out.append("…\n<i>[письмо обрезано, полный текст — в почте]</i>")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


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


def build_header(msg):
    subject = strip_invisible(str(msg.get("Subject") or "(без темы)")).strip()
    sender = strip_invisible(str(msg.get("From") or "")).strip()
    date = format_date(msg)
    lines = [f"📧 <b>{html.escape(subject)}</b>"]
    if sender:
        lines.append(f"От: {html.escape(sender)}")
    if date:
        lines.append(f"Дата: {date}")
    return "\n".join(lines)


def build_message(msg):
    text = build_header(msg)
    body = limit_lines(get_body_lines(msg), BODY_LIMIT)
    if body:
        text += "\n\n" + body
    return text


def to_plain(text):
    """Запасной вариант без разметки, если Telegram не принял HTML."""
    text = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2 (\1)", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))[:TG_TEXT_LIMIT]


def forward(raw_bytes):
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    text = build_message(msg)
    try:
        sent = send_text(text)
    except RuntimeError as err:
        if "parse" not in str(err).lower() and "too long" not in str(err).lower():
            raise
        sent = tg_call("sendMessage", {
            "chat_id": TG_CHAT_ID,
            "text": to_plain(text),
            "disable_web_page_preview": "true",
        })
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
