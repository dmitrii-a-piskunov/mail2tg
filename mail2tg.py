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
  SCREENSHOTS    — картинка письма: always (для всех писем, по умолчанию),
                   auto (только письма с таблицами) или off (никогда)
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
# Скриншот письма: always — для всех писем, auto — только с таблицами, off — никогда
SCREENSHOTS = (os.environ.get("SCREENSHOTS") or "always").strip().lower()

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
    """Вызов Bot API с повторами: при обрыве связи и при ограничении частоты (429)."""
    for attempt in range(5):
        try:
            resp = requests.post(f"{TG_API}/{method}", data=data, files=files, timeout=60)
            payload = resp.json()
        except (requests.ConnectionError, requests.Timeout, ValueError):
            # обрыв соединения или пустой ответ — ждём и пробуем ещё раз
            if attempt == 4:
                raise
            time.sleep(3 * (attempt + 1))
            continue
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


def strip_invisible(text):
    return INVISIBLE_RE.sub("", text.replace("\xa0", " "))


# Как устроен разбор HTML.
#
# Парсер выдаёт один длинный текст, в котором кроме обычного (уже экранированного)
# текста есть служебные метки:
#   \x01+b\x01 … \x01-b\x01      — начало/конец жирного (b, i, u, s, ссылка a)
#   отдельная строка \x02P       — граница абзаца (пустая строка)
#   отдельная строка \x02R       — разделитель строк таблицы
#   \x02Q+ / \x02Q-              — начало/конец цитаты
#   \x02E+ / \x02E-              — начало/конец моноширинного блока <pre>
# Затем render_marked() превращает это в список записей (текст, вид, цитата),
# закрывая и заново открывая теги на каждой строке, чтобы разметка всегда
# была корректной для Telegram — даже когда письмо режется на части.

PARA, ROWSEP = "\n\x02P\n", "\n\x02R\n"
Q_OPEN, Q_CLOSE = "\n\x02Q+\n", "\n\x02Q-\n"
PRE_OPEN, PRE_CLOSE = "\n\x02E+\n", "\n\x02E-\n"
TOKEN_RE = re.compile("\x01([+-])([a-z]+)(?:\x03([^\x01]*))?\x01")
STRUCT_RE = re.compile("\x02(?:P|R|Q\\+|Q-|E\\+|E-|T|C)")

ROW_SEP = "────────"      # разделитель между строками таблицы
ROW_MAX_LEN = 300        # строки длиннее считаем вёрсткой, а не таблицей с данными
COMPACT_LEN = 90         # строка таблицы не длиннее — выводится одной строкой
TABLE_COLLAPSE = 8       # таблица больше стольких строк сворачивается в цитату
ROW_MARK, CELL_MARK = "\x02T", "\x02C"   # метки строк таблицы (см. render_marked)
INDENT = " "        # отступ для вложенных списков (не съедается Telegram)
BULLETS = ["•", "◦", "▪"]

