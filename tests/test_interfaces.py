import contextlib
import io
import json
import threading
import unittest
import urllib.error
import urllib.request

from tests.helpers import CFG
from veridia.api import make_server
from veridia.cli import main
from veridia.config import ROOT
from veridia.data import load_dataset
from veridia.service import Service


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def test_one_command_gives_a_result(self):
        code, out, _ = run_cli("match", "--example", "buyer_de", "--no-log")
        self.assertEqual(code, 0)
        self.assertIn("31 of 1000 manufacturers match", out)
        self.assertIn("How the list narrowed", out)

    def test_json_output_is_complete_and_parseable(self):
        code, out, _ = run_cli("match", "--example", "buyer_a", "--no-log", "--json", "--limit", "3")
        data = json.loads(out)
        self.assertEqual((code, data["total_matches"], len(data["matches"])), (0, 34, 3))
        self.assertEqual(data["as_of"], "2026-10-28")

    def test_as_of_changes_the_answer_and_is_echoed(self):
        _, out, _ = run_cli("match", "--example", "buyer_a", "--no-log", "--json", "--as-of", "2029-12-31")
        data = json.loads(out)
        self.assertEqual((data["as_of"], data["total_matches"]), ("2029-12-31", 0))

    def test_bad_input_exits_2_with_helpful_message(self):
        import os, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"material": "steel", "process": "CNC", "quantity": 0, "deadline_weeks": -3}, f)
        try:
            code, _, err = run_cli("match", "--requirement", f.name, "--no-log")
        finally:
            os.unlink(f.name)
        self.assertEqual(code, 2)
        self.assertIn("did you mean", err)
        self.assertIn("quantity", err)
        code, _, err = run_cli("match", "--example", "buyer_a", "--as-of", "yesterday", "--no-log")
        self.assertEqual(code, 2)

    def test_why_not_quality_and_demo_commands(self):
        code, out, _ = run_cli("why-not", "VM1006", "--example", "buyer_a", "--no-log")
        self.assertEqual(code, 0)
        self.assertIn("MATCHES", out)
        code, out, _ = run_cli("quality", "--no-log")
        self.assertIn("DUPLICATE_ID", out)
        code, out, _ = run_cli("demo", "--no-log")
        self.assertIn("rejected", out)

    def test_ask_without_a_key_degrades_gracefully(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            code, _, err = run_cli("ask", "3000 aluminium parts", "--no-log")
        self.assertEqual(code, 2)
        self.assertIn("structured fields", err)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(Service(load_dataset(ROOT / "data" / "veridia_csv.csv"), CFG), "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    A = {"material": "aluminium", "process": "CNC machining", "quantity": 1000, "deadline_weeks": 6, "certifications": ["ISO 9001"]}
    B = {"material": "stainless steel", "process": "sheet metal", "quantity": 1500, "deadline_weeks": 6, "certifications": ["ISO 9001"]}

    def test_health_quality_match(self):
        self.assertEqual(self.call("GET", "/health")[1]["status"], "ok")
        self.assertIn("issue_counts", self.call("GET", "/quality")[1])
        code, body = self.call("POST", "/match", {"requirement": self.A, "limit": 2})
        self.assertEqual((code, body["total_matches"], len(body["matches"])), (200, 34, 2))

    def test_errors_are_structured_never_stack_traces(self):
        code, body = self.call("POST", "/match", {"requirement": {**self.A, "quantity": -1}})
        self.assertEqual(code, 400)
        self.assertEqual(body["error"]["errors"][0]["field"], "quantity")
        self.assertEqual(self.call("POST", "/match", raw=b"{not json")[0], 400)
        self.assertEqual(self.call("POST", "/match", raw=b"[1]")[0], 400)
        self.assertEqual(self.call("POST", "/match", {"requirement": None})[0], 400)
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("POST", "/match", raw=b"x" * 70000)[0], 400)
        self.assertEqual(self.call("POST", "/match", {"requirement": self.A, "as_of": "nope"})[0], 400)

    def test_session_flow_with_contention_returns_409(self):
        self.assertEqual(self.call("POST", "/sessions/api-A/requirement", {"requirement": self.A})[0], 200)
        self.assertEqual(self.call("POST", "/sessions/api-B/requirement", {"requirement": self.B})[0], 200)
        self.assertEqual(self.call("POST", "/sessions/api-A/hold", {"key": "VM1006"})[0], 200)
        code, body = self.call("POST", "/sessions/api-B/hold", {"key": "VM1006"})
        self.assertEqual((code, body["error"]["type"]), (409, "capacity_unavailable"))
        code, body = self.call("POST", "/sessions/api-B/requirement", {"requirement": {**self.B, "deadline_weeks": 8}})
        self.assertEqual(code, 200)
        self.assertEqual(body["diff"]["requirement_changes"], [{"field": "deadline_weeks", "from": 6, "to": 8}])
        self.assertEqual(self.call("GET", "/sessions/api-B/history")[1]["history"][-1]["revision"], 2)
        self.assertEqual(self.call("POST", "/sessions/nope/hold", {"key": "VM1006"})[0], 404)


if __name__ == "__main__":
    unittest.main()
