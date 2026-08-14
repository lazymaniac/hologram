import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
from hologram import run_cli  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JAVAMINI = FIXTURES / "javamini"
PYMINI_FILE = FIXTURES / "pymini" / "app.py"

needs_java = unittest.skipUnless(hologram.has_parser("java"),
                                 "tree-sitter-java not installed")


def _make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    shutil.copytree(JAVAMINI, repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )
    return repo


@needs_java
class CliBuildTest(unittest.TestCase):
    def test_build_embeds_map_in_claude_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            shutil.copytree(JAVAMINI, proj)
            code = run_cli(["build", "--root", str(proj), "--quiet"])
            self.assertEqual(code, 0)
            content = hologram.embedded_digest(proj / "CLAUDE.md")
            self.assertIn("PricingEngine", content)
            self.assertIn("> ", content)


@needs_java
class InitHooksTest(unittest.TestCase):
    def test_init_installs_hooks_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            self.assertEqual(run_cli(["init", "--root", str(repo), "--quiet"]), 0)
            self.assertEqual(run_cli(["init", "--root", str(repo), "--quiet"]), 0)
            hook = repo / ".git" / "hooks" / "post-commit"
            self.assertTrue(hook.exists())
            content = hook.read_text()
            # post-commit carries build + review invocations
            self.assertEqual(content.count("hologram.py"), 2)
            self.assertIn("review HEAD~1", content)
            self.assertNotIn("--embed", content)     # embedding is the only mode
            # the measured-and-reverted pre-commit variant must not install
            self.assertFalse((repo / ".git" / "hooks" / "pre-commit").exists())
            merge = (repo / ".git" / "hooks" / "post-merge").read_text()
            self.assertEqual(merge.count("hologram.py"), 1)
            self.assertNotIn("review", merge)        # review is commit-time only
            self.assertIn("hologram:start", (repo / "CLAUDE.md").read_text())

    def test_init_replaces_hook_line_from_older_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            script = Path(hologram.__file__).resolve().parent.parent / "hologram.py"
            hook = repo / ".git" / "hooks" / "post-commit"
            old = (f'python3 "{script}" build --root "{repo.resolve()}" '
                   f'--no-embed --quiet || true')
            hook.write_text("#!/bin/sh\n" + old + "\n")
            run_cli(["init", "--root", str(repo), "--quiet"])
            content = hook.read_text()
            self.assertNotIn("--no-embed", content)
            self.assertEqual(content.count("hologram.py"), 2)  # build + review

    def test_init_preserves_custom_wrapped_hologram_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            hook = repo / ".git" / "hooks" / "post-commit"
            script = Path(hologram.__file__).resolve().parent.parent / "hologram.py"
            custom = (f'[ -f /tmp/run-hologram ] && python3 "{script}" build '
                      f'--root "{repo.resolve()}" --quiet || true')
            hook.write_text("#!/bin/sh\n" + custom + "\n")
            run_cli(["init", "--root", str(repo), "--quiet"])
            content = hook.read_text()
            self.assertIn(custom, content)
            self.assertEqual(content.count("hologram.py"), 3)  # custom + build + review

    def test_init_chains_existing_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            hook = repo / ".git" / "hooks" / "post-commit"
            hook.write_text("#!/bin/sh\necho existing\n")
            run_cli(["init", "--root", str(repo), "--quiet"])
            content = hook.read_text()
            self.assertIn("echo existing", content)
            self.assertIn("hologram", content)


@needs_java
class InitLangTest(unittest.TestCase):
    def test_lang_flag_baked_into_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            run_cli(["init", "--root", str(repo), "--lang", "java", "--quiet"])
            hook = (repo / ".git" / "hooks" / "post-commit").read_text()
            self.assertIn("--lang java", hook)


@needs_java
class HookQuotingTest(unittest.TestCase):
    def test_dollar_in_repo_path_is_escaped_in_hook_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp) / "x$(touch pwned)"
            outer.mkdir()
            repo = _make_repo(outer)
            run_cli(["init", "--root", str(repo), "--quiet"])
            hook = (repo / ".git" / "hooks" / "post-commit").read_text()
        self.assertIn("x\\$(touch pwned)", hook)
        self.assertNotIn('"' + str(repo) + '"', hook)  # raw form absent

    def test_reinit_replaces_escaped_line_not_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp) / "y$z"
            outer.mkdir()
            repo = _make_repo(outer)
            run_cli(["init", "--root", str(repo), "--quiet"])
            run_cli(["init", "--root", str(repo), "--quiet"])
            hook = (repo / ".git" / "hooks" / "post-commit").read_text()
        self.assertEqual(hook.count("build --root"), 1)


