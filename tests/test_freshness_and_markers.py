import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
from hologram import Symbol, build_digest, run_cli  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _proj(tmp: Path) -> Path:
    root = tmp / "proj"
    root.mkdir()
    (root / "svc.py").write_text(
        "class Svc:\n"
        "    def run(self) -> int:\n        return self._step()\n"
        "    def _step(self) -> int:\n        return 1\n"
    )
    (root / "test_svc.py").write_text(
        "from svc import Svc\n\n"
        "def test_run_returns_one():\n    assert Svc().run() == 1\n"
    )
    return root


class StateAndCheckTest(unittest.TestCase):
    def test_state_stamp_matches_state_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            run_cli(["build", "--root", str(root), "--quiet"])
            embedded = hologram.embedded_digest(root / "CLAUDE.md")
            self.assertEqual(hologram._digest_state(embedded),
                             hologram._state_hash(root))

    def test_generator_change_invalidates_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            with patch.object(hologram.gather, "_generator_fingerprint",
                              return_value=b"one"):
                before = hologram._state_hash(root)
            with patch.object(hologram.gather, "_generator_fingerprint",
                              return_value=b"two"):
                after = hologram._state_hash(root)
        self.assertNotEqual(before, after)

    def test_fingerprint_covers_every_package_source(self):
        """The fingerprint hashes each .py in the package, sorted by relative path,
        so checkout, wheel, and zipapp installs of the same sources agree."""
        import hashlib
        pkg = Path(hologram.__file__).resolve().parent
        entries = sorted(
            (str(p.relative_to(pkg)).replace("\\", "/"), p.read_bytes())
            for p in pkg.rglob("*.py") if "__pycache__" not in p.parts)
        h = hashlib.sha256()
        for rel, data in entries:
            h.update(rel.encode())
            h.update(data)
        self.assertEqual(hologram.gather._generator_fingerprint(), h.digest())
        self.assertGreater(len(entries), 10)  # the split actually happened

    def test_check_fresh_then_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            run_cli(["build", "--root", str(root), "--quiet"])
            self.assertEqual(run_cli(["check", "--root", str(root),
                                      "--quiet"]), 0)
            (root / "svc.py").write_text("def added() -> int:\n    return 2\n")
            self.assertEqual(run_cli(["check", "--root", str(root),
                                      "--quiet"]), 1)

    def test_check_stale_when_no_block_embedded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            (root / "CLAUDE.md").write_text("# User rules\n")
            self.assertEqual(run_cli(["check", "--root", str(root), "--quiet"]), 1)

    def test_build_if_stale_skips_when_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            claude = root / "CLAUDE.md"
            run_cli(["build", "--root", str(root), "--quiet"])
            mtime = claude.stat().st_mtime_ns
            run_cli(["build", "--root", str(root), "--if-stale", "--quiet"])
            self.assertEqual(claude.stat().st_mtime_ns, mtime)   # untouched
            (root / "svc.py").write_text("def other() -> int:\n    return 3\n")
            run_cli(["build", "--root", str(root), "--if-stale", "--quiet"])
            self.assertNotEqual(claude.stat().st_mtime_ns, mtime)


class TestedMarkerTest(unittest.TestCase):
    def test_symbol_named_in_tests_gets_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            out = build_digest(root)
        run_line = next(ln for ln in out.splitlines() if "run()" in ln)
        self.assertIn("✓", run_line)                     # named in test file
        self.assertIn("✓=tested", out)

    def test_untested_symbol_unmarked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            root.mkdir()
            (root / "a.py").write_text("def lonely() -> int:\n    return 1\n")
            out = build_digest(root)
        lonely = next(ln for ln in out.splitlines() if "lonely()" in ln)
        self.assertNotIn("✓", lonely)


class SizeMarkerTest(unittest.TestCase):
    def test_large_body_marked_small_not(self):
        big = "def big() -> int:\n" + "".join(
            f"    x{i} = {i}\n" for i in range(60)) + "    return 0\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            root.mkdir()
            (root / "a.py").write_text(big + "\ndef small() -> int:\n    return 1\n")
            out = build_digest(root)
        big_line = next(ln for ln in out.splitlines() if "big()" in ln)
        self.assertIn("⋮", big_line)
        small_line = next(ln for ln in out.splitlines() if "small()" in ln)
        self.assertNotIn("⋮", small_line)


class TestIndexTest(unittest.TestCase):
    def test_test_files_always_listed_without_test_functions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            out = build_digest(root)
        self.assertIn("? tests", out)
        self.assertIn("test_svc.py", out)
        self.assertNotIn("test_run_returns_one", out)


