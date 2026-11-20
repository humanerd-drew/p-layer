import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from p_layer.cli import main


class CliUxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "memory.db"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--db", str(self.db), "--embed", "hash", *args])
        return code, output.getvalue()

    def test_status_is_read_only_before_init(self):
        code, output = self.run_cli("status", "--json")

        self.assertEqual(code, 0)
        self.assertFalse(self.db.exists())
        self.assertEqual(json.loads(output)["status"], "not_initialized")

    def test_init_doctor_and_remember_recall_happy_path(self):
        code, output = self.run_cli("init")
        self.assertEqual(code, 0)
        self.assertIn("next: p-layer --db", output)
        self.assertIn(" remember \"your first memory\"", output)
        self.assertIn(f"--db {self.db}", output)
        self.assertIn("--embed hash", output)

        code, _ = self.run_cli(
            "remember",
            "Groble handles payments",
            "--type",
            "decision",
            "--source",
            "test-session",
        )
        self.assertEqual(code, 0)

        code, output = self.run_cli("recall", "payments", "--why", "--no-semantic")
        self.assertEqual(code, 0)
        self.assertIn("(decision, P5)", output)
        self.assertIn("source=test-session", output)

        code, _ = self.run_cli(
            "remember",
            "deployments use blue green",
            "--type",
            "pattern",
        )
        self.assertEqual(code, 0)

        code, output = self.run_cli("context", "payments", "--no-semantic")
        self.assertEqual(code, 0)
        self.assertIn("Groble handles payments", output)
        self.assertNotIn("blue green", output)

        code, output = self.run_cli("doctor", "--json")
        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["counts"]["knowledge"], 2)

    def test_korean_readme_quickstart_roundtrip(self):
        code, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        code, _ = self.run_cli("init")
        self.assertEqual(code, 0)
        code, _ = self.run_cli(
            "remember",
            "결제를 PortOne v2로 바꿨다",
            "--type",
            "decision",
            "--source",
            "onboarding",
        )
        self.assertEqual(code, 0)
        code, _ = self.run_cli("remember", "무관한 서버 점검 기록", "--type", "fact")
        self.assertEqual(code, 0)

        code, recall_out = self.run_cli("recall", "결제", "--why")
        self.assertEqual(code, 0)
        self.assertIn("PortOne v2", recall_out)
        self.assertIn("source=onboarding", recall_out)
        self.assertNotIn("무관한 서버 점검 기록", recall_out)

        code, ctx_out = self.run_cli("context", "현재 결제 작업")
        self.assertEqual(code, 0)
        self.assertIn("PortOne v2", ctx_out)
        self.assertNotIn("무관한 서버 점검 기록", ctx_out)

        code, doc_out = self.run_cli("doctor", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(doc_out)["status"], "ok")


if __name__ == "__main__":
    unittest.main()