class ManagedHookLineTest(unittest.TestCase):
    def test_review_only_line_recognized_and_near_misses_rejected(self):
        from hologram.cli import _managed_hook_line, _sh_dq
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            script = Path(hologram.__file__).resolve().parent.parent / "hologram.py"
            root_q = _sh_dq(str(repo.resolve()))
            good = (f'python3 "{script}" review HEAD --root "{root_q}"'
                    f' --quiet-if-clean || true # hologram:managed')
            self.assertTrue(_managed_hook_line(good, repo))
            for bad in (good.replace(" --quiet-if-clean", ""),
                        good.replace(" || true", ""),
                        good.replace("review HEAD ", "review HEAD --force ")):
                self.assertFalse(_managed_hook_line(bad, repo), bad)

    def test_escaped_root_review_line_recognized(self):
        from hologram.cli import _managed_hook_line, _sh_dq
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp) / "d$r"
            outer.mkdir()
            repo = _make_repo(outer)
            script = Path(hologram.__file__).resolve().parent.parent / "hologram.py"
            line = (f'python3 "{script}" review HEAD --root '
                    f'"{_sh_dq(str(repo.resolve()))}" --quiet-if-clean || true')
            self.assertTrue(_managed_hook_line(line, repo))


class PostCommitHookE2ETest(unittest.TestCase):
    def test_findings_print_after_commit_and_never_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            repo.mkdir()
            (repo / "util.py").write_text(
                "def normalize_amount(v):\n    return int(v)\n")
            for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                         "commit", "-qm", "base"]):
                subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
            run_cli(["init", "--root", str(repo), "--quiet"])
            (repo / "money.py").write_text(
                "def normalise_amounts(vs):\n    return [int(v) for v in vs]\n")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                           capture_output=True)
            r = subprocess.run(["git", "-c", "user.email=t@t", "-c",
                                "user.name=t", "commit", "-m", "dup"],
                               cwd=repo, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0)  # advisory: never blocks
            # git routes hook stdout to its own stderr; the findings land in
            # the committing agent's tool result either way
            out = r.stdout + r.stderr
            self.assertIn("hologram review vs HEAD~1:", out)
            self.assertIn("normalize_amount", out)
            log = subprocess.run(["git", "log", "--oneline"], cwd=repo,
                                 capture_output=True, text=True).stdout
            self.assertIn("dup", log)  # the commit landed


