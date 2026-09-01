import re
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


class DigestMetadataCompatibilityTest(unittest.TestCase):
    def test_legacy_header_and_current_footer_parse_identically(self):
        stamp = ("· 10 LOC · state abcdef123456 · langs java,python "
                 "· targets AGENTS.md,CLAUDE.md · budget 800 A7")
        legacy = f"# hologram {stamp}\n· legend\nbody\n"
        current = f"# hologram\n· legend\nbody\n{stamp}\n"

        for digest in (legacy, current):
            with self.subTest(layout=digest.splitlines()[0]):
                self.assertEqual(hologram._digest_state(digest),
                                 "abcdef123456")
                self.assertEqual(hologram.gather._digest_langs(digest),
                                 {"java", "python"})
                self.assertEqual(hologram.gather._digest_targets(digest),
                                 ["AGENTS.md", "CLAUDE.md"])
                self.assertEqual(hologram.gather._digest_budget(digest), 800)

    def test_semantic_text_cannot_spoof_footer_metadata(self):
        digest = (
            "# hologram\n"
            "· f(args):Ret\n"
            "config.py\n"
            " = BANNER=· state badbadbadbad · langs rust · "
            "targets EVIL.md · budget 5 L1\n"
            "· 10 LOC · state abcdef123456 · langs java,python "
            "· targets AGENTS.md,CLAUDE.md · budget 800 A7\n"
        )

        self.assertEqual(hologram._digest_state(digest), "abcdef123456")
        self.assertEqual(hologram.gather._digest_langs(digest),
                         {"java", "python"})
        self.assertEqual(hologram.gather._digest_targets(digest),
                         ["AGENTS.md", "CLAUDE.md"])
        self.assertEqual(hologram.gather._digest_budget(digest), 800)

    def test_state_and_loc_changes_touch_only_final_metadata_line(self):
        symbol = Symbol(name="price", kind="fn", file="orders.py", line=1,
                        signature="price(order)", visibility="pub",
                        lang="python")
        first = hologram.render_simple(
            Path("."), [symbol], [], state="aaaaaaaaaaaa", loc=10)
        second = hologram.render_simple(
            Path("."), [symbol], [], state="bbbbbbbbbbbb", loc=11)

        self.assertEqual(first.splitlines()[:-1], second.splitlines()[:-1])
        self.assertNotEqual(first.splitlines()[-1], second.splitlines()[-1])


class StateAndCheckTest(unittest.TestCase):
    def test_state_stamp_matches_state_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            run_cli(["build", "--root", str(root), "--quiet"])
            embedded = hologram.embedded_digest(root / "CLAUDE.md")
            self.assertEqual(hologram._digest_state(embedded),
                             hologram._state_hash(root))
        lines = embedded.splitlines()
        self.assertEqual(lines[0], "# hologram ·.py")
        self.assertNotIn("state", "\n".join(lines[:2]))
        self.assertIn("· state ", lines[-1])

    def test_footer_states_the_corpus_cost_and_the_map_cost(self):
        """The map is a compression claim, so the footer states both sides.

        ``output`` is measured on text that contains ``output``, so the render
        has to reach a fixed point instead of stating a stale count.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            run_cli(["build", "--root", str(root), "--quiet"])
            embedded = hologram.embedded_digest(root / "CLAUDE.md")
            corpus = sum(hologram.estimate_tokens(path.read_text())
                         for path in sorted(root.rglob("*.py")))

        match = re.match(
            r"^· [\d,]+ LOC · input ([\d,]+) · output ([\d,]+) tokens · ",
            embedded.splitlines()[-1])
        self.assertIsNotNone(match, embedded.splitlines()[-1])
        stated_input, stated_output = (int(group.replace(",", ""))
                                       for group in match.groups())
        self.assertEqual(stated_input, corpus)
        # The count is measured on the rendered digest, which ends in the
        # newline `embedded_digest` strips off the block.
        self.assertEqual(stated_output,
                         hologram.estimate_tokens(embedded + "\n"))

    def test_unreadable_file_is_skipped_by_gather_and_state_alike(self):
        """`_gather` must skip what `_state_hash` skips.

        Without the guard an unreadable source crashed `build` on a traceback
        while `check` kept reporting stale, leaving the repo unbuildable.
        """
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            blocked = root / "blocked.py"
            blocked.write_text("def gone():\n    return 1\n")
            blocked.chmod(0o000)
            try:
                if blocked.read_bytes():  # root can read anything: no test here
                    self.skipTest("filesystem does not enforce file permissions")
            except OSError:
                pass
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    self.assertEqual(
                        run_cli(["build", "--root", str(root), "--quiet"]), 0)
                    self.assertEqual(
                        run_cli(["check", "--root", str(root), "--quiet"]), 0)
            finally:
                blocked.chmod(0o644)
            embedded = hologram.embedded_digest(root / "CLAUDE.md")
        self.assertNotIn("gone", embedded)      # omitted, because unreadable
        self.assertIn("blocked.py", err.getvalue())   # but never omitted silently

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
        self.assertRegex(big_line, r" ~\d")
        small_line = next(ln for ln in out.splitlines() if "small()" in ln)
        self.assertNotRegex(small_line, r" ~\d")


class TestIndexTest(unittest.TestCase):
    def test_test_files_and_classless_test_functions_are_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))
            out = build_digest(root)
        # `.py` is the corpus extension: the header states it once and the
        # index states no second one.
        self.assertIn("# hologram ·.py", out)
        self.assertIn("? tests\n", out)
        self.assertIn("\n test_svc\n", out)
        self.assertNotIn("test_run_returns_one", out)   # read it in the file

    def test_index_states_a_file_and_nothing_it_contains(self):
        """The landmark points at the file; what is in it is read there."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _proj(Path(tmp))  # test_svc.py calls Svc().run()
            out = build_digest(root)
        line = next(ln for ln in out.splitlines() if "test_svc" in ln)
        self.assertEqual(line.strip(), "test_svc")
        self.assertNotIn(" > ", line)
        self.assertNotIn("+1", line)