SKIP_TAGS = {"script", "style", "head", "title"}
VOID_TAGS = {"br", "hr", "img", "meta", "link", "input", "wbr", "col", "source", "area", "base"}
BLOCK_TAGS = {"div", "section", "article", "header", "footer", "center", "main",
              "nav", "aside", "address", "figure", "figcaption", "dl", "dt", "dd", "form"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
TAG_STYLES = {"b": "b", "strong": "b", "th": "b", "i": "i", "em": "i", "cite": "i",
              "u": "u", "ins": "u", "s": "s", "strike": "s", "del": "s"}
CELL_TAGS = {"td", "th"}
TABLE_STOP = {"td", "th", "tr", "table"}

FWD_RE = re.compile(r"forwarded message|begin forwarded|пересылаемое сообщение|"
                    r"пересланное сообщение|переадресованн|(subject|тема):\s*(fw|fwd|пересл)",
                    re.I)


def open_tok(name, href=None):
    return f"\x01+{name}\x03{href}\x01" if href is not None else f"\x01+{name}\x01"


def close_tok(name):
    return f"\x01-{name}\x01"


def strip_marks(text):
    """Видимый текст: без служебных меток и HTML-тегов."""
    text = TOKEN_RE.sub("", STRUCT_RE.sub("", text))
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def visible_len(line):
    """Длина строки так, как её считает Telegram: без тегов и адресов ссылок."""
    return len(strip_marks(line))


def style_tokens(tag, attrs):
    """Какие начертания включает элемент: по тегу и по inline-стилю."""
    names = []
    if tag in TAG_STYLES:
        names.append(TAG_STYLES[tag])
    if tag in HEADINGS:
        names.append("b")
    style = (attrs.get("style") or "").lower()
    if style:
        if re.search(r"font-weight\s*:\s*(bold|bolder|[6-9]00)", style):
            names.append("b")
        if re.search(r"font-style\s*:\s*italic", style):
            names.append("i")
        if re.search(r"text-decoration[^;]*line-through", style):
            names.append("s")
    return list(dict.fromkeys(names))


def zero_margin(style, side):
    """True, если у <p> нулевой внешний отступ сверху/снизу (тогда это не абзац)."""
    style = (style or "").lower().replace(" ", "")
    m = re.search(rf"margin-{side}:(-?[\d.]+)", style)
    if m:
        return float(m.group(1)) == 0
    m = re.search(r"margin:([^;]+)", style)
    if not m:
        return False
    values = re.findall(r"-?[\d.]+", m.group(1))
    if not values:
        return False
    if side == "top":
        value = values[0]
    else:
        value = values[2] if len(values) >= 3 else values[0]
    return float(value) == 0


class _HTMLToTelegram(HTMLParser):
    """Разбирает HTML письма в текст со служебными метками (см. выше)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bufs = [[]]       # стек буферов: основной текст и открытые ячейки таблиц
        self.elems = []        # стек открытых элементов: {"tag", "tokens", "end", ...}
        self.rows = []         # стек открытых строк таблиц: {"cells": [...], "cell": bool}
        self.tables = []       # для каждой открытой <table> — сколько строк было открыто до неё
        self.lists = []        # стек списков: {"ol": bool, "n": номер}
        self.blocks = []       # стек div/p: был ли в них текст и <br> (для пустых строк)
        self.links = []        # стек ссылок: (href, буфер, индекс начала)
        self.skip = 0
        self.pre = 0
        self.prefix = None     # маркер пункта списка, ждущий первого текста
        self.quote_rest = False
        self.has_table = False
        self.hidden = None     # [тег, глубина] — внутри скрытого элемента (display:none)

    @property
    def buf(self):
        return self.bufs[-1]

    def emit(self, text):
        self.buf.append(text)

    # -- стек элементов
    def push(self, tag, tokens=(), end="", **extra):
        for name in tokens:
            self.emit(open_tok(name))
        self.elems.append({"tag": tag, "tokens": list(tokens), "end": end, **extra})

    def pop_entry(self):
        entry = self.elems.pop()
        for name in reversed(entry["tokens"]):
            self.emit(close_tok(name))
        if entry.get("list"):
            if self.lists:
                self.lists.pop()
        if entry.get("block") and self.blocks:
            info = self.blocks.pop()
            if not info["text"] and (info["br"] or info.get("nbsp")):
                self.emit(PARA)            # <div><br></div>, <p>&nbsp;</p> — пустая строка
            elif info["text"] and self.blocks:
                self.blocks[-1]["text"] = True
        if entry.get("pre"):
            self.pre = max(0, self.pre - 1)
        self.emit(entry["end"])

    def pop_to(self, names, stop=TABLE_STOP):
        """Закрывает элементы до ближайшего с тегом из names (не выходя за stop)."""
        for i in range(len(self.elems) - 1, -1, -1):
            tag = self.elems[i]["tag"]
            if tag in names:
                while len(self.elems) > i:
                    self.pop_entry()
                return True
            if tag in stop:
                return False
        return False

    # -- таблицы
    def row_in_table(self):
        return not self.tables or len(self.rows) > self.tables[-1]

    def open_cell(self):
        if not self.rows or not self.row_in_table():
            self.rows.append({"cells": [], "cell": False})
        if self.rows[-1]["cell"]:
            self.pop_to(CELL_TAGS, stop={"tr", "table"})
            self.close_cell()
        self.bufs.append([])
        self.rows[-1]["cell"] = True

    def close_cell(self):
        row = self.rows[-1]
        if not row["cell"]:
            return
        raw = "".join(self.bufs.pop())
        row["cell"] = False
        if strip_marks(raw).strip():
            row["cells"].append(raw)

    def open_row(self):
        if self.rows and self.row_in_table():
            self.pop_to({"tr"}, stop={"table"})
            self.close_row()
        self.rows.append({"cells": [], "cell": False})

    def close_row(self):
        if not self.rows:
            return
        if self.rows[-1]["cell"]:
            self.close_cell()
        cells = self.rows.pop()["cells"]
        if not cells:
            return
        visible = [[l for l in (strip_marks(x).strip() for x in c.split("\n")) if l] for c in cells]
        is_data_row = (
            len(cells) > 1
            and all(len(v) <= 3 for v in visible)
            and sum(len(" ".join(v)) for v in visible) <= ROW_MAX_LEN
        )
        if is_data_row:
            self.has_table = True
            flat = [re.sub(r"\s+", " ", STRUCT_RE.sub(" ", c.replace("\n", " "))).strip()
                    for c in cells]
            vis = [strip_marks(c).strip() for c in flat]
            if len(flat) == 2 and len(vis[0]) + len(vis[1]) + 2 <= COMPACT_LEN:
                # пара «название — значение»: «Смена: 15»
                if vis[0].endswith(":") or vis[1].startswith(("=", ":")):
                    glue = " "
                elif ":" in vis[0]:
                    glue = " · "             # в названии уже есть двоеточие («1: Майонез»)
                else:
                    glue = ": "
                self.emit("\n" + ROW_MARK + flat[0] + glue + flat[1] + "\n")
            elif len(" · ".join(vis)) <= COMPACT_LEN:
                self.emit("\n" + ROW_MARK + " · ".join(flat) + "\n")
            else:
                # длинная строка — каждая ячейка отдельно, между строками черта
                self.emit(ROWSEP + "\n".join(CELL_MARK + c for c in flat) + ROWSEP)
        else:
            self.emit("\n" + "\n".join(cells) + "\n")

    # -- разбор
    def handle_starttag(self, tag, attrs_list):
        attrs = {k: (v or "") for k, v in attrs_list}
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if self.hidden:
            if tag == self.hidden[0] and tag not in VOID_TAGS:
                self.hidden[1] += 1
            return
        style = (attrs.get("style") or "").lower().replace(" ", "")
        if tag not in VOID_TAGS and ("display:none" in style or "mso-hide:all" in style
                                     or "visibility:hidden" in style or "hidden" in attrs):
            self.hidden = [tag, 1]         # скрытый текст рассылок — пропускаем
            return

        if tag == "br":
            self.emit("\n")
            if self.blocks:
                self.blocks[-1]["br"] = True
            return
        if tag == "hr":
            self.emit(PARA)
            return
        if tag in VOID_TAGS:
            return

        # Outlook: всё после этой метки — история переписки
        ident = (attrs.get("id") or "").lower()
        if not self.quote_rest and ident in ("divrplyfwdmsg", "appendonsend"):
            self.quote_rest = True
            self.emit(Q_OPEN)

        styles = style_tokens(tag, attrs)
        cls = (attrs.get("class") or "").lower()

        if tag == "table":
            self.emit("\n")
            self.tables.append(len(self.rows))
            self.push(tag, styles, "\n")
        elif tag == "tr":
            self.open_row()
            self.push(tag, styles)
        elif tag in CELL_TAGS:
            self.open_cell()
            self.push(tag, styles)
        elif tag in ("ul", "ol"):
            top = not self.lists
            start = attrs.get("start") or "1"
            self.lists.append({"ol": tag == "ol", "n": int(start) - 1 if start.isdigit() else 0})
            self.emit(PARA if top else "\n")
            self.push(tag, styles, PARA if top else "\n", list=True)
        elif tag == "li":
            self.pop_to({"li"}, stop={"ul", "ol"} | TABLE_STOP)
            depth = max(len(self.lists), 1)
            if self.lists and self.lists[-1]["ol"]:
                self.lists[-1]["n"] += 1
                marker = f"{self.lists[-1]['n']}."
            else:
                marker = BULLETS[min(depth, len(BULLETS)) - 1]
            self.emit("\n")
            self.prefix = INDENT * (depth - 1) + marker + " "
            self.push(tag, styles, "\n")
        elif tag == "blockquote":
            self.emit(Q_OPEN)
            self.push(tag, styles, Q_CLOSE)
        elif tag == "pre":
            self.emit(PRE_OPEN)
            self.pre += 1
            self.push(tag, (), PRE_CLOSE, pre=True)
        elif tag == "p" or tag in HEADINGS:
            style = attrs.get("style")
            in_li = any(e["tag"] == "li" for e in self.elems)
            if in_li:
                style = "margin:0"          # абзац внутри пункта списка — без пустых строк
            is_para = tag in HEADINGS or not zero_margin(style, "top")
            self.emit(PARA if is_para else "\n")
            self.blocks.append({"text": False, "br": False})
            end = PARA if ((tag in HEADINGS and not in_li) or not zero_margin(style, "bottom")) else "\n"
            self.push(tag, styles, end, block=True)
        elif tag in BLOCK_TAGS:
            if "gmail_quote" in cls and "gmail_quote_container" not in cls:
                self.emit(Q_OPEN)
                self.blocks.append({"text": False, "br": False})
                self.push(tag, styles, Q_CLOSE, block=True)
            else:
                self.emit("\n")
                self.blocks.append({"text": False, "br": False})
                self.push(tag, styles, "\n", block=True)
        elif tag == "a":
            if self.prefix:
                self.emit(self.prefix)
                self.prefix = None
            href = attrs.get("href", "").strip()
            if not re.match(r"(https?://|mailto:|tel:)", href, re.I):
                href = ""
            self.links.append((href, self.buf, len(self.buf)))
            self.push(tag, styles)
        else:
            self.push(tag, styles)

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag in VOID_TAGS:
            return
        if self.hidden:
            if tag == self.hidden[0]:
                self.hidden[1] -= 1
                if self.hidden[1] == 0:
                    self.hidden = None
            return
        if tag == "table":
            if self.pop_to({"table"}, stop=set()) and self.tables:
                depth = self.tables.pop()
                while len(self.rows) > depth:
                    self.close_row()
        elif tag == "tr":
            if self.pop_to({"tr"}, stop={"table"}) and self.rows and self.row_in_table():
                self.close_row()
        elif tag in CELL_TAGS:
            if self.pop_to(CELL_TAGS, stop={"tr", "table"}) and self.rows:
                self.close_cell()
        elif tag == "a":
            if not self.pop_to({"a"}):
                return
            if not self.links:
                return
            href, buf, start = self.links.pop()
            if buf is not self.buf:
                return                     # ссылка «перешагнула» ячейку — оставляем текст
            inner = "".join(buf[start:])
            inner = re.sub(r"\s+", " ", STRUCT_RE.sub(" ", inner.replace("\n", " "))).strip()
            del buf[start:]
            if href and strip_marks(inner).strip() and "\x01+a" not in inner:
                buf.append(" " + open_tok("a", html.escape(href, quote=True)) + inner + close_tok("a") + " ")
            elif inner:
                buf.append(" " + inner + " ")
        else:
            self.pop_to({tag})

    def handle_data(self, data):
        if self.skip or self.hidden:
            return
        if self.pre:
            text = strip_invisible(data).replace("\r", "").replace("\t", "    ")
            self.emit(html.escape(text, quote=False))
            return
        if self.blocks and "\xa0" in data and not strip_invisible(data).strip():
            self.blocks[-1]["nbsp"] = True
        text = re.sub(r"\s+", " ", strip_invisible(data))
        if not text:
            return
        if text.strip():
            if self.prefix:
                self.emit(self.prefix)
                self.prefix = None
            if self.blocks:
                self.blocks[-1]["text"] = True
        self.emit(html.escape(text, quote=False))

    def result(self):
        while self.elems:
            self.pop_entry()
        while self.rows:
            self.close_row()
        while len(self.bufs) > 1:
            inner = self.bufs.pop()
            self.buf.extend(inner)
        if self.quote_rest:
            self.emit(Q_CLOSE)
        return "".join(self.bufs[0])


# ---------------------------------------------------------------- записи текста
#
# Текст письма — список записей (text, kind, quote):
#   kind:  "text" — строка, "para" — пустая строка, "sep" — разделитель таблицы,
#          "pre" — строка моноширинного блока
#   quote: False, "quote" (сворачиваемая цитата) или "fwd" (пересланное письмо)

def balance_line(line, active):
    """Делает из строки с метками корректный HTML: открывает начертания,
    действующие с прошлых строк, и закрывает всё в конце строки."""
    def open_html(name, href):
        return f'<a href="{href}">' if name == "a" else f"<{name}>"

    out = [open_html(n, h) for n, h in active]
    pos = 0
    for m in TOKEN_RE.finditer(line):
        out.append(line[pos:m.start()])
        pos = m.end()
        sign, name, href = m.group(1), m.group(2), m.group(3)
        if sign == "+":
            if name != "a" and any(n == name for n, _ in active):
                continue                    # уже действует — второй раз не открываем
            active.append((name, href))
            out.append(open_html(name, href))
        else:
            idx = next((i for i in range(len(active) - 1, -1, -1) if active[i][0] == name), None)
            if idx is None:
                continue
            above = active[idx + 1:]
            for n, _ in reversed(above):
                out.append(f"</{n}>")
            out.append(f"</{name}>")
            del active[idx:]
            for n, h in above:
                active.append((n, h))
                out.append(open_html(n, h))
    out.append(line[pos:])
    for n, _ in reversed(active):
        out.append(f"</{n}>")
    text = "".join(out)
    # пустые пары тегов и пробелы внутри краёв
    for _ in range(3):
        text = re.sub(r"<(b|i|u|s)>(\s*)</\1>", r"\2", text)
        text = re.sub(r'<a href="[^"]*">(\s*)</a>', r"\1", text)
    return text


def render_marked(marked):
    """Текст со служебными метками → список записей."""
    entries, active = [], []
    quote, pre = 0, 0
    for raw in marked.split("\n"):
        mark = raw.strip()
        if mark == "\x02P":
            entries.append(("", "para", bool(quote)))
            continue
        if mark == "\x02R":
            entries.append((ROW_SEP, "sep", bool(quote)))
            continue
        if mark in ("\x02Q+", "\x02Q-"):
            quote = quote + 1 if mark == "\x02Q+" else max(0, quote - 1)
            entries.append(("", "para", bool(quote)))
            continue
        if mark in ("\x02E+", "\x02E-"):
            pre = pre + 1 if mark == "\x02E+" else max(0, pre - 1)
            continue
        if pre:
            line = TOKEN_RE.sub("", raw).rstrip()
            entries.append((line, "pre", bool(quote)))
            continue
        kind = "text"
        stripped = raw.lstrip(" \t")
        if stripped.startswith(ROW_MARK):
            kind, raw = "trow", stripped[len(ROW_MARK):]
        elif stripped.startswith(CELL_MARK):
            kind, raw = "tcell", stripped[len(CELL_MARK):]
        line = re.sub(r"[ \t]+", " ", raw).strip(" \t")
        line = balance_line(line, active)
        line = re.sub(r"^((?:[ \t]*<[^>]+>)*)[ \t]+", r"\1", line)    # пробелы в начале
        line = re.sub(r"[ \t]+((?:</[^>]+>[ \t]*)*)$", r"\1", line)    # и в конце
        if not strip_marks(line).strip():
            continue
        entries.append((line, kind, bool(quote)))
    return entries


def normalize(entries):
    """Наводит порядок: схлопывает пустые строки, приклеивает огрызки вроде «₽»,
    помечает пересланные письма."""
    out = []
    for text, kind, quote in entries:
        if kind == "para":
            if out and out[-1][1] not in ("para", "sep") and not (out[-1][1] == "pre" and False):
                out.append(("", "para", quote))
            continue
        if kind == "sep":
            while out and out[-1][1] == "para":
                out.pop()
            if out and out[-1][1] == "sep":
                continue
            out.append((text, kind, quote))
            continue
        if kind == "text" and out and out[-1][1] == "text" and out[-1][2] == quote:
            vis = strip_marks(text).strip()
            prev_vis = strip_marks(out[-1][0]).strip()
            if (len(vis) <= 2 and "<a " not in text and not re.match(r"^[•◦▪\d]", vis)
                    and not re.match(r"^( )*([•◦▪]|\d+\.)$", prev_vis)):
                out[-1] = (out[-1][0] + " " + text, "text", quote)
                continue
        if out and out[-1][1] == "para" and out[-1][2] != quote:
            out[-1] = ("", "para", False)
        out.append((text, kind, quote))

    # убрать пустые строки и разделители по краям
    while out and out[0][1] in ("para", "sep"):
        out.pop(0)
    while out and out[-1][1] in ("para", "sep"):
        out.pop()
    # пустая строка прямо перед/после разделителя таблицы не нужна
    out = [e for i, e in enumerate(out)
           if not (e[1] == "para" and ((i + 1 < len(out) and out[i + 1][1] == "sep")
                                        or (i > 0 and out[i - 1][1] == "sep")))]

    # разделитель таблицы на границе с цитатой — лишний
    def neighbour(idx, step):
        idx += step
        while 0 <= idx < len(out) and out[idx][1] == "para":
            idx += step
        return out[idx] if 0 <= idx < len(out) else None
    out = [e for i, e in enumerate(out)
           if not (e[1] == "sep" and any(n is None or n[2] != e[2]
                                          for n in (neighbour(i, -1), neighbour(i, 1))))]

    # большие таблицы (не внутри цитаты) сворачиваем в цитату-«спойлер»
    table_kinds = ("trow", "tcell", "sep")
    i = 0
    while i < len(out):
        if out[i][1] not in table_kinds or out[i][2]:
            i += 1
            continue
        j = i
        while j < len(out) and out[j][1] in table_kinds and not out[j][2]:
            j += 1
        run = out[i:j]
        rows = sum(1 for e in run if e[1] == "trow") + max(0, sum(1 for e in run if e[1] == "sep") - 1)
        if rows > TABLE_COLLAPSE:
            while run and run[0][1] == "sep":
                run.pop(0)
            while run and run[-1][1] == "sep":
                run.pop()
            out[i:j] = [(t, k, "table") for t, k, _ in run]
            j = i + len(run)
        i = j

    # цитата, которая начинается как пересланное письмо, — не сворачиваем
    result, i = [], 0
    while i < len(out):
        if not out[i][2]:
            result.append((out[i][0], out[i][1], False))
            i += 1
            continue
        if out[i][2] == "table":
            result.append(out[i])
            i += 1
            continue
        j = i
        while j < len(out) and out[j][2] and out[j][2] != "table":
            j += 1
        run = out[i:j]
        head = " ".join(strip_marks(t) for t, k, _ in run[:8] if k == "text")
        mode = "fwd" if FWD_RE.search(head) else "quote"
        # пустые строки по краям цитаты выносим наружу
        while run and run[0][1] == "para":
            run.pop(0)
        while run and run[-1][1] == "para":
            run.pop()
        result.extend((t, k, mode) for t, k, _ in run)
        i = j
    return result


def parse_html(raw):
    """Возвращает (записи текста, есть ли в письме таблица с данными)."""
    parser = _HTMLToTelegram()
    parser.feed(raw)
    parser.close()
    return normalize(render_marked(parser.result())), parser.has_table


ATTRIBUTION_RE = re.compile(r"(wrote|писал|написал|пишет)[^:]*:\s*$|^\s*\d.*<[^>]+@[^>]+>.*:\s*$", re.I)


def plain_entries(raw):
    """Текст письма без HTML: абзацы по пустым строкам, «>» — цитата."""
    text = strip_invisible(raw.replace("\r", ""))
    entries = []
    for line in text.split("\n"):
        quote = False
        m = re.match(r"^\s*((?:>\s?)+)(.*)$", line)
        if m:
            quote, line = True, m.group(2)
        line = line.rstrip()
        if not line.strip():
            entries.append(("", "para", quote))
        else:
            entries.append((html.escape(line, quote=False), "text", quote))
    # строка «… писал(а):» перед цитатой — тоже часть цитаты
    for i, (t, k, q) in enumerate(entries):
        if k == "text" and not q and ATTRIBUTION_RE.search(html.unescape(t)):
            nxt = next((e for e in entries[i + 1:] if e[1] != "para"), None)
            if nxt and nxt[2]:
                entries[i] = (t, k, True)
    return normalize(entries)


def part_text(part):
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def get_body(msg):
    """Текст письма для Telegram.

    Возвращает (записи текста, исходный HTML или None, есть ли таблица с данными).
    """
    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        raw_html = part_text(html_part)
        entries, has_table = parse_html(raw_html)
        if entries:
            return entries, raw_html, has_table
    plain_part = msg.get_body(preferencelist=("plain",))
    if plain_part is not None:
        return plain_entries(part_text(plain_part)), None, False
    return [], None, False


# ---------------------------------------------------------------- деление на сообщения

def split_long_line(line, limit):
    """Очень длинную строку режет на куски (разметка в этом случае теряется)."""
    if visible_len(line) <= limit:
        return [line]
    plain = strip_marks(line)
    return [html.escape(plain[i:i + limit], quote=False) for i in range(0, len(plain), limit)]


def render_entries(entries):
    """Записи одной части → HTML для Telegram (цитаты и <pre> оборачиваются)."""
    blocks, i = [], 0
    while i < len(entries):
        quote = entries[i][2]
        j = i
        while j < len(entries) and entries[j][2] == quote:
            j += 1
        group = entries[i:j]
        i = j

        lines, k = [], 0
        while k < len(group):
            text, kind, _ = group[k]
            if kind == "pre" and not quote:
                run = []
                while k < len(group) and group[k][1] == "pre":
                    run.append(group[k][0])
                    k += 1
                lines.append("<pre>" + "\n".join(run).strip("\n") + "</pre>")
                continue
            lines.append("" if kind == "para" else text)
            k += 1
        body = "\n".join(lines).strip("\n")
        if not body:
            continue
        if quote in ("quote", "table"):
            body = f"<blockquote expandable>{body}</blockquote>"
        elif quote == "fwd":
            body = f"<blockquote>{body}</blockquote>"
        blocks.append(body)
    text = "\n".join(blocks)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_parts(header, entries):
    """Делит письмо на части, каждая влезает в одно сообщение Telegram."""
    parts, current = [], [(header, "text", False), ("", "para", False)]
    size, raw = visible_len(header) + 2, len(header) + 2
    for text, kind, quote in entries:
        pieces = split_long_line(text, PART_LIMIT - 200) if kind == "text" else [text]
        for piece in pieces:
            length = visible_len(piece) + 1
            # второе условие — подстраховка от огромных адресов ссылок
            if size + length > PART_LIMIT or raw + len(piece) + 40 > RAW_LIMIT:
                parts.append(current)
                current, size, raw = [], 0, 0
                if kind == "para":
                    continue
            current.append((piece, kind, quote))
            size += length
            raw += len(piece) + 40          # 40 — запас на теги цитат
    if current:
        parts.append(current)

    rendered = [render_entries(p) for p in parts]
    rendered = [p for p in rendered if p]
    if len(rendered) > MAX_PARTS:
        rendered = rendered[:MAX_PARTS]
        rendered[-1] += "\n…\n<i>[письмо очень длинное, продолжение — в почте]</i>"
    if len(rendered) > 1:
        rendered = [f"{p}\n\n<i>(часть {i} из {len(rendered)})</i>"
                    for i, p in enumerate(rendered, 1)]
    return rendered


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


def build_messages(msg, raw_msg, entries):
    return split_parts(build_header(msg, raw_msg), entries)


def to_plain(text):
    """Запасной вариант без разметки, если Telegram не принял HTML."""
    text = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2 (\1)", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))[:TG_TEXT_LIMIT]


def fallback_entries(msg):
    """Самый простой текст письма: plain-часть или HTML без тегов."""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        text = part_text(part) if part is not None else ""
        if part is not None and part.get_content_type() == "text/html":
            text = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", text)
            text = html.unescape(re.sub(r"<[^>]+>", "\n", text))
        return plain_entries(text)
    except Exception:
        return [("<i>Текст письма не удалось прочитать — откройте его в почте.</i>", "text", False)]


def forward(raw_bytes):
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    raw_msg = email.message_from_bytes(raw_bytes, policy=email.policy.compat32)
    try:
        entries, raw_html, has_table = get_body(msg)
    except Exception as err:
        # необычный HTML не должен ломать пересылку — отправляем упрощённый текст
        print(f"Не удалось разобрать письмо ({type(err).__name__}), отправляю упрощённо.")
        entries, raw_html, has_table = fallback_entries(msg), None, False
    reply_to = None
    for text in build_messages(msg, raw_msg, entries):
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

    if raw_html and (SCREENSHOTS == "always" or (SCREENSHOTS == "auto" and has_table)):
        send_screenshot(msg, raw_html, reply_to)

    for filename, content in get_attachments(msg):
        if len(content) > TG_FILE_LIMIT:
            send_text(
                f"📎 Файл «{html.escape(filename)}» больше 50 МБ — откройте его в почте.",
                reply_to=reply_to,
            )
        else:
            send_file(filename, content, reply_to=reply_to)
        time.sleep(1)


# ---------------------------------------------------------------- скриншот письма

SHOT_WIDTH = 680            # ширина «окна», px (письма обычно свёрстаны под 600 px)
SHOT_MAX_WIDTH = 1200       # шире не делаем, иначе на телефоне будет мелко
SHOT_SCALE = 2              # чёткость картинки
SHOT_CHUNK = 1800           # высота одного кадра, px (длинное письмо режется на кадры)
SHOT_MAX = 10               # не больше 10 картинок (лимит альбома Telegram)


def inline_cid_images(msg, raw_html):
    """Подставляет в HTML картинки, вложенные в само письмо (src="cid:...")."""
    import base64
    for part in msg.walk():
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        if not cid or part.get_content_maintype() != "image":
            continue
        data = part.get_payload(decode=True) or b""
        uri = f"data:{part.get_content_type()};base64,{base64.b64encode(data).decode()}"
        raw_html = raw_html.replace(f"cid:{cid}", uri)
    return raw_html


def png_size(png):
    """Ширина и высота PNG без сторонних библиотек."""
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def render_screenshots(page_html):
    """Открывает письмо в браузере и возвращает список PNG-кадров."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome")   # Chrome, предустановленный в GitHub
        except Exception:
            browser = p.chromium.launch()                   # Chromium от Playwright, если есть
        try:
            context = browser.new_context(
                viewport={"width": SHOT_WIDTH, "height": 100},   # высота подстроится под письмо
                device_scale_factor=SHOT_SCALE,
                java_script_enabled=False,                    # скрипты из писем не выполняем
            )
            page = context.new_page()
            try:
                page.set_content(page_html, wait_until="load", timeout=20000)
            except Exception:
                pass                                          # часть картинок не успела — не страшно

            # Письмо шире окна (например, iCloud) — расширяем окно под письмо,
            # иначе правая часть обрезается.
            try:
                content_width = page.evaluate(
                    "Math.max(document.documentElement.scrollWidth,"
                    " document.body ? document.body.scrollWidth : 0)")
            except Exception:
                content_width = SHOT_WIDTH
            width = min(max(int(content_width or 0), SHOT_WIDTH), SHOT_MAX_WIDTH)
            if width != SHOT_WIDTH:
                page.set_viewport_size({"width": width, "height": 100})
                page.wait_for_timeout(300)

            full = page.screenshot(full_page=True)
            width_px, height_px = png_size(full)
            width = width_px // SHOT_SCALE
            height = height_px // SHOT_SCALE
            if height <= SHOT_CHUNK * 1.3:
                return [full]
            shots = []
            for top in range(0, height, SHOT_CHUNK):
                if len(shots) >= SHOT_MAX:
                    break
                clip = {"x": 0, "y": top, "width": width,
                        "height": min(SHOT_CHUNK, height - top)}
                shots.append(page.screenshot(full_page=True, clip=clip))
            return shots
        finally:
            browser.close()


