"""Проверки уведомлений без отправки HTTP-запросов."""

import importlib.util
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location(
    "mail2tg_monitoring", Path(__file__).resolve().parents[1] / "monitoring.py")
monitoring = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitoring)


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"WORKFLOW_STATUS": "success", "TG_TOKEN": "secret",
                              "TG_ADMIN_CHAT_ID": "123", "STATE_BEFORE_PATH": "before.json"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.current = {"uidvalidity": 1, "last_uid": 19, "failed": {"live": {}}}
        self.before = {"uidvalidity": 1, "last_uid": 18}
        self.reader = patch.object(monitoring, "read_state",
                                   side_effect=lambda path: self.before if path == "before.json" else self.current)
        self.reader.start()
        self.addCleanup(self.reader.stop)
        self.notice = patch.object(monitoring, "notify_admin", return_value=True)
        self.notify = self.notice.start()
        self.addCleanup(self.notice.stop)
        self.pinger = patch.object(monitoring, "heartbeat", return_value=True)
        self.ping = self.pinger.start()
        self.addCleanup(self.pinger.stop)

    def test_clean_run_sends_only_heartbeat(self):
        self.assertTrue(monitoring.report())
        self.ping.assert_called_once()
        self.notify.assert_not_called()

    def test_failed_workflow_sends_notice_without_heartbeat(self):
        os.environ["WORKFLOW_STATUS"] = "failure"
        self.assertTrue(monitoring.report())
        self.notify.assert_called_once()
        self.ping.assert_not_called()

    def test_new_exhaustion_notifies_once(self):
        self.current["failed"]["live"]["19"] = {"attempts": 5}
        monitoring.report()
        self.notify.assert_called_once()
        self.ping.assert_not_called()
        self.before = self.current
        self.notify.reset_mock()
        monitoring.report()
        self.notify.assert_not_called()

    def test_pending_failure_suppresses_heartbeat(self):
        self.current["failed"]["live"]["19"] = {"attempts": 1}
        monitoring.report()
        self.ping.assert_not_called()
        self.notify.assert_not_called()

    def test_history_cannot_mask_live_outage(self):
        os.environ["BACKFILL_SINCE"] = "01.09.2026"
        self.current["failed"]["history:01-Sep-2026"] = {"10": {"attempts": 5}}
        monitoring.report()
        self.notify.assert_called_once()
        self.ping.assert_not_called()

    def test_invalid_state_notifies_and_fails(self):
        with patch.object(monitoring, "read_state", side_effect=ValueError("private")):
            self.assertFalse(monitoring.report())
        self.notify.assert_called_once()
        self.ping.assert_not_called()

    def test_ping_failure_notifies(self):
        self.ping.return_value = False
        self.assertFalse(monitoring.report())
        self.notify.assert_called_once()

    def test_admin_message_uses_private_chat_and_run_link(self):
        self.notice.stop()
        os.environ.update(GITHUB_REPOSITORY="owner/repo", GITHUB_RUN_ID="42")
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"ok":true}')
        with patch.object(monitoring, "urlopen", return_value=response) as request:
            self.assertTrue(monitoring.notify_admin("failure"))
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload["chat_id"], "123")
        self.assertIn("https://github.com/owner/repo/actions/runs/42", payload["text"])
        self.assertNotIn("secret", payload["text"])

    def test_notification_failure_does_not_leak_token(self):
        self.notice.stop()
        with patch.object(monitoring, "urlopen", side_effect=OSError("secret URL")), \
                patch("builtins.print") as output:
            self.assertFalse(monitoring.notify_admin("failure"))
        self.assertNotIn("secret", str(output.call_args_list))

    def test_heartbeat_requires_positive_acknowledgement(self):
        self.pinger.stop()
        os.environ["HEALTHCHECK_URL"] = "https://hc-ping.com/example"
        response = MagicMock()
        response.status = 200
        response.read.return_value = b"OK (not found)"
        response.__enter__.return_value = response
        with patch.object(monitoring, "urlopen", return_value=response):
            self.assertFalse(monitoring.heartbeat())
            response.read.return_value = b"OK"
            self.assertTrue(monitoring.heartbeat())


if __name__ == "__main__":
    unittest.main()