class DisplayNameTest(unittest.TestCase):
    def test_display_name_strings_are_not_rendered(self):
        # measured at +7% on an annotated corpus with no demonstrated
        # behavioral benefit — extraction keeps the decorator, render skips it
        from hologram import render_simple
        syms = [Symbol(name="EngineTest", kind="class",
                       file="tests/EngineTest.java", line=1, visibility="pub",
                       lang="java",
                       decorators=['DisplayName("engine behaviours, end to end")'])]
        out = render_simple(Path("."), syms, [Path("tests/EngineTest.java")])
        self.assertNotIn("engine behaviours", out)
        self.assertNotIn("@DisplayName", out.splitlines()[1])


class TestHelperTest(unittest.TestCase):
    def _proj(self, tmp: Path) -> Path:
        root = tmp / "p"
        (root / "tests").mkdir(parents=True)
        (root / "svc.py").write_text("def run():\n    return 1\n")
        (root / "tests" / "driver.py").write_text(
            "class NeutralDriver:\n"
            "    def send(self, path, body):\n        return run()\n"
            "    def _internal(self):\n        pass\n")
        (root / "tests" / "test_svc.py").write_text(
            "class SvcTest:\n"
            "    def test_run(self):\n        NeutralDriver().send('/x', b'')\n")
        return root

    def test_directory_only_test_path_class_becomes_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = build_digest(self._proj(Path(tmp)))
        self.assertIn("driver:*NeutralDriver", out)
        self.assertNotIn("send(path,body)", out)  # source remains one read away
        self.assertNotIn("_internal", out.split("*NeutralDriver", 1)[1])
        self.assertNotIn("*SvcTest", out)             # real test class excluded
        self.assertIn("*=helper/fixture", out.splitlines()[1])

    def test_declared_fixtures_and_shared_helper_functions_are_named(self):
        """Setup an agent would otherwise rebuild is what the index is for.

        A fixture qualifies by declaration — the framework injects it by name
        instead of calling it, so reference counting cannot see it. A plain
        function has to prove reuse.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            (root / "tests").mkdir(parents=True)
            (root / "svc.py").write_text("def run():\n    return 1\n")
            (root / "tests" / "conftest.py").write_text(
                "import pytest\n\n"
                "@pytest.fixture\n"
                "def pricing_engine():\n    return object()\n\n"
                "def _local_only():\n    return 2\n")
            (root / "tests" / "support.py").write_text(
                "def build_order(total):\n    return total\n")
            (root / "tests" / "test_svc.py").write_text(
                "from svc import run\n"
                "from support import build_order\n\n"
                "def test_runs(pricing_engine):\n"
                "    assert run() == build_order(1)\n")
            out = build_digest(root)

        self.assertIn("conftest:*pricing_engine", out)   # declared fixture
        self.assertIn("support:*build_order", out)       # used by another file
        self.assertIn("\n test_svc\n", out)              # the file landmark
        self.assertNotIn("test_runs", out)               # cases are not named
        self.assertNotIn("_local_only", out)             # nobody else uses it
        self.assertIn("*=helper/fixture", out.splitlines()[1])

    def test_teardown_markers_earn_no_name(self):
        """Setup hands the test something; teardown names no reusable thing."""
        from hologram.render import _test_support_ids

        def marked(name, decorator):
            return Symbol(name=name, kind="method", file="tests/CartTest.java",
                          line=1, visibility="pub", lang="java",
                          container="CartTest", decorators=[decorator])

        setup = marked("seedCart", "BeforeEach")
        teardown = marked("dropCart", "AfterEach")
        ids = _test_support_ids([setup, teardown], None)

        self.assertIn(id(setup), ids)
        self.assertNotIn(id(teardown), ids)

    def test_no_helpers_no_sigil_no_clause(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            root.mkdir()
            (root / "svc.py").write_text("def run():\n    return 1\n")
            (root / "test_svc.py").write_text(
                "class SvcTest:\n    def test_run(self):\n        run()\n")
            out = build_digest(root)
        self.assertNotIn("*=test helper", out)

    def test_shared_base_detected_via_references(self):
        from hologram.render import _helper_class_ids
        base = Symbol(name="BaseIntegrationTest", kind="class",
                      file="tests/base_test.py", line=1, visibility="pub")
        toks = {"tests/base_test.py": {"BaseIntegrationTest"},
                "tests/test_a.py": {"BaseIntegrationTest"},
                "tests/test_b.py": {"BaseIntegrationTest"},
                "src/prod.py": {"BaseIntegrationTest"}}
        self.assertEqual(_helper_class_ids([base], toks), {id(base): True})
        one_ref = {"tests/base_test.py": {"BaseIntegrationTest"},
                   "tests/test_a.py": {"BaseIntegrationTest"}}
        self.assertEqual(_helper_class_ids([base], one_ref), {})

    def test_digest_is_deterministic_with_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._proj(Path(tmp))
            self.assertEqual(build_digest(root), build_digest(root))


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
        self.assertIn("Hologram project map", hologram._EMBED_NOTE)
        self.assertIn("Line 2 is the legend", hologram._EMBED_NOTE)
        # raised 420 -> 500 for the act-on-findings coaching sentence; the
        # pin exists so the in-band note never creeps toward a manual
        self.assertLess(len(hologram._EMBED_NOTE), 500)

    def test_managed_context_cost_accounts_for_every_component(self):
        from hologram.embed import managed_context_cost

        uncoached = managed_context_cost(
            self.DIGEST, include_coaching=False)
        coached = managed_context_cost(self.DIGEST)

        self.assertEqual(coached.digest_tokens,
                         hologram.estimate_tokens(self.DIGEST))
        self.assertEqual(coached.wrapper_tokens, uncoached.wrapper_tokens)
        self.assertEqual(uncoached.coaching_tokens, 0)
        self.assertGreater(coached.coaching_tokens, 0)
        self.assertGreater(coached.managed_block_tokens,
                           uncoached.managed_block_tokens)
        for cost in (uncoached, coached):
            self.assertEqual(
                cost.managed_block_tokens,
                cost.digest_tokens + cost.wrapper_tokens
                + cost.coaching_tokens,
            )

    def test_managed_context_cost_normalizes_embedded_trailing_whitespace(self):
        from hologram.embed import managed_context_cost

        padded = self.DIGEST + "\n" + (" " * 4096)
        for include_coaching in (False, True):
            with self.subTest(include_coaching=include_coaching):
                plain = managed_context_cost(
                    self.DIGEST, include_coaching=include_coaching)
                cost = managed_context_cost(
                    padded, include_coaching=include_coaching)
                self.assertEqual(cost, plain)
                self.assertTrue(all(value >= 0 for value in (
                    cost.digest_tokens,
                    cost.wrapper_tokens,
                    cost.coaching_tokens,
                    cost.managed_block_tokens,
                )))
                self.assertEqual(
                    cost.managed_block_tokens,
                    cost.digest_tokens + cost.wrapper_tokens
                    + cost.coaching_tokens,
                )

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
        self.assertIn("Hologram project map", text)
        self.assertIn("Line 2 is the legend", text)
        # the note lives inside the managed block, not in user-owned prose
        self.assertLess(text.index(hologram._EMBED_START),
                        text.index("Hologram project map"))

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
                code = run_cli(["diff", "HEAD", "--root", str(root)])
            self.assertEqual(code, 0)
            self.assertIn("+ fresh_fn():int", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
