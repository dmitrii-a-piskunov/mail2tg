"""Проверки состояния на временных файлах без сети и секретов."""

import ast
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class StateTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / "mail2tg.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
                 and n.name in {"StateError", "validate_state", "load_state", "save_state"}]
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.ns = {"json": json, "os": os, "re": re, "tempfile": tempfile,
                   "Path": Path, "STATE_FILE": self.path}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), self.ns)
        self.error = self.ns["StateError"]
        self.state = {"uidvalidity": 1, "last_uid": 19}

    def test_legacy_state_round_trip(self):
        self.ns["save_state"](self.state)
        self.assertEqual(self.ns["load_state"](), self.state)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_missing_state_stops_instead_of_resetting(self):
        with self.assertRaises(self.error):
            self.ns["load_state"]()

    def test_bad_json_and_encoding_stop_without_echoing_content(self):
        for data in (b'{"private":', b'\xff'):
            self.path.write_bytes(data)
            with self.assertRaises(self.error) as caught:
                self.ns["load_state"]()
            self.assertNotIn("private", str(caught.exception))
            self.assertEqual(self.path.read_bytes(), data)

    def test_invalid_structures_rejected(self):
        invalid = [[], None, {"last_uid": 1}, {"uidvalidity": True, "last_uid": 1},
                   {"uidvalidity": 1, "last_uid": -1},
                   dict(self.state, failed={"live": {"19": {"attempts": "1", "error": "x"}}}),
                   dict(self.state, deliveries={"live": {"19": {"text_parts": 1}}}),
                   dict(self.state, deliveries={"live": {"19": {"attachments": [0, 0]}}}),
                   dict(self.state, backfill={"since": "01-Sep-2026", "last_uid": 20, "end_uid": 18})]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(self.error):
                self.ns["validate_state"](value)

    def test_explicit_empty_state_allows_first_initialization(self):
        self.ns["save_state"]({})
        self.assertEqual(self.ns["load_state"](), {})

    def test_progress_and_queues_round_trip(self):
        self.state.update(
            failed={"live": {"19": {"attempts": 1, "error": "ConnectionError"}}},
            deliveries={"live": {"19": {"fingerprint": "a" * 64, "reply_to": 42,
                                         "text_parts": 1, "attachments": [0]}}},
            backfill={"since": "01-Sep-2026", "last_uid": 10, "end_uid": 18})
        self.ns["save_state"](self.state)
        self.assertEqual(self.ns["load_state"](), self.state)

    def test_replace_failure_preserves_original_and_cleans_temp(self):
        self.ns["save_state"](self.state)
        original = self.path.read_bytes()
        with patch.object(os, "replace", side_effect=OSError("private path")):
            with self.assertRaises(self.error):
                self.ns["save_state"](dict(self.state, last_uid=20))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_flush_failure_preserves_original(self):
        self.ns["save_state"](self.state)
        original = self.path.read_bytes()
        with patch.object(os, "fsync", side_effect=OSError()):
            with self.assertRaises(self.error):
                self.ns["save_state"](dict(self.state, last_uid=20))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