class BudgetTest(unittest.TestCase):
    def _proj(self, tmp: Path) -> Path:
        root = tmp / "p"
        root.mkdir()
        (root / "config.py").write_text(
            "MAX_RETRIES = 3\n\n"
            "def _private_helper():\n    pass\n\n"
            "def used():\n    _private_helper()\n")
        (root / "test_config.py").write_text(
            "class ConfigTest:\n    def test_used(self):\n        used()\n")
        return root

    def test_ladder_is_deterministic_and_stamped(self):
        from hologram import build_digest
        with tempfile.TemporaryDirectory() as tmp:
            root = self._proj(Path(tmp))
            full = build_digest(root)
            a = build_digest(root, budget=1)
            b = build_digest(root, budget=1)
        self.assertEqual(a, b)                      # same budget, same map
        self.assertIn("MAX_RETRIES=3", full)
        self.assertNotIn("MAX_RETRIES=3", a)        # L1: const values gone
        self.assertIn("MAX_RETRIES", a)             # ...but names stay
        self.assertNotIn("- config.py:", a)  # L3: private inventory line gone
        self.assertIn("· budget 1 L", a.splitlines()[0])

    def test_untested_chains_drop_before_tested(self):
        from hologram import build_digest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            root.mkdir()
            (root / "app.py").write_text(
                "def target():\n    return 1\n\n"
                "def covered():\n    return target()\n\n"
                "def uncovered():\n    return target()\n")
            (root / "test_app.py").write_text(
                "from app import covered\n\n"
                "def test_covered():\n    assert covered() == 1\n")
            # detail 4 renders: covered keeps its chain, uncovered loses it
            from hologram.render import render_simple
            from hologram.gather import _gather
            files, syms, ft, ut, state = _gather(root, None)
            out = render_simple(root, syms, files, file_tokens=ft, detail=4)
        cov = next(ln for ln in out.splitlines() if "covered()" in ln
                   and "uncovered" not in ln)
        unc = next(ln for ln in out.splitlines() if "uncovered()" in ln)
        self.assertIn("> target", cov)
        self.assertNotIn("> target", unc)

    def test_levels_monotonic(self):
        # monotonicity holds for the map body; the one-line budget
        # disclosure in the header is bounded overhead that real corpora
        # dwarf but a five-line fixture does not
        from hologram.gather import _gather
        from hologram.render import _MAX_LEVEL, render_simple
        from hologram import estimate_tokens

        def body(text):
            return "\n".join(l for l in text.splitlines()
                             if not l.startswith(("# hologram", "· ", "‥ ")))

        with tempfile.TemporaryDirectory() as tmp:
            root = self._proj(Path(tmp))
            files, syms, ft, ut, state = _gather(root, None)
            sizes = [estimate_tokens(body(render_simple(
                         root, syms, files, file_tokens=ft, detail=lvl)))
                     for lvl in range(_MAX_LEVEL + 1)]
        for earlier, later in zip(sizes, sizes[1:]):
            self.assertLessEqual(later, earlier)

    def test_skeleton_floor(self):
        from hologram import build_digest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            root.mkdir()
            (root / "engine.py").write_text(
                "class Engine:\n"
                "    def __init__(self, prices):\n        self.prices = prices\n"
                "    def evaluate(self, order):\n        return self.check(order)\n"
                "    def check(self, order):\n        return order\n\n"
                "def top_level(x):\n    return Engine(x).evaluate(x)\n")
            (root / "test_engine.py").write_text(
                "from engine import Engine\n\n"
                "def test_e():\n    assert Engine({}).evaluate(1) == 1\n")
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                a = build_digest(root, budget=1)
        self.assertIn("Engine(C", a)               # type header stays
        self.assertIn("top_level(x)", a)           # top-level fn sig stays
        self.assertNotIn("evaluate", a)            # method lines gone
        self.assertNotIn(" > ", a)                 # no chains anywhere
        self.assertNotIn("- ", "\n".join(
            l for l in a.splitlines() if l.strip().startswith("- ")))
        self.assertIn("· budget 1 L7", a.splitlines()[0])
        self.assertNotIn("project calls", a.splitlines()[1])  # legend honest
        self.assertIn("even the skeleton map", err.getvalue())

    def test_floor_warning_only_below_skeleton(self):
        from hologram import build_digest, estimate_tokens
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            root = self._proj(Path(tmp))
            floor = build_digest(root, budget=1)
            generous = estimate_tokens(floor) + 50
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                build_digest(root, budget=generous)
        self.assertNotIn("warning", err.getvalue())

    def test_cold_type_fan_in(self):
        from hologram.gather import _gather
        from hologram.render import render_simple
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            root.mkdir()
            (root / "app.py").write_text(
                "class Warm:\n"
                "    def hit(self):\n        return 1\n\n"
                "class Cold:\n"
                "    def miss(self):\n        return 2\n\n"
                "def caller():\n    return Warm().hit()\n")
            files, syms, ft, ut, state = _gather(root, None)
            out = render_simple(root, syms, files, file_tokens=ft, detail=5)
        self.assertIn("hit(", out)      # externally referenced type keeps methods
        self.assertNotIn("miss(", out)  # zero fan-in type loses them
        self.assertIn("Cold", out)      # ...but keeps its (grouped) header

    def test_budget_stamp_recalled_and_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._proj(Path(tmp))
            run_cli(["build", "--root", str(root), "--budget", "1", "--quiet"])
            first = (root / "CLAUDE.md").read_text()
            self.assertIn("· budget 1", first)
            run_cli(["build", "--root", str(root), "--quiet"])  # flagless
            self.assertIn("· budget 1",
                          (root / "CLAUDE.md").read_text())     # recalled
            run_cli(["build", "--root", str(root), "--budget", "0", "--quiet"])
            cleared = (root / "CLAUDE.md").read_text()
            self.assertNotIn("· budget", cleared)
            self.assertIn("MAX_RETRIES=3", cleared)


