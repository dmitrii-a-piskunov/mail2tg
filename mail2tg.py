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
import email.header
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
PART_LIMIT = 3800             # видимых символов в одной части длинного письма
RAW_LIMIT = 20000             # максимум символов разметки в одной части
MAX_PARTS = 10                # больше частей не отправляем (защита от спама)
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


CELL_SEP = " | "        # разделитель ячеек, когда таблицу не удалось разобрать
ROW_MAX_LEN = 300       # строки длиннее считаем вёрсткой, а не таблицей
HEADER_CELL_MAX = 30    # ячейка заголовка таблицы — короткий текст
NARROW_ROW = 40         # строки таблицы не длиннее — оставляем как есть через « | »
HREF_RE = re.compile(r'href="([^"]*)"')


def is_link_cell(cell):
    cell = cell.strip()
    return cell.startswith("<a ") and cell.endswith("</a>") and cell.count("<a ") == 1


def looks_like_header(cells):
    return all(
        "<a " not in c and visible_len(c) <= HEADER_CELL_MAX and not re.search(r"\d", c)
        for c in cells
    )


def render_table_rows(rows):
    """Превращает строки таблицы с данными в удобный для телефона текст.

    rows — список (ячейки, строка_заголовка), ячейки уже в формате Telegram HTML.
    """
    out = []
    if not rows:
        return out
    first, first_is_th = rows[0]
    n = len(first)

    # 0) Узкая таблица (короткое расписание и т.п.) — строками через « | »,
    #    так она и на телефоне читается как таблица.
    has_header = len(rows) >= 2 and n >= 2 and (first_is_th or looks_like_header(first))
    key_value = not has_header and all(len(cells) == 2 for cells, _ in rows)
    if not key_value and all(visible_len(CELL_SEP.join(cells)) <= NARROW_ROW for cells, _ in rows):
        if has_header:
            out.append("<b>" + CELL_SEP.join(first) + "</b>")
            rows = rows[1:]
        out.extend(CELL_SEP.join(cells) for cells, _ in rows)
        return out

    # 1) Таблица с заголовком: каждая строка — карточка с подписями.
    #    • Патч-корд Cablexpert
    #    Кол-во: 1 шт. · Цена: 370 ₽ · Сумма: 370 ₽
    if len(rows) >= 2 and (first_is_th or looks_like_header(first)):
        headers = first
        for cells, _ in rows[1:]:
            if len(cells) == n:
                out.append("• " + cells[0])
                details = [f"{h}: {v}" for h, v in zip(headers[1:], cells[1:]) if v]
                if details:
                    out.append(" · ".join(details))
            else:
                out.append(" ".join(cells))
        return out

    # 2) Товары/новости колонками: первая строка — ссылки, ниже — их цены и кнопки.
    #    Перестраиваем «по колонкам», дубли ссылок («Подробнее») убираем.
    if (len(rows) >= 2 and n >= 2 and all(is_link_cell(c) for c in first)
            and all(len(cells) == n for cells, _ in rows)):
        for j in range(n):
            name = first[j]
            seen = set(HREF_RE.findall(name))
            extra = []
            for cells, _ in rows[1:]:
                cell = cells[j]
                hrefs = set(HREF_RE.findall(cell))
                if hrefs and hrefs <= seen:
                    continue
                seen |= hrefs
                extra.append(cell)
            out.append("• " + name)
            if extra:
                out.append(" · ".join(extra))
        return out

    # 3) Остальное: пары «ключ: значение» или ячейки через « | ».
    for cells, _ in rows:
        key = cells[0].strip()
        if len(cells) == 2 and "<a " not in key and visible_len(key) <= HEADER_CELL_MAX:
            out.append(key + (" " if key.endswith(":") else ": ") + cells[1])
        else:
            out.append(CELL_SEP.join(cells))
    return out


