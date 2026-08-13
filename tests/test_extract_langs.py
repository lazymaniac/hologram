import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hologram  # noqa: E402
from hologram import extract_file  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PYMINI = FIXTURES / "pymini"
TSMINI = FIXTURES / "tsmini"

needs_ts = unittest.skipUnless(hologram.has_parser("typescript"),
                               "tree-sitter-typescript not installed")


class PythonExtractTest(unittest.TestCase):
    def test_classes_and_methods(self):
        syms = extract_file(PYMINI / "models.py", PYMINI)
        classes = {s.name for s in syms if s.kind == "class"}
        self.assertEqual(classes, {"UserId", "OrderId", "ItemId"})
        methods = [s for s in syms if s.kind == "method" and s.container == "UserId"]
        self.assertEqual([m.name for m in methods], ["check"])
        self.assertEqual(next(s for s in syms if s.name == "UserId").fields, ["value"])

    def test_function_signature_from_annotations(self):
        syms = extract_file(PYMINI / "app.py", PYMINI)
        fns = {s.name: s for s in syms if s.kind == "fn"}
        self.assertIn("price_order", fns)
        self.assertEqual(fns["price_order"].params, ["OrderId", "list[ItemId]"])
        self.assertEqual(fns["price_order"].param_names, ["order", "items"])
        self.assertEqual(fns["price_order"].returns, "int")


@needs_ts
class TypeScriptExtractTest(unittest.TestCase):
    def test_interface_class_function(self):
        syms = extract_file(TSMINI / "api.ts", TSMINI)
        by_kind = {}
        for s in syms:
            by_kind.setdefault(s.kind, set()).add(s.name)
        self.assertIn("Quote", by_kind.get("interface", set()))
        self.assertIn("PricingClient", by_kind.get("class", set()))
        self.assertIn("formatCents", by_kind.get("fn", set()))
        self.assertEqual(next(s for s in syms if s.name == "Quote").fields,
                         ["orderId", "totalCents"])
        self.assertEqual(next(s for s in syms if s.name == "PricingClient").fields,
                         ["baseUrl"])

    def test_method_and_returns(self):
        syms = extract_file(TSMINI / "api.ts", TSMINI)
        m = next(s for s in syms if s.name == "fetchQuote")
        self.assertEqual(m.kind, "method")
        self.assertEqual(m.container, "PricingClient")
        self.assertEqual(m.param_names, ["orderId"])
        self.assertEqual(m.returns, "Promise<Quote>")

    def test_exported_symbols_public(self):
        syms = extract_file(TSMINI / "api.ts", TSMINI)
        fn = next(s for s in syms if s.name == "formatCents")
        self.assertEqual(fn.visibility, "pub")


@needs_ts
class ArrowFunctionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = extract_file(TSMINI / "arrows.ts", TSMINI)

    def test_exported_arrow_is_public_fn(self):
        fn = next(s for s in self.syms if s.name == "fetchUser")
        self.assertEqual(fn.kind, "fn")
        self.assertEqual(fn.visibility, "pub")
        self.assertEqual(fn.params, ["string"])
        self.assertEqual(fn.param_names, ["id"])
        self.assertEqual(fn.returns, "Promise<string>")
        self.assertIn("lookup", fn.calls)

    def test_unexported_arrow_is_private(self):
        fn = next(s for s in self.syms if s.name == "lookup")
        self.assertEqual(fn.visibility, "priv")
        self.assertEqual(fn.returns, "string")

    def test_const_non_function_not_extracted(self):
        self.assertNotIn("cache", {s.name for s in self.syms})

    def test_class_field_arrow_is_method(self):
        m = next(s for s in self.syms if s.name == "onEvent")
        self.assertEqual(m.kind, "method")
        self.assertEqual(m.container, "EventHub")
        self.assertEqual(m.visibility, "pub")
        self.assertIn("dispatch", m.calls)

    def test_nested_closures_not_top_level(self):
        # the arrow body of fetchUser contains no declarators, but guard anyway:
        # every extracted fn must be fetchUser/lookup or an EventHub member
        names = {s.name for s in self.syms if s.kind == "fn"}
        self.assertEqual(names, {"fetchUser", "lookup"})


class DecoratorExtractTest(unittest.TestCase):
    """Extractors store every decorator verbatim; render applies the allowlist."""

    def _one(self, tmp, fname, source):
        p = Path(tmp) / fname
        p.write_text(source)
        return extract_file(p, Path(tmp))

    def test_python_decorators_captured(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            syms = self._one(tmp, "app.py", (
                "@dataclass\n"
                "class Order:\n    pass\n\n"
                '@app.route("/orders", methods=["POST"])\n'
                "def create():\n    pass\n"))
        order = next(s for s in syms if s.name == "Order")
        self.assertEqual(order.decorators, ["dataclass"])
        create = next(s for s in syms if s.name == "create")
        self.assertEqual(create.decorators,
                         ["app.route('/orders',methods=['POST'])"])

    @unittest.skipUnless(hologram.has_parser("java"), "tree-sitter-java missing")
    def test_java_annotations_captured(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            syms = self._one(tmp, "C.java", (
                "@RestController @RequestMapping(\"/api/v1\")\n"
                "public class C {\n"
                "  @GetMapping(\"/users/{id}\") public String find(long id)"
                " { return null; }\n"
                "}\n"))
        c = next(s for s in syms if s.kind == "class")
        self.assertEqual(c.decorators,
                         ["RestController", 'RequestMapping("/api/v1")'])
        find = next(s for s in syms if s.name == "find")
        self.assertEqual(find.decorators, ['GetMapping("/users/{id}")'])

    @needs_ts
    def test_ts_decorators_captured_for_class_and_method(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            syms = self._one(tmp, "c.ts", (
                "@Component({ selector: 'app-user' })\n"
                "export class UserComponent {\n"
                "  @HostListener('click')\n"
                "  onClick(): void {}\n"
                "}\n"))
        comp = next(s for s in syms if s.kind == "class")
        self.assertEqual(comp.decorators, ["Component({ selector: 'app-user' })"])
        click = next(s for s in syms if s.name == "onClick")
        self.assertEqual(click.decorators, ["HostListener('click')"])


if __name__ == "__main__":
    unittest.main()