def send_screenshot(msg, raw_html, reply_to):
    """Отправляет картинку письма ответом на пост. Ошибки не мешают пересылке."""
    try:
        page_html = inline_cid_images(msg, raw_html)
        if "<meta charset" not in page_html.lower():
            page_html = '<meta charset="utf-8">' + page_html
        shots = render_screenshots(page_html)
    except Exception as err:
        print(f"Скриншот не получился: {type(err).__name__}")
        return

    caption = "🖼 Так письмо выглядит в почте"
    if len(shots) == 1:
        data = {"chat_id": TG_CHAT_ID, "caption": caption}
        if reply_to:
            data["reply_to_message_id"] = reply_to
        try:
            tg_call("sendPhoto", data, files={"photo": ("letter.png", shots[0])})
        except RuntimeError:
            # слишком вытянутая картинка — отправляем файлом
            tg_call("sendDocument", data, files={"document": ("letter.png", shots[0])})
    else:
        media, files = [], {}
        for i, shot in enumerate(shots):
            name = f"shot{i}"
            item = {"type": "photo", "media": f"attach://{name}"}
            if i == 0:
                item["caption"] = f"{caption} ({len(shots)} фрагм.)"
            media.append(item)
            files[name] = (f"{name}.png", shot)
        data = {"chat_id": TG_CHAT_ID, "media": json.dumps(media)}
        if reply_to:
            data["reply_to_message_id"] = reply_to
        tg_call("sendMediaGroup", data, files=files)
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
                try:
                    forward(raw)
                    sent += 1
                except requests.RequestException:
                    raise                   # нет связи с Telegram — попробуем в следующий запуск
                except Exception as err:
                    # одно «сломанное» письмо не должно останавливать бота навсегда
                    print(f"Письмо не переслано: {type(err).__name__}: {str(err)[:200]}")
                    try:
                        send_text("⚠️ Одно письмо не удалось переслать — откройте почту.")
                    except Exception:
                        pass
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