class _HTMLToTelegram(HTMLParser):
    """Превращает HTML письма в текст для Telegram: строки + ссылки <a>.

    Небольшие таблицы с данными собираются в карточки (render_table_rows).
    Большие ячейки (вёрстка письма, а не таблица) выводятся друг под другом.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bufs = [[]]     # стек буферов: основной текст и открытые ячейки
        self.rows = []       # стек открытых строк: {"cells": [(строки, th)], "cell": bool, "th": bool}
        self.tables = []     # стек открытых таблиц: {"depth": строк до неё, "items": [...]}
        self.skip = 0
        self.links = []      # стек: (href, буфер, индекс начала)

    # -- буферы и таблицы
    @property
    def buf(self):
        return self.bufs[-1]

    def row_in_table(self):
        return not self.tables or len(self.rows) > self.tables[-1]["depth"]

    def open_cell(self, tag):
        if not self.rows or not self.row_in_table():
            self.rows.append({"cells": [], "cell": False, "th": False})
        if self.rows[-1]["cell"]:
            self.close_cell()
        self.bufs.append([])
        self.rows[-1]["cell"] = True
        self.rows[-1]["th"] = tag == "th"

    def close_cell(self):
        row = self.rows[-1]
        if not row["cell"]:
            return
        lines = tidy_lines("".join(self.bufs.pop()))
        row["cell"] = False
        if lines:
            row["cells"].append((lines, row["th"]))

    def open_row(self):
        if self.rows and not self.rows[-1]["cell"] and self.row_in_table():
            self.close_row()          # предыдущая строка не была закрыта
        self.rows.append({"cells": [], "cell": False, "th": False})

    def close_row(self):
        if not self.rows:
            return
        if self.rows[-1]["cell"]:
            self.close_cell()
        cells = self.rows.pop()["cells"]
        if not cells:
            return
        flat = [" ".join(lines) for lines, _ in cells]
        is_data_row = (
            len(cells) > 1
            and all(len(lines) <= 3 for lines, _ in cells)
            and sum(visible_len(c) for c in flat) <= ROW_MAX_LEN
        )
        if is_data_row:
            item = ("data", flat, all(th for _, th in cells))
        else:
            item = ("text", "\n".join(line for lines, _ in cells for line in lines))

        table = self.tables[-1] if self.tables else None
        if table is not None and len(self.rows) == table["depth"]:
            table["items"].append(item)
        else:
            self.emit_items([item])

    def emit_items(self, items):
        """Выводит строки таблицы, группируя подряд идущие строки с данными."""
        lines, group = [], []
        for item in items + [("text", None)]:
            if item[0] == "data":
                group.append((item[1], item[2]))
                continue
            lines.extend(render_table_rows(group))
            group = []
            if item[1]:
                lines.append(item[1])
        if lines:
            self.buf.append("\n" + "\n".join(lines) + "\n")

    def close_table(self):
        table = self.tables[-1]
        while len(self.rows) > table["depth"]:
            self.close_row()
        self.tables.pop()
        self.emit_items(table["items"])

    # -- разбор
    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag == "table":
            self.buf.append("\n")
            self.tables.append({"depth": len(self.rows), "items": []})
        elif tag == "tr":
            self.open_row()
        elif tag in ("td", "th"):
            self.open_cell(tag)
        elif tag in BLOCK_TAGS:
            self.buf.append("\n")
        elif tag == "li":
            self.buf.append("\n• ")
        elif tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if not re.match(r"(https?://|mailto:)", href, re.I):
                href = ""
            self.links.append((href, self.buf, len(self.buf)))

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag == "table":
            if self.tables:
                self.close_table()
            self.buf.append("\n")
        elif tag == "tr":
            if self.rows and self.row_in_table():
                self.close_row()
        elif tag in ("td", "th"):
            if self.rows:
                self.close_cell()
        elif tag in BLOCK_TAGS or tag == "li":
            self.buf.append("\n")
        elif tag == "a" and self.links:
            href, buf, start = self.links.pop()
            if buf is not self.buf:
                return                # ссылка «перешагнула» ячейку — оставляем как есть
            inner = re.sub(r"\s+", " ", "".join(buf[start:])).strip()
            del buf[start:]
            if href and inner and "<a " not in inner:
                buf.append(f' <a href="{html.escape(href, quote=True)}">{inner}</a> ')
            elif inner:
                buf.append(f" {inner} ")

    def handle_data(self, data):
        if self.skip:
            return
        text = re.sub(r"\s+", " ", strip_invisible(data))
        if text:
            self.buf.append(html.escape(text, quote=False))

    def result(self):
        while self.tables:
            self.close_table()
        while self.rows:
            self.close_row()
        while len(self.bufs) > 1:     # незакрытые ячейки без строк
            inner = self.bufs.pop()
            self.buf.extend(inner)
        return "".join(self.bufs[0])


def tidy_lines(text):
    """Убирает пустые и «пустотные» строки, приклеивает огрызки вроде «₽»."""
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        visible = re.sub(r"<[^>]+>", "", line).strip()
        if not visible or visible == "•":
            continue
        if lines and len(visible) <= 2 and "<a " not in line and CELL_SEP.strip() not in line:
            lines[-1] += " " + line
        else:
            lines.append(line)
    return lines


def html_body_lines(raw):
    parser = _HTMLToTelegram()
    parser.feed(raw)
    parser.close()
    return tidy_lines(parser.result())


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


def visible_len(line):
    """Длина строки так, как её считает Telegram: без тегов и адресов ссылок."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", line)))