class TargetOptionTest(unittest.TestCase):
    """--target restricts which context files carry the map; the restriction
    is stamped into the header and recalled by flagless rebuilds."""

    def _proj(self, tmp: Path) -> Path:
        root = tmp / "p"
        root.mkdir()
        (root / "app.py").write_text("def run():\n    pass\n")
        (root / "CLAUDE.md").write_text("# claude prose\n")
        (root / "AGENTS.md").write_text("# agents prose\n")
        return root

    def test_restrict_stamps_recalls_and_prunes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._proj(Path(tmp))
            run_cli(["build", "--root", str(root), "--quiet"])
            self.assertIn("hologram:start", (root / "AGENTS.md").read_text())
            run_cli(["build", "--root", str(root), "--target", "CLAUDE.md",
                     "--quiet"])
            claude = (root / "CLAUDE.md").read_text()
            agents = (root / "AGENTS.md").read_text()
            self.assertIn("· targets CLAUDE.md", claude)
            self.assertNotIn("hologram:start", agents)   # block pruned
            self.assertIn("agents prose", agents)         # prose survives
            # check validates only the stamped subset
            self.assertEqual(run_cli(["check", "--root", str(root),
                                      "--quiet"]), 0)
            # flagless rebuild respects the stamp
            (root / "app.py").write_text("def run2():\n    pass\n")
            run_cli(["build", "--root", str(root), "--quiet"])
            self.assertNotIn("hologram:start", (root / "AGENTS.md").read_text())
            self.assertIn("run2", (root / "CLAUDE.md").read_text())

    def test_target_all_restores_autodetect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._proj(Path(tmp))
            run_cli(["build", "--root", str(root), "--target", "CLAUDE.md",
                     "--quiet"])
            run_cli(["build", "--root", str(root), "--target", "all",
                     "--quiet"])
            self.assertIn("hologram:start", (root / "AGENTS.md").read_text())
            self.assertNotIn("· targets", (root / "CLAUDE.md").read_text())

    def test_unknown_and_ambiguous_targets_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._proj(Path(tmp))
            with self.assertRaises(SystemExit) as ctx:
                run_cli(["build", "--root", str(root), "--target", "NOPE.md",
                         "--quiet"])
            self.assertIn("unknown target", str(ctx.exception))
            with self.assertRaises(SystemExit) as ctx:
                run_cli(["build", "--root", str(root), "--target",
                         "hologram.md", "--quiet"])
            self.assertIn("ambiguous target", str(ctx.exception))

    def test_named_target_created_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            root.mkdir()
            (root / "app.py").write_text("def run():\n    pass\n")
            run_cli(["build", "--root", str(root), "--target", "AGENTS.md",
                     "--quiet"])
            self.assertIn("hologram:start", (root / "AGENTS.md").read_text())
            self.assertFalse((root / "CLAUDE.md").exists())


class LangFilterPersistenceTest(unittest.TestCase):
    """--lang is stamped into the map header and recalled by later commands."""

    def _proj(self, tmp: Path) -> Path:
        root = tmp / "p"
        root.mkdir()
        (root / "app.py").write_text("def visible():\n    pass\n")
        (root / "tool.sh").write_text("hidden() {\n  true\n}\n")
        return root

    def test_filter_stamped_recalled_and_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._proj(Path(tmp))
            run_cli(["build", "--root", str(root), "--lang", "python", "--quiet"])
            text = (root / "CLAUDE.md").read_text()
            self.assertIn("· langs python", text)
            self.assertNotIn("hidden", text)
            # check without --lang recalls the filter
            self.assertEqual(run_cli(["check", "--root", str(root),
                                      "--quiet"]), 0)
            # out-of-filter edits don't stale the map; in-filter edits do
            (root / "tool.sh").write_text("changed() {\n  true\n}\n")
            self.assertEqual(run_cli(["check", "--root", str(root),
                                      "--quiet"]), 0)
            (root / "app.py").write_text("def visible2():\n    pass\n")
            self.assertEqual(run_cli(["check", "--root", str(root),
                                      "--quiet"]), 1)
            # rebuild without --lang keeps the stored filter
            run_cli(["build", "--root", str(root), "--quiet"])
            text = (root / "CLAUDE.md").read_text()
            self.assertIn("· langs python", text)
            self.assertIn("visible2", text)
            self.assertNotIn("changed", text)

    @unittest.skipUnless(hologram.has_parser("bash"),
                         "tree-sitter-bash not installed")
    def test_lang_all_clears_stored_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._proj(Path(tmp))
            run_cli(["build", "--root", str(root), "--lang", "python", "--quiet"])
            run_cli(["build", "--root", str(root), "--lang", "all", "--quiet"])
            text = (root / "CLAUDE.md").read_text()
            self.assertNotIn("· langs", text)
            self.assertIn("hidden", text)


