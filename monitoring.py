"""Уведомления владельцу и сигнал внешнему мониторингу после завершения workflow."""

import json
import os
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen


def run_link():
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return f"{server}/{repository}/actions/runs/{run_id}" if repository and run_id else ""


def notify_admin(text):
    token = os.environ.get("TG_TOKEN", "")
    chat_id = os.environ.get("TG_ADMIN_CHAT_ID", "")
    if not token or not chat_id:
        print("Личные уведомления не настроены: нужны TG_TOKEN и TG_ADMIN_CHAT_ID.")
        return True
    link = run_link()
    payload = {"chat_id": chat_id, "text": text + (f"\n{link}" if link else ""),
               "disable_web_page_preview": True}
    request = Request(f"https://api.telegram.org/bot{token}/sendMessage",
                      data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            if not json.load(response).get("ok"):
                raise ValueError("Telegram rejected notification")
    except Exception as err:
        # Исключение urllib может содержать URL с токеном: не выводим его текст.
        print(f"Не удалось уведомить владельца: {type(err).__name__}.")
        return False
    return True


def read_state(path):
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(state, dict) or type(state.get("last_uid")) is not int:
        raise ValueError("Invalid state")
    return state


def delivery_scope(since):
    if not since:
        return "live"
    for date_format in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            date = datetime.strptime(since, date_format)
            months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            return f"history:{date.day:02d}-{months[date.month - 1]}-{date.year}"
        except ValueError:
            pass
    raise ValueError("Invalid history date")


def exhausted_uids(state, scope):
    return {uid for uid, failure in state.get("failed", {}).get(scope, {}).items()
            if failure["attempts"] >= 5}


def heartbeat():
    url = os.environ.get("HEALTHCHECK_URL", "").strip()
    if not url:
        print("Внешний контроль запусков не настроен: HEALTHCHECK_URL пуст.")
        return True
    try:
        if not url.startswith("https://"):
            raise ValueError("HTTPS required")
        with urlopen(Request(url, data=b"", method="POST"), timeout=15) as response:
            if response.status != 200 or response.read(100).strip() != b"OK":
                raise ValueError("Heartbeat rejected")
    except Exception as err:
        print(f"Не удалось отправить сигнал мониторингу: {type(err).__name__}.")
        return False
    return True


def report():
    status = os.environ.get("WORKFLOW_STATUS", "failure")
    if status != "success":
        return notify_admin("⚠️ mail2tg: запуск завершился с ошибкой или был отменён. "
                            "Проверьте шаги доставки и сохранения состояния.")
    since = os.environ.get("BACKFILL_SINCE", "").strip()
    try:
        current = read_state(os.environ.get("STATE_PATH", "state.json"))
        scope = delivery_scope(since)
        exhausted = exhausted_uids(current, scope)
        try:
            before = read_state(os.environ.get("STATE_BEFORE_PATH", ""))
            # При новой нумерации старые UID нельзя сравнивать с текущими.
            previous = (exhausted_uids(before, scope)
                        if before.get("uidvalidity") == current.get("uidvalidity") else set())
            # Явный ручной повтор начинает новый цикл попыток.
            reset = {str(int(uid.strip())) for uid in
                     os.environ.get("RETRY_FAILED_UIDS", "").split(",") if uid.strip()}
            previous -= reset
        except (OSError, ValueError, KeyError, TypeError):
            previous = set()
        new_errors = exhausted - previous
        live_failures = bool(current.get("failed", {}).get("live", {}))
    except Exception as err:
        print(f"Не удалось проверить результат доставки: {type(err).__name__}.")
        notify_admin("⚠️ mail2tg: не удалось проверить состояние после запуска. Проверьте логи.")
        return False
    if new_errors:
        uids = ", ".join(sorted(new_errors, key=int)[:20])
        mode = "история" if since else "новые письма"
        if not notify_admin(f"⚠️ mail2tg: исчерпаны попытки доставки ({mode}). "
                            f"Требуется ручной повтор. UID: {uids}."):
            return False
    # Ручная история не должна скрывать остановку обычной проверки почты.
    if not since and not live_failures:
        if not heartbeat():
            notify_admin("⚠️ mail2tg: проверка почты завершена, но сигнал внешнему мониторингу не доставлен.")
            return False
    elif not since:
        print("Сигнал успеха не отправлен: в очереди новых писем есть ошибки.")
    return True


if __name__ == "__main__":
    raise SystemExit(0 if report() else 1)
