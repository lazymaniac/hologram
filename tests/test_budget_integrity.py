import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hologram.gather import _gather, _zero_usage_names  # noqa: E402
from hologram.render import (  # noqa: E402
    _MAX_LEVEL,
    _helper_class_ids,
    _resolved_project_calls,
    _total_loc,
    build_digest,
    build_digest_with_stats,
    estimate_tokens,
    render_simple,
)
from hologram.symbols import Symbol  # noqa: E402


def _budget_candidates(root: Path, budget: int) -> list[str]:
    files, symbols, file_tokens, usage_tokens, state = _gather(root, None)
    invariants = {
        "state": state,
        "zero_usage": _zero_usage_names(symbols, usage_tokens),
        "file_tokens": file_tokens,
        "budget": budget,
        "loc": _total_loc(files),
        "resolved": _resolved_project_calls(symbols),
        "helpers": _helper_class_ids(symbols, file_tokens),
    }
    return [render_simple(root, symbols, files, detail=level, **invariants)
            for level in range(_MAX_LEVEL + 1)]


class WholeMapBudgetSelectionTest(unittest.TestCase):
    def test_unlimited_and_generous_normal_builds_render_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("def run():\n    return 1\n")
            for budget in (None, 100_000):
                with self.subTest(budget=budget), patch(
                        "hologram.render.render_simple",
                        wraps=render_simple) as renderer:
                    build_digest(root, budget=budget)
                self.assertEqual(renderer.call_count, 1)

    def test_negative_library_budget_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("def run():\n    pass\n")
            with self.assertRaisesRegex(ValueError, "non-negative"):
                build_digest(root, budget=-1)

    def test_budget_70_does_not_grow_a_tiny_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # The budget stamp pushes L0 just over 70 estimated tokens. Loss
            # disclosures make every degraded candidate larger, especially L7.
            name = "x" * 180
            (root / "app.py").write_text(f"def {name}():\n    pass\n")
            candidates = _budget_candidates(root, 70)
            sizes = [estimate_tokens(candidate) for candidate in candidates]
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                selected = build_digest(root, budget=70)

        self.assertTrue(all(size > 70 for size in sizes))
        self.assertLess(sizes[0], sizes[-1])
        self.assertEqual(selected, candidates[min(
            range(len(candidates)), key=lambda level: (sizes[level], level))])
        self.assertNotIn(" L7", selected.splitlines()[0])
        self.assertIn("smallest complete candidate is L0", err.getvalue())

    def test_least_degraded_whole_candidate_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source: list[str] = []
            for index in range(30):
                source.extend([
                    f"def _private_helper_with_long_name_{index}():",
                    "    return 1",
                    "",
                ])
            source.extend([
                "def run():",
                "    return _private_helper_with_long_name_0()",
            ])
            (root / "app.py").write_text("\n".join(source))
            candidates = _budget_candidates(root, 90)
            sizes = [estimate_tokens(candidate) for candidate in candidates]
            with patch("hologram.render.render_simple",
                       wraps=render_simple) as renderer:
                selected, stats = build_digest_with_stats(root, budget=90)

        self.assertTrue(all(size > 90 for size in sizes[:3]))
        self.assertLessEqual(sizes[3], 90)
        self.assertLess(sizes[4], sizes[3])
        self.assertEqual(selected, candidates[3])
        self.assertEqual(stats.effective_detail, "L3")
        self.assertGreaterEqual(renderer.call_count, _MAX_LEVEL + 1)


