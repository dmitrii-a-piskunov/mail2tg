"""Продолжение частично отправленного письма без настоящего Telegram."""

import ast
import copy
import email
import email.policy
import hashlib
import html
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


class DeliveryProgressTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "mail2tg.py").read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in {"forward", "send_attachments", "send_screenshot"}]
        self.ns = {
            "email": email, "hashlib": hashlib, "json": json, "html": html,
            "TG_CHAT_ID": "test-chat", "TG_FILE_LIMIT": 50, "ALBUM_MAX": 10,
            "SCREENSHOTS": "always", "time": SimpleNamespace(sleep=Mock()), "print": Mock(),
            "is_excluded_sender": Mock(return_value=False),
            "get_body": Mock(return_value=([], "<p>test</p>", False)),
            "build_messages": Mock(return_value=["part1", "part2"]),
            "get_attachments": Mock(return_value=[("a", b"a"), ("b", b"b")]),
            "send_text": Mock(return_value={"message_id": 42}),
            "send_file": Mock(return_value={"message_id": 43}),
            "tg_call": Mock(return_value=[{"message_id": 44}]),
            "inline_cid_images": Mock(return_value="<p>test</p>"),
            "render_screenshots": Mock(return_value=[b"png"]),
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "mail2tg.py", "exec"), self.ns)
        self.raw = b"From: teacher@example.test\r\n\r\nTest"
        self.progress = {}
        self.snapshots = []

    def checkpoint(self):
        self.snapshots.append(copy.deepcopy(self.progress))

    def deliver(self):
        return self.ns["forward"](self.raw, self.progress, self.checkpoint)

    def test_second_text_failure_resumes_with_reply(self):
        self.ns["send_text"].side_effect = [{"message_id": 42}, ConnectionError()]
        with self.assertRaises(ConnectionError):
            self.deliver()
        self.progress = copy.deepcopy(self.snapshots[-1])
        self.ns["send_text"].side_effect = None
        self.ns["send_text"].reset_mock()
        self.deliver()
        self.ns["send_text"].assert_called_once_with("part2", reply_to=42)

    def test_partial_attachment_fallback_resumes_remaining_file(self):
        self.ns["tg_call"].side_effect = RuntimeError("album rejected")
        self.ns["send_file"].side_effect = [{"message_id": 43}, ConnectionError()]
        with self.assertRaises(ConnectionError):
            self.deliver()
        self.assertEqual(self.progress["attachments"], [0])
        self.progress = copy.deepcopy(self.snapshots[-1])
        self.ns["tg_call"].side_effect = None
        self.ns["send_file"].side_effect = None
        self.ns["send_file"].reset_mock()
        self.ns["send_text"].reset_mock()
        self.deliver()
        self.ns["send_file"].assert_called_once_with("b", b"b", reply_to=42)
        self.ns["send_text"].assert_not_called()

    def test_screenshot_network_failure_does_not_repeat_text_or_attachments(self):
        def api(method, *args, **kwargs):
            if method == "sendPhoto":
                raise ConnectionError()
            return []
        self.ns["tg_call"].side_effect = api
        with self.assertRaises(ConnectionError):
            self.deliver()
        self.ns["tg_call"].side_effect = None
        self.ns["tg_call"].reset_mock()
        self.ns["send_text"].reset_mock()
        self.deliver()
        self.ns["send_text"].assert_not_called()
        self.assertEqual([c.args[0] for c in self.ns["tg_call"].call_args_list], ["sendPhoto"])

    def test_render_failure_is_optional_and_recorded(self):
        self.ns["render_screenshots"].side_effect = RuntimeError()
        self.assertTrue(self.deliver())
        self.assertEqual(self.progress["screenshot"], "render_failed")
        self.assertTrue(self.progress["complete"])

    def test_completed_delivery_is_not_repeated(self):
        self.deliver()
        self.ns["send_text"].reset_mock()
        self.ns["tg_call"].reset_mock()
        self.deliver()
        self.ns["send_text"].assert_not_called()
        self.ns["tg_call"].assert_not_called()

    def test_changed_plan_stops_before_sending(self):
        self.deliver()
        self.ns["build_messages"].return_value = ["changed"]
        self.ns["send_text"].reset_mock()
        with self.assertRaisesRegex(RuntimeError, "DeliveryPlanChanged"):
            self.deliver()
        self.ns["send_text"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
