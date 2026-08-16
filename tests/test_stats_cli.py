import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from hologram.cli import run_cli


class BudgetStatsCliTest(unittest.TestCase):
    def test_json_reports_exact_selection_without_writing_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text(
                "def public_operation(value):\n    return value\n")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = run_cli(["stats", "--root", str(root), "--budget",
                                "80", "--json"])

            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["policy_version"], "adaptive-bundles-v2")
            self.assertEqual(payload["requested_budget"], 80)
            self.assertEqual(payload["selected_tokens"], payload["full_tokens"])
            self.assertTrue(payload["fits"])
            self.assertEqual(payload["digest_tokens"],
                             payload["selected_tokens"])
            self.assertEqual(
                payload["managed_block_tokens"],
                payload["digest_tokens"] + payload["wrapper_tokens"]
                + payload["coaching_tokens"],
            )
            self.assertEqual(payload["retained_reasons"], {})
            self.assertEqual(payload["dropped_reasons"], {})
            self.assertFalse((root / "CLAUDE.md").exists())

    def test_human_summary_names_fit_and_token_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("def run():\n    return 1\n")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = run_cli(["stats", "--root", str(root)])

            self.assertEqual(code, 0)
            self.assertIn("policy: adaptive-bundles-v2", out.getvalue())
            self.assertIn("budget: unlimited · fits: yes", out.getvalue())
            self.assertIn("tokens: selected", out.getvalue())
            self.assertIn("managed block:", out.getvalue())
            self.assertIn("wrapper", out.getvalue())
            self.assertIn("coaching", out.getvalue())


if __name__ == "__main__":
    unittest.main()