class EntrypointFloorTest(unittest.TestCase):
    def test_framework_methods_survive_cold_and_skeleton_levels(self):
        symbols = [
            Symbol(name="Controller", kind="class", file="api.py", line=1,
                   visibility="pub", lang="python"),
            Symbol(name="orders", kind="method", file="api.py", line=2,
                   container="Controller", visibility="pub", lang="python",
                   decorators=['app.get("/orders")']),
            Symbol(name="refresh", kind="method", file="api.py", line=3,
                   container="Controller", visibility="pub", lang="python",
                   decorators=["EventListener"]),
            Symbol(name="ordinary", kind="method", file="api.py", line=4,
                   container="Controller", visibility="pub", lang="python"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            outputs = [render_simple(Path(tmp), symbols, [], detail=level,
                                     zero_usage={"Controller", "orders", "refresh",
                                                 "ordinary"})
                       for level in (5, 7)]

        for output in outputs:
            self.assertIn("Controller(C)", output)
            self.assertIn("orders() @GET/orders", output)
            self.assertIn("refresh() @EventListener", output)
            self.assertNotIn("ordinary()", output)
            self.assertNotIn("orders() ×0", output)
            self.assertNotIn("Controller(C) ×0", output)

    def test_make_targets_are_external_entrypoints_at_the_floor(self):
        symbols = [
            Symbol(name="Makefile", kind="class", file="Makefile", line=1,
                   visibility="pub", lang="make"),
            Symbol(name="deploy", kind="method", file="Makefile", line=2,
                   container="Makefile", visibility="pub", lang="make",
                   params=["ENV"], param_names=["ENV"]),
            Symbol(name="_internal", kind="method", file="Makefile", line=3,
                   container="Makefile", visibility="priv", lang="make"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output = render_simple(Path(tmp), symbols, [], detail=7,
                                   zero_usage={"Makefile", "deploy"})

        self.assertIn("Makefile(C)", output)
        self.assertIn("deploy(ENV)", output)
        self.assertNotIn("Makefile(C) ×0", output)
        self.assertNotIn("deploy(ENV) ×0", output)
        self.assertNotIn("_internal", output)

    def test_private_framework_entrypoints_survive_every_level(self):
        symbols = [
            Symbol(name="_Controller", kind="class", file="api.py", line=1,
                   visibility="priv", lang="python"),
            Symbol(name="_health", kind="method", file="api.py", line=2,
                   container="_Controller", visibility="priv", lang="python",
                   decorators=['app.get("/health")']),
            Symbol(name="_ready", kind="fn", file="ready.py", line=1,
                   visibility="priv", lang="python",
                   decorators=['app.get("/ready")']),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            outputs = [render_simple(Path(tmp), symbols, [], detail=level,
                                     zero_usage={"_Controller", "_health",
                                                 "_ready"})
                       for level in (0, 5, 7)]

        for output in outputs:
            self.assertIn('_Controller(C)', output)
            self.assertEqual(output.count('_Controller'), 1)
            self.assertNotIn('_Controller×0', output)
            self.assertIn('_health() @GET/health', output)
            self.assertIn('_ready() @GET/ready', output)
            self.assertNotIn('_health() ×0', output)

    def test_split_entrypoint_merges_decorator_and_definition_facts(self):
        symbols = [
            Symbol(name="Controller", kind="class", file="api.hpp", line=1,
                   visibility="pub", lang="cpp"),
            Symbol(name="route", kind="method", file="api.hpp", line=2,
                   signature="route()", container="Controller",
                   visibility="pub", lang="cpp",
                   decorators=['app.get("/x")']),
            Symbol(name="route", kind="method", file="api.cpp", line=2,
                   signature="route()", container="Controller",
                   visibility="pub", lang="cpp", calls=["helper"]),
            Symbol(name="helper", kind="fn", file="api.cpp", line=8,
                   visibility="pub", lang="cpp"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            full = render_simple(Path(tmp), symbols, [], detail=0,
                                 zero_usage={"Controller", "route"})
            cold = render_simple(Path(tmp), symbols, [], detail=5,
                                 zero_usage={"Controller", "route"})

        self.assertIn("route() @GET/x > helper", full)
        self.assertEqual(full.count("route()"), 1)
        self.assertIn("route() @GET/x", cold)
        self.assertNotIn("route() ×0", full + cold)

    def test_duplicate_private_top_level_routes_are_file_qualified(self):
        symbols = [
            Symbol(name="_route", kind="fn", file="a.py", line=1,
                   visibility="priv", lang="python",
                   decorators=['app.get("/a")']),
            Symbol(name="_route", kind="fn", file="b.py", line=1,
                   visibility="priv", lang="python",
                   decorators=['app.get("/b")']),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            output = render_simple(Path(tmp), symbols, [])

        self.assertIn("a.py:_route() @GET/a", output)
        self.assertIn("b.py:_route() @GET/b", output)

    def test_duplicate_private_route_owners_are_file_qualified(self):
        symbols: list[Symbol] = []
        for file, path in (("a.py", "/a"), ("b.py", "/b")):
            symbols.extend([
                Symbol(name="_Controller", kind="class", file=file, line=1,
                       visibility="priv", lang="python"),
                Symbol(name="_route", kind="method", file=file, line=2,
                       container="_Controller", visibility="priv",
                       lang="python", decorators=[f'app.get("{path}")']),
            ])

        with tempfile.TemporaryDirectory() as tmp:
            output = render_simple(Path(tmp), symbols, [])

        self.assertIn("a.py:_Controller(C)", output)
        self.assertIn("b.py:_Controller(C)", output)
        self.assertNotIn("_Controller,_Controller", output)


class OwnerIdentityTest(unittest.TestCase):
    def _symbols(self) -> list[Symbol]:
        return [
            Symbol(name="Client", kind="class", file="pkg/one.py", line=1,
                   visibility="pub", lang="python"),
            Symbol(name="alpha", kind="method", file="pkg/one.py", line=2,
                   container="Client", visibility="pub", lang="python"),
            Symbol(name="run", kind="method", file="pkg/one.py", line=3,
                   container="Client", visibility="pub", lang="python",
                   calls=["self.beta"]),
            Symbol(name="use_alpha", kind="fn", file="pkg/one.py", line=4,
                   visibility="pub", lang="python", calls=["Client.alpha"]),
            Symbol(name="Client", kind="class", file="pkg/two.py", line=1,
                   visibility="pub", lang="python"),
            Symbol(name="beta", kind="method", file="pkg/two.py", line=2,
                   container="Client", visibility="pub", lang="python"),
        ]

    def test_same_named_types_do_not_share_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = render_simple(Path(tmp), self._symbols(), [])

        self.assertIn("one.py:Client(C)", output)
        self.assertIn("two.py:Client(C)", output)
        lines = [line.strip() for line in output.splitlines()]
        self.assertEqual(lines.count("alpha()"), 1)
        self.assertEqual(lines.count("beta()"), 1)

    def test_coldness_is_file_qualified(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = render_simple(Path(tmp), self._symbols(), [], detail=5)

        self.assertIn("alpha()", output)
        self.assertNotIn("beta()", output)

    def test_same_named_types_do_not_lend_methods_across_files(self):
        symbols = self._symbols()
        run = next(symbol for symbol in symbols if symbol.name == "run")
        _, _, raw = _resolved_project_calls(symbols)

        self.assertEqual(raw[id(run)], [])

    def test_make_prerequisites_resolve_to_the_local_makefile(self):
        symbols: list[Symbol] = []
        deploys: list[Symbol] = []
        for file in ("a/Makefile", "b/Makefile"):
            symbols.append(Symbol(
                name="Makefile", kind="class", file=file, line=1,
                visibility="pub", lang="make"))
            symbols.append(Symbol(
                name="build", kind="method", file=file, line=2,
                container="Makefile", visibility="pub", lang="make"))
            deploy = Symbol(
                name="deploy", kind="method", file=file, line=3,
                container="Makefile", visibility="pub", lang="make",
                calls=["build"])
            symbols.append(deploy)
            deploys.append(deploy)

        _, _, raw = _resolved_project_calls(symbols)

        for deploy in deploys:
            self.assertEqual([target.file for target in raw[id(deploy)]],
                             [deploy.file])

    def test_split_go_receiver_attaches_to_unique_package_type(self):
        symbols = [
            Symbol(name="Widget", kind="class", file="pkg/widget.go", line=1,
                   visibility="pub", lang="go"),
            Symbol(name="Run", kind="method", file="pkg/run.go", line=1,
                   container="Widget", visibility="pub", lang="go"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            output = render_simple(Path(tmp), symbols, [])

        self.assertIn("Widget(C)", output)
        self.assertIn("Run()", output)

    def test_split_cpp_definition_keeps_its_call_chain(self):
        symbols = [
            Symbol(name="Widget", kind="class", file="widget.hpp", line=1,
                   visibility="pub", lang="cpp"),
            Symbol(name="run", kind="method", file="widget.hpp", line=2,
                   container="Widget", visibility="pub", lang="cpp"),
            Symbol(name="run", kind="method", file="widget.cpp", line=2,
                   container="Widget", visibility="pub", lang="cpp",
                   calls=["helper"]),
            Symbol(name="helper", kind="fn", file="widget.cpp", line=8,
                   visibility="pub", lang="cpp"),
            Symbol(name="invoke", kind="fn", file="main.cpp", line=1,
                   visibility="pub", lang="cpp", calls=["Widget.run"]),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            output = render_simple(Path(tmp), symbols, [])
        invoke = next(symbol for symbol in symbols if symbol.name == "invoke")
        _, _, raw = _resolved_project_calls(symbols)

        self.assertIn("run() > helper", output)
        self.assertEqual(output.count("run()"), 1)
        self.assertIn("invoke() > run", output)
        self.assertEqual([target.file for target in raw[id(invoke)]],
                         ["widget.cpp"])

    def test_split_go_resolution_stays_with_its_package(self):
        symbols: list[Symbol] = []
        for package in ("one", "two"):
            symbols.extend([
                Symbol(name="Widget", kind="class",
                       file=f"{package}/widget.go", line=1,
                       visibility="pub", lang="go"),
                Symbol(name="Run", kind="method", file=f"{package}/run.go",
                       line=1, container="Widget", visibility="pub",
                       lang="go"),
            ])
        caller = Symbol(name="use", kind="fn", file="one/use.go", line=1,
                        visibility="pub", lang="go", calls=["w.Run"],
                        bindings={"w": "Widget"})
        symbols.append(caller)

        displayed, _, raw = _resolved_project_calls(symbols)
        with tempfile.TemporaryDirectory() as tmp:
            output = render_simple(Path(tmp), symbols, [])

        self.assertEqual([target.file for target in raw[id(caller)]],
                         ["one/run.go"])
        self.assertEqual(displayed[id(caller)], ["one/run.go:Widget.Run"])
        self.assertNotIn("one/widget.go:Widget", output)

    def test_bash_calls_resolve_within_each_script_owner(self):
        symbols: list[Symbol] = []
        deploys: list[Symbol] = []
        for file in ("one.sh", "two.sh"):
            owner = Path(file).name
            symbols.extend([
                Symbol(name=owner, kind="class", file=file, line=1,
                       visibility="pub", lang="bash"),
                Symbol(name="build", kind="method", file=file, line=2,
                       container=owner, visibility="pub", lang="bash"),
            ])
            deploy = Symbol(name="deploy", kind="method", file=file, line=3,
                            container=owner, visibility="pub", lang="bash",
                            calls=["build"])
            symbols.append(deploy)
            deploys.append(deploy)

        _, _, raw = _resolved_project_calls(symbols)

        for deploy in deploys:
            self.assertEqual([target.file for target in raw[id(deploy)]],
                             [deploy.file])

    def test_make_under_test_directory_remains_production_entrypoint(self):
        symbols = [
            Symbol(name="Makefile", kind="class", file="tests/Makefile",
                   line=1, visibility="pub", lang="make"),
            Symbol(name="integration", kind="method", file="tests/Makefile",
                   line=2, container="Makefile", visibility="pub",
                   lang="make", params=["DB_URL"], param_names=["DB_URL"]),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            makefile = root / "tests" / "Makefile"
            makefile.parent.mkdir()
            makefile.write_text("integration: ; echo $(DB_URL)\n")
            output = render_simple(root, symbols, [makefile])

        self.assertIn("integration(DB_URL)", output)
        self.assertNotIn("*Makefile", output)


class MeaningfulDunderTest(unittest.TestCase):
    def test_protocol_dunders_remain_in_private_inventory(self):
        symbols = [
            Symbol(name="Resource", kind="class", file="resource.py", line=1,
                   visibility="pub", lang="python"),
        ]
        for line, name in enumerate(
                ("__init__", "__repr__", "__iter__", "__enter__"), start=2):
            symbols.append(Symbol(
                name=name, kind="method", file="resource.py", line=line,
                container="Resource", visibility="priv", lang="python"))

        with tempfile.TemporaryDirectory() as tmp:
            output = render_simple(Path(tmp), symbols, [])

        self.assertIn("__iter__", output)
        self.assertIn("__enter__", output)
        self.assertNotIn("__init__", output)
        self.assertNotIn("__repr__", output)

    def test_orphan_private_member_keeps_file_scoped_inventory(self):
        symbols = [Symbol(name="_hidden", kind="method", file="mod.lua", line=1,
                          container="M", visibility="priv", lang="lua")]

        with tempfile.TemporaryDirectory() as tmp:
            output = render_simple(Path(tmp), symbols, [])

        self.assertIn("- M: _hidden", output)

    def test_repeated_orphan_module_owners_are_file_qualified(self):
        symbols: list[Symbol] = []
        for file in ("a.lua", "b.lua"):
            symbols.extend([
                Symbol(name="quote", kind="method", file=file, line=1,
                       container="M", visibility="pub", lang="lua"),
                Symbol(name="_hidden", kind="method", file=file, line=2,
                       container="M", visibility="priv", lang="lua"),
            ])

        with tempfile.TemporaryDirectory() as tmp:
            output = render_simple(Path(tmp), symbols, [])

        for file in ("a.lua", "b.lua"):
            self.assertIn(f"{file}:M: quote()", output)
            self.assertIn(f"- {file}:M: _hidden", output)

    def test_orphan_module_qualified_calls_keep_their_chain(self):
        symbols = [
            Symbol(name="start", kind="method", file="app.lua", line=1,
                   container="M", visibility="pub", lang="lua",
                   calls=["M.finish"]),
            Symbol(name="finish", kind="method", file="app.lua", line=2,
                   container="M", visibility="pub", lang="lua"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            output = render_simple(Path(tmp), symbols, [])

        self.assertIn("start() > finish", output)


class ConstructorIntegrityTest(unittest.TestCase):
    def test_only_record_constructor_is_structurally_redundant(self):
        symbols = [
            Symbol(name="Engine", kind="class", file="engine.java", line=1,
                   visibility="pub", lang="java", fields=["price"]),
            Symbol(name="Engine", kind="ctor", file="engine.java", line=2,
                   container="Engine", visibility="pub", lang="java",
                   params=["price"], param_names=["price"]),
            Symbol(name="Price", kind="record", file="price.java", line=1,
                   visibility="pub", lang="java", fields=["value"]),
            Symbol(name="Price", kind="ctor", file="price.java", line=2,
                   container="Price", visibility="pub", lang="java",
                   params=["value"], param_names=["value"]),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            output = render_simple(Path(tmp), symbols, [])

        self.assertIn("Engine(price)", output)
        self.assertNotIn("Price(value)", output)


if __name__ == "__main__":
    unittest.main()