def split_long_line(line, limit):
    """Очень длинную строку без разметки режет на куски."""
    if visible_len(line) <= limit:
        return [line]
    plain = html.unescape(re.sub(r"<[^>]+>", "", line))
    return [html.escape(plain[i:i + limit], quote=False) for i in range(0, len(plain), limit)]


def split_parts(header, lines):
    """Делит письмо на части, каждая влезает в одно сообщение Telegram."""
    parts, current = [], [header, ""]
    size, raw = visible_len(header) + 1, len(header) + 1
    for line in lines:
        for piece in split_long_line(line, PART_LIMIT - 100):
            length = visible_len(piece) + 1
            # второе условие — подстраховка от огромных адресов ссылок
            if size + length > PART_LIMIT or raw + len(piece) > RAW_LIMIT:
                parts.append("\n".join(current).strip())
                current, size, raw = [], 0, 0
            current.append(piece)
            size += length
            raw += len(piece) + 1
    if current:
        parts.append("\n".join(current).strip())
    parts = [p for p in parts if p]

    if len(parts) > MAX_PARTS:
        parts = parts[:MAX_PARTS]
        parts[-1] += "\n…\n<i>[письмо очень длинное, продолжение — в почте]</i>"
    if len(parts) > 1:
        parts = [f"{p}\n\n<i>(часть {i} из {len(parts)})</i>" for i, p in enumerate(parts, 1)]
    return parts


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


def decode_hdr(raw_msg, name):
    """Декодирует заголовок письма.

    Рассылки часто режут длинное имя на куски посреди слова
    (=?utf-8?B?...?= =?utf-8?B?...?=). Куски нужно склеить без пробелов,
    иначе получается «гипермар кет цифрово й».
    """
    value = raw_msg.get(name)
    if not value:
        return ""
    value = re.sub(r"\r?\n[ \t]+", " ", str(value))
    # пробелы между соседними закодированными кусками (даже внутри кавычек) убираем
    value = re.sub(r"(\?=)\s+(=\?)", r"\1\2", value)
    try:
        text = str(email.header.make_header(email.header.decode_header(value)))
    except Exception:
        text = value
    return strip_invisible(re.sub(r"\s+", " ", text)).strip()


def build_header(msg, raw_msg):
    subject = decode_hdr(raw_msg, "Subject") or "(без темы)"
    sender = decode_hdr(raw_msg, "From")
    name, addr = email.utils.parseaddr(sender)
    if addr:
        name = name.strip(' "\'')
        sender = f"{name} <{addr}>" if name else addr
    date = format_date(msg)
    lines = [f"📧 <b>{html.escape(subject)}</b>"]
    if sender:
        lines.append(f"От: {html.escape(sender)}")
    if date:
        lines.append(f"Дата: {date}")
    return "\n".join(lines)


def build_messages(msg, raw_msg):
    return split_parts(build_header(msg, raw_msg), get_body_lines(msg))


def to_plain(text):
    """Запасной вариант без разметки, если Telegram не принял HTML."""
    text = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2 (\1)", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))[:TG_TEXT_LIMIT]


def forward(raw_bytes):
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    raw_msg = email.message_from_bytes(raw_bytes, policy=email.policy.compat32)
    reply_to = None
    for text in build_messages(msg, raw_msg):
        try:
            sent = send_text(text, reply_to=reply_to)
        except RuntimeError as err:
            if "parse" not in str(err).lower() and "too long" not in str(err).lower():
                raise
            data = {"chat_id": TG_CHAT_ID, "text": to_plain(text),
                    "disable_web_page_preview": "true"}
            if reply_to:
                data["reply_to_message_id"] = reply_to
            sent = tg_call("sendMessage", data)
        if reply_to is None:
            reply_to = sent["message_id"]   # продолжение и файлы — ответом на первую часть
        time.sleep(1)

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
