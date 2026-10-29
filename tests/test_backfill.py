"""Изолированные проверки цикла доставки без секретов, зависимостей и сети."""

import ast
import copy
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


class BackfillTests(unittest.TestCase):
    def setUp(self):
        # Загружаем проверяемые функции без настройки окружения при импорте.
        source = Path(__file__).resolve().parents[1] / "mail2tg.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name in {"main", "parse_date", "deliver_uid"}]
        self.state = {"uidvalidity": 1, "last_uid": 18}
        self.imap = Mock()
        self.imap.select.return_value = ("OK", [b"20"])
        self.imap.uid.side_effect = self.uid
        self.searches = []
        self.saved = []
        self.ns = {
            "re": re, "sys": sys,
            "IMAP_MONTHS": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
            "IMAP_HOST": "example.test", "IMAP_USER": "test", "IMAP_PASSWORD": "test",
            "IMAP_FOLDER": "INBOX", "MAX_PER_RUN": 2, "BACKFILL_SINCE": "2026-09-01",
            "MAX_DELIVERY_ATTEMPTS": 5, "RETRY_FAILED_UIDS": "",
            "load_state": lambda: copy.deepcopy(self.state),
            "save_state": self.save,
            "imaplib": SimpleNamespace(IMAP4_SSL=Mock(return_value=self.imap)),
            "folder_status": Mock(return_value=(1, 20)),
            "forward": Mock(return_value=True), "send_text": Mock(),
            "time": SimpleNamespace(sleep=Mock()),
            "requests": SimpleNamespace(RequestException=ConnectionError),
            "print": Mock(),
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), self.ns)

    def save(self, state):
        self.state = copy.deepcopy(state)
        self.saved.append(copy.deepcopy(state))

    def uid(self, command, *args):
        if command == "search":
            self.searches.append(args)
            low, high = map(int, args[1].removeprefix("UID ").split(":"))
            # Письма с UID 10 и выше относятся к выбранной дате.
            return "OK", [" ".join(str(u) for u in range(max(low, 10), high + 1)).encode()]
        return "OK", [(b"header", args[0].encode())]

    def run_delivery(self, since):
        self.ns["BACKFILL_SINCE"] = since
        self.ns["forward"].reset_mock()
        self.ns["main"]()
        return [call.args[0] for call in self.ns["forward"].call_args_list]

    def test_history_then_cron_then_history(self):
        self.assertEqual(self.run_delivery("2026-09-01"), [b"10", b"11"])
        self.assertTrue(all(s["last_uid"] == 18 for s in self.saved))
        self.assertEqual(self.state["backfill"]["end_uid"], 18)
        self.assertEqual(self.run_delivery(""), [b"19"])
        self.assertEqual(self.state["last_uid"], 19)
        self.assertEqual(self.run_delivery("01.09.2026"), [b"12", b"13"])
        self.assertEqual(self.state["backfill"]["end_uid"], 18)
        self.assertEqual(self.state["last_uid"], 19)

    def test_completed_history_is_not_repeated(self):
        self.ns["MAX_PER_RUN"] = 20
        self.assertEqual(len(self.run_delivery("2026-09-01")), 9)
        self.assertEqual(self.run_delivery("2026-09-01"), [])
        self.assertEqual(self.state["last_uid"], 18)

    def test_different_date_starts_new_history(self):
        self.run_delivery("2026-09-01")
        self.assertEqual(self.run_delivery("2026-09-02"), [b"10", b"11"])
        self.assertEqual(self.state["last_uid"], 18)

    def test_empty_history_preserves_live_cursor(self):
        self.imap.uid.side_effect = None
        self.imap.uid.return_value = ("OK", [b""])
        self.assertEqual(self.run_delivery("2026-09-01"), [])
        self.assertEqual(self.state["last_uid"], 18)
        self.assertEqual(self.state["backfill"]["last_uid"], 18)

    def test_network_failure_resumes_history(self):
        self.ns["forward"].side_effect = [True, ConnectionError("offline")]
        self.run_delivery("2026-09-01")
        self.assertEqual(self.state["last_uid"], 18)
        self.assertEqual(self.state["backfill"]["last_uid"], 11)
        self.assertEqual(self.state["failed"]["history:01-Sep-2026"]["11"]["attempts"], 1)
        self.ns["forward"].side_effect = None
        self.assertEqual(self.run_delivery("2026-09-01"), [b"11", b"12"])

    def test_live_failure_does_not_block_new_mail(self):
        self.ns["folder_status"].return_value = (1, 22)
        self.ns["forward"].side_effect = [ConnectionError("private text"), True]
        self.assertEqual(self.run_delivery(""), [b"19", b"20"])
        self.assertEqual(self.state["last_uid"], 20)
        self.assertEqual(self.state["failed"]["live"]["19"],
                         {"attempts": 1, "error": "ConnectionError"})
        self.ns["forward"].side_effect = None
        self.assertEqual(self.run_delivery(""), [b"19", b"21"])
        self.assertEqual(self.state["failed"]["live"], {})

    def test_exhausted_failure_can_be_retried_manually(self):
        self.state["last_uid"] = 19
        self.state["failed"] = {"live": {"19": {"attempts": 5, "error": "RuntimeError"}}}
        self.assertEqual(self.run_delivery(""), [])
        self.assertIn("19", self.state["failed"]["live"])
        self.ns["RETRY_FAILED_UIDS"] = "19"
        self.assertEqual(self.run_delivery(""), [b"19"])
        self.assertEqual(self.state["failed"]["live"], {})

    def test_empty_fetch_is_queued(self):
        original = self.uid
        self.imap.uid.side_effect = lambda command, *args: (
            ("OK", [b")"]) if command == "fetch" else original(command, *args))
        self.assertEqual(self.run_delivery(""), [])
        self.assertEqual(self.state["failed"]["live"]["19"]["attempts"], 1)

    def test_failed_fetch_is_queued_even_with_body(self):
        original = self.uid
        self.imap.uid.side_effect = lambda command, *args: (
            ("NO", [(b"header", b"body")]) if command == "fetch" else original(command, *args))
        self.assertEqual(self.run_delivery(""), [])
        self.assertIn("19", self.state["failed"]["live"])

    def test_five_failures_stop_automatic_retries(self):
        self.ns["forward"].side_effect = ConnectionError("secret")
        for _ in range(5):
            self.run_delivery("")
        self.assertEqual(self.state["failed"]["live"]["19"]["attempts"], 5)
        self.assertEqual(self.run_delivery(""), [])
        self.assertNotIn("secret", repr(self.state))
        self.assertNotIn("secret", repr(self.ns["print"].call_args_list))

    def test_excluded_sender_clears_failure(self):
        self.state["last_uid"] = 19
        self.state["failed"] = {"live": {"19": {"attempts": 1, "error": "ConnectionError"}}}
        self.ns["forward"].return_value = False
        self.run_delivery("")
        self.assertEqual(self.state["failed"]["live"], {})

    def test_changing_history_date_preserves_old_failures(self):
        self.ns["forward"].side_effect = ConnectionError("offline")
        self.run_delivery("2026-09-01")
        self.ns["forward"].side_effect = None
        self.run_delivery("2026-09-02")
        self.assertIn("10", self.state["failed"]["history:01-Sep-2026"])

    def test_select_error_preserves_cursor(self):
        self.imap.select.return_value = ("NO", [b"private"])
        with self.assertRaises(RuntimeError):
            self.run_delivery("")
        self.assertEqual(self.state["last_uid"], 18)
        self.assertFalse(self.ns["forward"].called)

    def test_search_error_preserves_cursor(self):
        self.imap.uid.side_effect = None
        self.imap.uid.return_value = ("NO", [b"private"])
        with self.assertRaises(RuntimeError):
            self.run_delivery("")
        self.assertEqual(self.state["last_uid"], 18)

    def test_history_failures_are_not_retried_by_cron(self):
        self.ns["forward"].side_effect = ConnectionError("offline")
        self.run_delivery("2026-09-01")
        self.ns["forward"].side_effect = None
        self.assertEqual(self.run_delivery(""), [b"19"])
        self.assertIn("10", self.state["failed"]["history:01-Sep-2026"])

    def test_uidvalidity_change_clears_old_history(self):
        self.state["backfill"] = {"since": "01-Sep-2026", "last_uid": 100, "end_uid": 200}
        self.ns["folder_status"].return_value = (2, 20)
        self.assertEqual(self.run_delivery("2026-09-01"), [b"10", b"11"])
        self.assertEqual(self.state["uidvalidity"], 2)
        self.assertEqual(self.state["last_uid"], 19)
        self.assertEqual(self.state["backfill"]["end_uid"], 19)

    def test_first_regular_run_does_not_forward_old_mail(self):
        self.state = {}
        self.assertEqual(self.run_delivery(""), [])
        self.assertEqual(self.state, {"uidvalidity": 1, "last_uid": 19})


if __name__ == "__main__":
    unittest.main()
