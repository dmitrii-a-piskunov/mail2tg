"""Ограничения скриншотов и необязательная проверка настоящим браузером."""

import ast
import email
import os
import threading
import unittest
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock


def screenshot_functions():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "mail2tg.py").read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if
             (isinstance(node, ast.FunctionDef) and node.name in
              {"render_screenshots", "inline_cid_images", "png_size"}) or
             (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name)
              and t.id.startswith("SHOT_") for t in node.targets))]
    namespace = {"print": Mock()}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "mail2tg.py", "exec"), namespace)
    return namespace


class ScreenshotTests(unittest.TestCase):
    def test_html_limit_is_checked_before_browser_launch(self):
        ns = screenshot_functions()
        ns["SHOT_HTML_LIMIT"] = 10
        with self.assertRaisesRegex(ValueError, "ScreenshotHTMLTooLarge"):
            ns["render_screenshots"]("я" * 6)

    def test_cid_image_is_embedded(self):
        ns = screenshot_functions()
        msg = EmailMessage()
        msg.set_content("test")
        msg.add_attachment(b"image-data", maintype="image", subtype="png", cid="<test-image>")
        result = ns["inline_cid_images"](msg, '<img src="cid:test-image">')
        self.assertIn("data:image/png;base64,", result)
        self.assertNotIn("cid:", result)


@unittest.skipUnless(os.environ.get("MAIL2TG_BROWSER_TESTS") == "1", "Browser tests are opt-in")
class BrowserScreenshotTests(unittest.TestCase):
    def test_network_is_blocked_and_long_page_is_bounded(self):
        hits = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"test")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            ns = screenshot_functions()
            ns["SHOT_CHUNK"], ns["SHOT_MAX"] = 300, 2
            url = f"http://127.0.0.1:{server.server_port}"
            document = (f'<link rel="stylesheet" href="{url}/style.css">'
                        f'<img src="{url}/tracker.png"><iframe src="{url}/frame"></iframe>'
                        '<img src="data:image/png;base64,'
                        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=">'
                        '<div style="height:100000px;width:100000px">Lesson</div>')
            shots = ns["render_screenshots"](document)
            self.assertEqual(hits, [], "Browser contacted a blocked resource")
            self.assertEqual(len(shots), 2)
            for shot in shots:
                width, height = ns["png_size"](shot)
                self.assertLessEqual(width, ns["SHOT_MAX_WIDTH"] * ns["SHOT_SCALE"])
                self.assertLessEqual(height, 300 * ns["SHOT_SCALE"])
                self.assertGreater(height, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