class DepsMapTest(unittest.TestCase):
    def test_cross_module_type_reference_produces_edge(self):
        syms = [
            Symbol(name="Core", kind="class", file="core/c.py", line=1,
                   visibility="pub"),
            Symbol(name="use_core", kind="fn", file="app/a.py", line=1,
                   signature="use_core()", visibility="pub"),
        ]
        tokens = {"core/c.py": {"Core"},
                  "app/a.py": {"use_core", "Core"}}
        lines = hologram._dep_lines(syms, tokens, min_refs=1)
        self.assertTrue(any("app→core" in ln for ln in lines))


class EmbedTest(unittest.TestCase):
    DIGEST = ("# proj @x 2026-08-08 · 10 LOC · state ab · regen: x\n"
              "· legend: …\n"
              "src\n"
              " Svc(C)\n"
              "  run():int ✓ > _step\n"
              "  - _step\n")

    def test_embed_creates_block_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cm = Path(tmp) / "CLAUDE.md"
            cm.write_text("# My rules\nUse tabs.\n")
            hologram.embed_digest(cm, self.DIGEST)
            text = cm.read_text()
        self.assertIn("My rules", text)                     # user content kept
        self.assertIn("hologram:start", text)
        self.assertIn("run():int ✓ > _step", text)
        self.assertNotIn("whole codebase at a glance", text)

    def test_embed_note_stays_short_and_identifiable(self):
        self.assertIn("hologram map of this repository", hologram._EMBED_NOTE)
        self.assertIn("Line 2 is the legend", hologram._EMBED_NOTE)
        self.assertLess(len(hologram._EMBED_NOTE), 260)

    def test_embed_is_idempotent_and_refreshes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cm = Path(tmp) / "CLAUDE.md"
            cm.write_text("before\n")
            hologram.embed_digest(cm, self.DIGEST)
            hologram.embed_digest(cm, self.DIGEST.replace("run()", "go()"))
            text = cm.read_text()
        self.assertEqual(text.count("hologram:start"), 1)   # one block, replaced
        self.assertIn("go():int", text)
        self.assertNotIn("run():int", text)
        self.assertTrue(text.startswith("before"))

    def test_large_digest_is_embedded_exactly_without_degradation(self):
        big = self.DIGEST + "\n".join(
            f"  method{i}(int):int > callee{i},other{i}" for i in range(400))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CLAUDE.md"
            hologram.embed_digest(path, big)
            embedded = path.read_text()
        self.assertEqual(
            embedded,
            f"{hologram._EMBED_START}\n{hologram._EMBED_NOTE}\n\n```\n"
            f"{big.rstrip()}\n```\n{hologram._EMBED_END}\n",
        )
        self.assertIn("> callee399,other399", embedded)

    def test_block_carries_a_note_explaining_what_it_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CLAUDE.md"
            hologram.embed_digest(path, self.DIGEST)
            text = path.read_text()
        self.assertIn("hologram map of this repository", text)
        self.assertIn("Line 2 is the legend", text)
        # the note lives inside the managed block, not in user-owned prose
        self.assertLess(text.index(hologram._EMBED_START),
                        text.index("hologram map of this repository"))

    def test_embedded_digest_roundtrips_the_exact_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CLAUDE.md"
            path.write_text("# Rules\n\nmentioning hologram:end in prose\n")
            hologram.embed_digest(path, self.DIGEST)
            self.assertEqual(hologram.embedded_digest(path), self.DIGEST.rstrip())

    def test_prose_mentioning_end_marker_does_not_duplicate_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CLAUDE.md"
            path.write_text(f"# Rules\n\nDon't write {hologram._EMBED_END} here.\n")
            hologram.embed_digest(path, self.DIGEST)
            hologram.embed_digest(path, self.DIGEST)
            text = path.read_text()
        self.assertEqual(text.count(hologram._EMBED_START), 1)
        self.assertEqual(text.count("run():int"), 1)

    def test_cli_build_embeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            run_cli(["build", "--root", str(root), "--quiet"])
            text = (root / "CLAUDE.md").read_text()
        self.assertIn("hologram:start", text)
        self.assertIn("Svc(C)", text)

    def test_cli_build_preserves_user_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            claude = root / "CLAUDE.md"
            claude.write_text("# User rules\n\nKeep this.\n")
            run_cli(["build", "--root", str(root), "--quiet"])
            text = claude.read_text()
        self.assertTrue(text.startswith("# User rules\n\nKeep this."))
        self.assertIn("hologram:start", text)

    def test_check_and_if_stale_follow_the_embedded_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            args = ["--root", str(root), "--quiet"]
            claude = root / "CLAUDE.md"
            run_cli(["build", *args])
            self.assertEqual(run_cli(["check", *args]), 0)
            claude.write_text(claude.read_text().replace("· state ", "· state x"))
            self.assertEqual(run_cli(["check", *args]), 1)
            self.assertEqual(run_cli(["build", "--if-stale", *args]), 0)
            self.assertEqual(run_cli(["check", *args]), 0)