class BootstrapTest(unittest.TestCase):
    def test_missing_parser_langs_detects_gap(self):
        files = [JAVAMINI / "src/App.java", PYMINI_FILE]
        saved = hologram._PARSERS["java"]
        hologram._PARSERS["java"] = None
        try:
            self.assertEqual(hologram._missing_parser_langs(files), {"java"})
        finally:
            hologram._PARSERS["java"] = saved
        # python never needs a parser
        self.assertEqual(hologram._missing_parser_langs([PYMINI_FILE]), set())

    def test_cli_exits_with_instructions_when_bootstrap_exhausted(self):
        saved = hologram._PARSERS["java"]
        hologram._PARSERS["java"] = None
        os.environ["HOLOGRAM_BOOTSTRAPPED"] = "1"  # pretend re-exec already happened
        try:
            with tempfile.TemporaryDirectory() as tmp:
                proj = Path(tmp) / "proj"
                shutil.copytree(JAVAMINI, proj)
                with self.assertRaises(SystemExit) as ctx:
                    run_cli(["build", "--root", str(proj), "--quiet"])
            self.assertIn("pip install", str(ctx.exception))
            self.assertIn("tree-sitter-java", str(ctx.exception))
        finally:
            hologram._PARSERS["java"] = saved
            del os.environ["HOLOGRAM_BOOTSTRAPPED"]


@needs_java
class PrintCommandTest(unittest.TestCase):
    def test_print_writes_digest_to_stdout_and_touches_nothing(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            shutil.copytree(JAVAMINI, proj)
            before = sorted(p.relative_to(proj) for p in proj.rglob("*"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = run_cli(["print", "--root", str(proj), "--quiet"])
            self.assertEqual(code, 0)
            self.assertIn("PricingEngine", out.getvalue())
            self.assertIn("# hologram ·", out.getvalue())
            after = sorted(p.relative_to(proj) for p in proj.rglob("*"))
            self.assertEqual(before, after)  # no CLAUDE.md created, nothing embedded


@needs_java
class UninstallTest(unittest.TestCase):
    def test_uninstall_removes_hooks_and_blocks_preserving_prose(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            claude = repo / "CLAUDE.md"
            claude.write_text("# My notes\n\nHand-written guidance.\n")
            run_cli(["init", "--root", str(repo), "--quiet"])
            self.assertIn("hologram:start", claude.read_text())
            self.assertTrue((repo / ".git" / "hooks" / "post-commit").exists())
            run_cli(["uninstall", "--root", str(repo), "--quiet"])
            content = claude.read_text()
            self.assertNotIn("hologram:start", content)
            self.assertIn("Hand-written guidance.", content)
            self.assertFalse((repo / ".git" / "hooks" / "post-commit").exists())
            self.assertFalse((repo / ".git" / "hooks" / "pre-commit").exists())

    def test_uninstall_keeps_foreign_hook_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            hook = repo / ".git" / "hooks" / "post-commit"
            hook.write_text("#!/bin/sh\necho existing\n")
            run_cli(["init", "--root", str(repo), "--quiet"])
            run_cli(["uninstall", "--root", str(repo), "--quiet"])
            content = hook.read_text()
            self.assertIn("echo existing", content)
            self.assertNotIn("hologram:managed", content)

    def test_uninstall_deletes_managed_rule_dir_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            (repo / ".cursor" / "rules").mkdir(parents=True)
            run_cli(["build", "--root", str(repo), "--quiet"])
            managed = repo / ".cursor" / "rules" / "hologram.mdc"
            self.assertTrue(managed.exists())
            run_cli(["uninstall", "--root", str(repo), "--quiet"])
            self.assertFalse(managed.exists())

    def test_keep_blocks_limits_to_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            run_cli(["init", "--root", str(repo), "--quiet"])
            run_cli(["uninstall", "--root", str(repo), "--keep-blocks", "--quiet"])
            self.assertIn("hologram:start", (repo / "CLAUDE.md").read_text())
            self.assertFalse((repo / ".git" / "hooks" / "post-commit").exists())


@needs_java
class SizeWarningTest(unittest.TestCase):
    def test_warns_over_threshold_but_embeds_exactly(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            shutil.copytree(JAVAMINI, proj)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                run_cli(["build", "--root", str(proj), "--warn-tokens", "10",
                         "--quiet"])
            self.assertIn("warning", err.getvalue())
            self.assertIn("--lang", err.getvalue())
            embedded = hologram.embedded_digest(proj / "CLAUDE.md")
            self.assertIn("PricingEngine", embedded)  # embedded exactly, not cut

    def test_zero_disables_warning(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            shutil.copytree(JAVAMINI, proj)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                run_cli(["build", "--root", str(proj), "--warn-tokens", "0",
                         "--quiet"])
            self.assertEqual(err.getvalue(), "")


class HookPythonSelectionTest(unittest.TestCase):
    def test_hook_uses_tool_venv_python_when_present(self):
        from hologram import _hook_python
        tool_dir = Path(__file__).resolve().parents[1]
        venv_py = tool_dir / ".venv" / "bin" / "python"
        expected = str(venv_py) if venv_py.exists() else "python3"
        self.assertEqual(_hook_python(), expected)


if __name__ == "__main__":
    unittest.main()
