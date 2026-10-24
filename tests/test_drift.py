"""Tests for drift-cli. Fixture pages are served by a local
http.server on an ephemeral port; the page bodies are mutated between
watch and check to plant changes. Run: python3 -m unittest discover tests
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DRIFT_PY = os.path.join(HERE, "..", "drift.py")
spec = importlib.util.spec_from_file_location("drift", DRIFT_PY)
drift = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drift)

PAGES = {}

BASE = """<!doctype html><html><head><title>Models</title>
<script>var x = 1;</script><style>.a{color:red}</style></head>
<body>
<nav>Home Pricing Docs</nav>
<main>
<h1>Available models</h1>
<ul>
<li>gpt-alpha</li>
<li>gpt-beta</li>
</ul>
<p>Pro plan: $20 / month</p>
<p>Last sync 3 minutes ago</p>
<p>Build 2026-10-24T10:11:12Z sid=9f8e7d6c5b4a39281706f5e4d3c2b1a0</p>
</main>
<footer>Copyright nobody</footer>
</body></html>"""

SECRET_V1 = BASE.replace(
    "</main>", "<p>demo key sk-TESTSECRET1234567890</p></main>")
SECRET_V2 = BASE.replace(
    "</main>", "<p>demo key sk-OTHERSECRET0987654321</p></main>")

NOISE_V2 = (BASE
            .replace("3 minutes ago", "7 minutes ago")
            .replace("2026-10-24T10:11:12Z", "2026-10-24T18:45:59Z")
            .replace("9f8e7d6c5b4a39281706f5e4d3c2b1a0",
                     "0123456789abcdef0123456789abcdef"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class DriftTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever,
                         daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.get("DRIFT_HOME")
        os.environ["DRIFT_HOME"] = os.path.join(self.tmp.name, "store")
        self.url = f"http://127.0.0.1:{self.port}/page"
        PAGES["/page"] = BASE
        self.report = os.path.join(self.tmp.name, "drift-report.md")

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop("DRIFT_HOME", None)
        else:
            os.environ["DRIFT_HOME"] = self.old_env
        self.tmp.cleanup()

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = drift.main(argv)
        return code, out.getvalue(), err.getvalue()

    def watch(self):
        code, out, _ = self.run_cli(["watch", self.url])
        self.assertEqual(code, 0, out)
        return out

    def read_report(self):
        with open(self.report) as f:
            return f.read()

    # 1 ------------------------------------------------ unchanged
    def test_unchanged_no_change(self):
        self.watch()
        code, out, _ = self.run_cli(["check", self.url, "--out",
                                     self.report])
        self.assertEqual(code, 0, out)
        report = self.read_report()
        self.assertIn("NO CHANGE", report)
        self.assertIn("No changes.", report)

    # 2 ------------------------------------------- planted addition
    def test_added_line_detected(self):
        self.watch()
        PAGES["/page"] = BASE.replace(
            "<li>gpt-beta</li>",
            "<li>gpt-beta</li>\n<li>gpt-gamma</li>")
        code, out, _ = self.run_cli(["check", self.url, "--out",
                                     self.report])
        self.assertEqual(code, 1, out)
        report = self.read_report()
        self.assertIn("CHANGED", report)
        added_section = report.split("### Added")[1]
        self.assertIn("gpt-gamma", added_section)

    # 3 -------------------------------------- planted line change
    def test_price_change_is_changed_pair(self):
        self.watch()
        PAGES["/page"] = BASE.replace("$20", "$25")
        code, out, _ = self.run_cli(["check", self.url, "--out",
                                     self.report])
        self.assertEqual(code, 1, out)
        report = self.read_report()
        changed_section = report.split("### Changed")[1]
        self.assertIn("- Pro plan: $20 / month", changed_section)
        self.assertIn("+ Pro plan: $25 / month", changed_section)

    # 4 --------------------------------------------- noise filtered
    def test_noise_only_change_is_no_change(self):
        self.watch()
        PAGES["/page"] = NOISE_V2
        code, out, _ = self.run_cli(["check", self.url, "--out",
                                     self.report])
        self.assertEqual(code, 0, out)
        self.assertIn("NO CHANGE", self.read_report())

    # 5 -------------------------------------------- secret redaction
    def test_secret_never_reaches_report_or_snapshot(self):
        PAGES["/page"] = SECRET_V1
        self.watch()
        PAGES["/page"] = SECRET_V2
        self.run_cli(["check", self.url, "--out", self.report])
        report = self.read_report()
        self.assertNotIn("TESTSECRET", report)
        self.assertNotIn("OTHERSECRET", report)
        store = os.environ["DRIFT_HOME"]
        for name in os.listdir(store):
            with open(os.path.join(store, name)) as f:
                self.assertNotIn("TESTSECRET", f.read())

    # 6 ------------------------------------------------- list/forget
    def test_list_and_forget(self):
        self.watch()
        code, out, _ = self.run_cli(["list"])
        self.assertEqual(code, 0)
        self.assertIn(self.url, out)
        code, _, _ = self.run_cli(["forget", self.url])
        self.assertEqual(code, 0)
        code, _, err = self.run_cli(["check", self.url, "--out",
                                     self.report])
        self.assertEqual(code, 2)  # not watched anymore -> error

    # 7 --------------------------------------------------- MCP smoke
    def test_mcp_smoke(self):
        env = dict(os.environ)
        env["DRIFT_HOME"] = os.environ["DRIFT_HOME"]
        proc = subprocess.Popen(
            [sys.executable, DRIFT_PY, "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, env=env)
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "drift_watch",
                        "arguments": {"url": self.url}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "drift_check",
                        "arguments": {"url": self.url}}},
        ]
        payload = "\n".join(json.dumps(m) for m in msgs) + "\n"
        out, _ = proc.communicate(payload, timeout=30)
        replies = {}
        for line in out.splitlines():
            if line.strip():
                obj = json.loads(line)
                if "id" in obj:
                    replies[obj["id"]] = obj
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"],
                         "drift-cli")
        names = {t["name"] for t in replies[2]["result"]["tools"]}
        self.assertEqual(names, {"drift_watch", "drift_check"})
        watch_text = replies[3]["result"]["content"][0]["text"]
        self.assertIn("snapshot saved", watch_text)
        check_text = replies[4]["result"]["content"][0]["text"]
        self.assertIn("NO CHANGE", check_text)


if __name__ == "__main__":
    unittest.main()