class ContextTargetsTest(unittest.TestCase):
    def test_defaults_to_claude_md_when_repo_has_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            self.assertEqual(hologram.context_targets(root),
                             [root / "CLAUDE.md"])

    def test_detects_existing_agent_files_and_rule_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            (root / "AGENTS.md").write_text("# codex\n")
            (root / "GEMINI.md").write_text("# gemini\n")
            (root / ".clinerules").write_text("# cline\n")
            (root / ".github").mkdir()
            (root / ".github" / "copilot-instructions.md").write_text("# copilot\n")
            (root / ".cursor" / "rules").mkdir(parents=True)
            targets = {str(t.relative_to(root))
                       for t in hologram.context_targets(root)}
        self.assertEqual(targets, {
            "AGENTS.md", "GEMINI.md", ".clinerules",
            ".github/copilot-instructions.md", ".cursor/rules/hologram.mdc",
        })
        self.assertNotIn("CLAUDE.md", targets)   # absent file isn't created

    def test_detects_new_agent_files_and_rule_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            (root / "AGENT.md").write_text("# amp\n")
            (root / "CONVENTIONS.md").write_text("# aider\n")
            (root / ".junie").mkdir()
            (root / ".continue" / "rules").mkdir(parents=True)
            (root / ".kiro" / "steering").mkdir(parents=True)
            targets = {str(t.relative_to(root))
                       for t in hologram.context_targets(root)}
        self.assertEqual(targets, {
            "AGENT.md", "CONVENTIONS.md", ".junie/guidelines.md",
            ".continue/rules/hologram.md", ".kiro/steering/hologram.md",
        })

    def test_continue_rule_seeded_with_front_matter_clinerules_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            (root / ".continue" / "rules").mkdir(parents=True)
            (root / ".clinerules").mkdir()
            run_cli(["build", "--root", str(root), "--quiet"])
            cont = (root / ".continue" / "rules" / "hologram.md").read_text()
            cline = (root / ".clinerules" / "hologram.md").read_text()
        self.assertTrue(cont.startswith("---\n"))
        self.assertIn("alwaysApply: true", cont)
        self.assertFalse(cline.startswith("---\n"))  # same basename, no seed

    def test_build_embeds_into_every_present_context_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            (root / "CLAUDE.md").write_text("# claude\n")
            (root / "AGENTS.md").write_text("# codex\n")
            (root / ".cursor" / "rules").mkdir(parents=True)
            run_cli(["build", "--root", str(root), "--quiet"])
            for rel in ("CLAUDE.md", "AGENTS.md", ".cursor/rules/hologram.mdc"):
                self.assertIn("Svc(C)", hologram.embedded_digest(root / rel), rel)
            mdc = (root / ".cursor/rules/hologram.mdc").read_text()
        self.assertTrue(mdc.startswith("---\n"))       # cursor front matter seeded
        self.assertIn("alwaysApply: true", mdc)

    def test_check_is_stale_when_one_target_lags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            (root / "CLAUDE.md").write_text("# claude\n")
            (root / "AGENTS.md").write_text("# codex\n")
            args = ["--root", str(root), "--quiet"]
            run_cli(["build", *args])
            self.assertEqual(run_cli(["check", *args]), 0)
            (root / "AGENTS.md").write_text("# codex only\n")   # block dropped
            self.assertEqual(run_cli(["check", *args]), 1)
            run_cli(["build", "--if-stale", *args])
            self.assertEqual(run_cli(["check", *args]), 0)


class DiffCommandTest(unittest.TestCase):
    def test_diff_shows_added_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "-qm", "one"], cwd=root, check=True)
            (root / "svc.py").write_text(
                (root / "svc.py").read_text()
                + "\ndef fresh_fn() -> int:\n    return 9\n")
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = run_cli(["diff", "HEAD", "--root", str(root), "--quiet"])
            self.assertEqual(code, 0)
            self.assertIn("+fresh_fn():int", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
