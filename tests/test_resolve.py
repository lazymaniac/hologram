from __future__ import annotations

import hashlib
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import hologram.resolve as resolver_module
from hologram.model import (
    Binding,
    CallKind,
    CallRef,
    FileIR,
    ImportRef,
    Language,
    ProjectIR,
    ReferenceConfidence,
    ReferenceContext,
    ReferenceKind,
    ReferenceRef,
    SourceFile,
    SourceRole,
    SourceSpan,
    Symbol,
    SymbolId,
    SymbolKind,
    Visibility,
)
from hologram.parsers.api import extract_project
from hologram.resolve import (
    UNKNOWN_TYPE_KEY,
    ResolutionResult,
    ResolutionStatus,
    ResolvedCall,
    ResolvedImport,
    ResolvedReference,
    canonical_type_key,
    resolve_project,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "resolution"
CALLABLE_KINDS = {
    SymbolKind.FUNCTION,
    SymbolKind.METHOD,
    SymbolKind.CONSTRUCTOR,
}


def source(file: str, language: Language, raw: bytes = b"\n") -> SourceFile:
    return SourceFile(
        Path("/repo") / file,
        file,
        language,
        SourceRole.PRODUCTION,
        raw,
        hashlib.sha256(raw).hexdigest(),
    )


def span(file: str, line: int = 1, column: int = 0) -> SourceSpan:
    return SourceSpan(file, line, column, line, column + 1)


def symbol(
    file: str,
    language: Language,
    name: str,
    *,
    kind: SymbolKind = SymbolKind.FUNCTION,
    container: tuple[str, ...] = (),
    params: tuple[str, ...] = (),
    supers: tuple[str, ...] = (),
    bindings: tuple[Binding, ...] = (),
    visibility: Visibility = Visibility.PUBLIC,
    line: int = 1,
) -> Symbol:
    signature_key = f"({','.join(params)})" if kind in CALLABLE_KINDS else ""
    identifier = SymbolId(language, file, container, kind, name, signature_key)
    return Symbol(
        identifier,
        span(file, line),
        visibility,
        name,
        params=params,
        supers=supers,
        bindings=bindings,
    )


def file_ir(
    file: str,
    language: Language,
    *,
    module: str | None,
    symbols: tuple[Symbol, ...] = (),
    calls: tuple[CallRef, ...] = (),
    imports: tuple[ImportRef, ...] = (),
    references: tuple[ReferenceRef, ...] = (),
) -> FileIR:
    return FileIR(
        source(file, language),
        module=module,
        symbols=symbols,
        calls=calls,
        imports=imports,
        references=references,
    )


def project(*files: FileIR) -> ProjectIR:
    return ProjectIR(Path("/repo"), files, (), True)


def call(
    caller: Symbol,
    name: str,
    *,
    receiver: str | None = None,
    kind: CallKind = CallKind.CALL,
    arity: int | None = 0,
    line: int = 10,
) -> CallRef:
    return CallRef(caller.id, span(caller.file, line), name, receiver, kind, arity)


def reference(
    file: str,
    name: str,
    *,
    owner: Symbol | None = None,
    qualifier: str | None = None,
    kind: ReferenceKind = ReferenceKind.NAME,
    context: ReferenceContext = ReferenceContext.CODE,
    confidence: ReferenceConfidence = ReferenceConfidence.DEFINITE,
    line: int = 10,
) -> ReferenceRef:
    return ReferenceRef(
        owner.id if owner is not None else None,
        span(file, line),
        name,
        qualifier,
        kind,
        context,
        confidence,
    )


def assert_cardinality(
    test: unittest.TestCase, raw: ProjectIR, result: ResolutionResult
) -> None:
    test.assertEqual(len(result.imports), sum(len(file.imports) for file in raw.files))
    test.assertEqual(len(result.calls), sum(len(file.calls) for file in raw.files))
    test.assertEqual(
        len(result.references),
        sum(len(file.references) for file in raw.files),
    )


class ResolutionRecordTest(unittest.TestCase):
    def test_public_records_are_exact_frozen_slotted_values(self) -> None:
        expected = {
            ResolvedImport: (
                "source_file",
                "fact",
                "status",
                "target_files",
                "target_symbols",
            ),
            ResolvedCall: ("fact", "status", "target", "candidates", "display_name"),
            ResolvedReference: ("fact", "status", "target", "candidates"),
            ResolutionResult: ("imports", "calls", "references", "diagnostics"),
        }
        for record, fields in expected.items():
            self.assertEqual(tuple(record.__dataclass_fields__), fields)
            self.assertEqual(record.__slots__, fields)

        owner = symbol("app.py", Language.PYTHON, "run")
        raw = call(owner, "missing")
        item = ResolvedCall(
            raw,
            ResolutionStatus.UNRESOLVED,
            None,
            (),
            None,
        )
        with self.assertRaises(FrozenInstanceError):
            item.status = ResolutionStatus.RESOLVED  # type: ignore[misc]
        self.assertFalse(hasattr(item, "__dict__"))

    def test_empty_project_has_no_diagnostics(self) -> None:
        result = resolve_project(project())
        self.assertEqual(result, ResolutionResult((), (), (), ()))

    def test_root_exports_are_lazy_and_records_own_caller_lists(self) -> None:
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys, hologram; "
                    "assert 'hologram.resolve' not in sys.modules; "
                    "assert hologram.resolve_project; "
                    "assert 'hologram.resolve' in sys.modules"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(child.returncode, 0, child.stderr)

        owner = symbol("app.py", Language.PYTHON, "run")
        raw = call(owner, "missing")
        candidates: list[SymbolId] = []
        item = ResolvedCall(
            raw,
            ResolutionStatus.UNRESOLVED,
            None,
            candidates,
            None,
        )
        candidates.append(owner.id)
        self.assertEqual(item.candidates, ())


class CanonicalTypeKeyTest(unittest.TestCase):
    def test_unknown_spellings_share_one_frozen_sentinel(self) -> None:
        self.assertEqual(UNKNOWN_TYPE_KEY, "<?>")
        for value in (None, "", "   ", "?", "<?>\n"):
            self.assertEqual(canonical_type_key(value), UNKNOWN_TYPE_KEY)

    def test_generic_whitespace_and_punctuation_are_stable(self) -> None:
        expected = "Map<String,List<int?>>"
        values = (
            " Map < String , List < int ? > > ",
            "Map<String,List<int?>>",
            "Map< String,List < int ? > >",
        )
        for value in values:
            normalized = canonical_type_key(value)
            self.assertEqual(normalized, expected)
            self.assertEqual(canonical_type_key(normalized), normalized)

    def test_real_top_and_bottom_type_names_are_not_unknown(self) -> None:
        keys = {canonical_type_key(value) for value in ("Any", "Object", "void")}
        self.assertEqual(keys, {"Any", "Object", "void"})
        self.assertNotIn(UNKNOWN_TYPE_KEY, keys)


class ImportResolutionTest(unittest.TestCase):
    def test_python_relative_import_and_init_module_are_exact(self) -> None:
        helper = symbol("pkg/__init__.py", Language.PYTHON, "helper")
        client = symbol("pkg/sub/client.py", Language.PYTHON, "run")
        imports = (
            ImportRef(span(client.file, 2), "...", "helper", None),
            ImportRef(span(client.file, 3), "..api", "fetch", "load"),
        )
        fetch = symbol("pkg/api.py", Language.PYTHON, "fetch")
        raw = project(
            file_ir(
                client.file,
                Language.PYTHON,
                module="pkg.sub.client",
                symbols=(client,),
                imports=imports,
            ),
            file_ir(helper.file, Language.PYTHON, module="pkg", symbols=(helper,)),
            file_ir(fetch.file, Language.PYTHON, module="pkg.api", symbols=(fetch,)),
        )
        result = resolve_project(raw)
        self.assertEqual(
            [item.status for item in result.imports],
            [ResolutionStatus.EXTERNAL, ResolutionStatus.RESOLVED],
        )
        self.assertEqual(result.imports[1].target_symbols, (fetch.id,))
        assert_cardinality(self, raw, result)

    def test_typescript_relative_path_does_not_guess_suffix_case_or_extension(
        self,
    ) -> None:
        fetch = symbol("lib/api.ts", Language.TYPESCRIPT, "fetch")
        owner = symbol("lib/app.ts", Language.TYPESCRIPT, "run")
        imports = tuple(
            ImportRef(span(owner.file, line), module, "fetch", None)
            for line, module in enumerate(("./api", "api", "./API", "./api.js"), 2)
        )
        raw = project(
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="lib/app",
                symbols=(owner,),
                imports=imports,
            ),
            file_ir(
                fetch.file, Language.TYPESCRIPT, module="lib/api", symbols=(fetch,)
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(
            [item.status for item in result.imports],
            [
                ResolutionStatus.RESOLVED,
                ResolutionStatus.EXTERNAL,
                ResolutionStatus.EXTERNAL,
                ResolutionStatus.EXTERNAL,
            ],
        )

    def test_typescript_exact_extension_and_named_private_import_are_explicit(
        self,
    ) -> None:
        private = symbol(
            "lib/api.ts",
            Language.TYPESCRIPT,
            "privateFetch",
            visibility=Visibility.PRIVATE,
        )
        owner = symbol("lib/app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="lib/app",
                symbols=(owner,),
                imports=(
                    ImportRef(
                        span(owner.file, 2),
                        "./api.ts",
                        "privateFetch",
                        None,
                    ),
                ),
                calls=(call(owner, "privateFetch"),),
            ),
            file_ir(
                private.file,
                Language.TYPESCRIPT,
                module="lib/api",
                symbols=(private,),
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(result.imports[0].status, ResolutionStatus.RESOLVED)
        self.assertEqual(result.imports[0].target_symbols, (private.id,))
        self.assertEqual(result.calls[0].target, private.id)

    def test_explicit_extension_does_not_collide_with_longer_filename_stem(
        self,
    ) -> None:
        cases = (
            (Language.TYPESCRIPT, "api.ts", "api.ts.js", "./api.ts"),
            (Language.C, "api.h", "api.h.c", "api.h"),
        )
        for language, exact_file, longer_file, imported in cases:
            with self.subTest(language=language):
                exact = symbol(exact_file, language, "fetch")
                decoy = symbol(longer_file, language, "fetch")
                owner_file = "app.ts" if language is Language.TYPESCRIPT else "main.c"
                owner = symbol(owner_file, language, "run")
                raw = project(
                    file_ir(exact.file, language, module=None, symbols=(exact,)),
                    file_ir(decoy.file, language, module=None, symbols=(decoy,)),
                    file_ir(
                        owner.file,
                        language,
                        module=None,
                        symbols=(owner,),
                        imports=(
                            ImportRef(
                                span(owner.file, 2),
                                imported,
                                None,
                                "api",
                            ),
                        ),
                    ),
                )
                resolved = resolve_project(raw).imports[-1]
                self.assertEqual(resolved.status, ResolutionStatus.RESOLVED)
                self.assertEqual(resolved.target_files, (exact.file,))

    def test_java_static_import_resolves_member_of_exact_type(self) -> None:
        client = symbol(
            "shop/engine/Client.java", Language.JAVA, "Client", kind=SymbolKind.CLASS
        )
        fetch = symbol(
            client.file,
            Language.JAVA,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        owner = symbol(
            "shop/app/App.java", Language.JAVA, "main", kind=SymbolKind.METHOD
        )
        imported = ImportRef(
            span(owner.file, 2),
            "shop.engine.Client",
            "fetch",
            None,
        )
        raw = project(
            file_ir(
                owner.file,
                Language.JAVA,
                module="shop.app",
                symbols=(owner,),
                calls=(call(owner, "fetch"),),
                imports=(imported,),
            ),
            file_ir(
                client.file,
                Language.JAVA,
                module="shop.engine",
                symbols=(client, fetch),
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(result.imports[0].status, ResolutionStatus.RESOLVED)
        self.assertEqual(result.imports[0].target_symbols, (fetch.id,))
        self.assertEqual(result.calls[0].target, fetch.id)

    def test_named_static_import_can_reach_nonpublic_project_type(self) -> None:
        client = symbol(
            "shop/engine/Client.java",
            Language.JAVA,
            "Client",
            kind=SymbolKind.CLASS,
            visibility=Visibility.INTERNAL,
        )
        fetch = symbol(
            client.file,
            Language.JAVA,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        owner = symbol(
            "shop/engine/App.java",
            Language.JAVA,
            "main",
            kind=SymbolKind.METHOD,
        )
        raw = project(
            file_ir(
                owner.file,
                Language.JAVA,
                module="shop.engine",
                symbols=(owner,),
                imports=(
                    ImportRef(
                        span(owner.file, 2),
                        "shop.engine.Client",
                        "fetch",
                        None,
                    ),
                ),
                calls=(call(owner, "fetch"),),
            ),
            file_ir(
                client.file,
                Language.JAVA,
                module="shop.engine",
                symbols=(client, fetch),
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(result.imports[0].target_symbols, (fetch.id,))
        self.assertEqual(result.calls[0].target, fetch.id)

    def test_nested_static_type_and_rust_crate_type_keys_keep_full_paths(self) -> None:
        cases = (
            (
                Language.JAVA,
                "shop/Outer.java",
                "shop",
                ("Outer",),
                "Inner",
                "shop.Outer.Inner",
            ),
            (
                Language.RUST,
                "src/lib.rs",
                "src/lib",
                (),
                "Client",
                "crate::Client",
            ),
        )
        for language, target_file, module, container, type_name, imported in cases:
            with self.subTest(language=language):
                type_ = symbol(
                    target_file,
                    language,
                    type_name,
                    kind=SymbolKind.CLASS,
                    container=container,
                )
                fetch = symbol(
                    target_file,
                    language,
                    "fetch",
                    kind=SymbolKind.METHOD,
                    container=(*container, type_name),
                )
                owner_file = (
                    "shop/App.java" if language is Language.JAVA else "src/app.rs"
                )
                owner = symbol(owner_file, language, "run")
                raw = project(
                    file_ir(
                        target_file,
                        language,
                        module=module,
                        symbols=(type_, fetch),
                    ),
                    file_ir(
                        owner.file,
                        language,
                        module=None,
                        symbols=(owner,),
                        imports=(
                            ImportRef(span(owner.file, 2), imported, "fetch", None),
                        ),
                        calls=(call(owner, "fetch"),),
                    ),
                )
                result = resolve_project(raw)
                self.assertEqual(result.imports[-1].target_symbols, (fetch.id,))
                self.assertEqual(result.calls[0].target, fetch.id)

    def test_named_module_alias_is_a_namespace_receiver(self) -> None:
        module = symbol(
            "src/lib.rs",
            Language.RUST,
            "net",
            kind=SymbolKind.MODULE,
        )
        fetch = symbol("src/net.rs", Language.RUST, "fetch")
        owner = symbol("src/app.rs", Language.RUST, "run")
        raw = project(
            file_ir("src/lib.rs", Language.RUST, module=None, symbols=(module,)),
            file_ir("src/net.rs", Language.RUST, module=None, symbols=(fetch,)),
            file_ir(
                owner.file,
                Language.RUST,
                module=None,
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "crate", "net", "api"),),
                calls=(call(owner, "fetch", receiver="api"),),
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(result.imports[-1].target_symbols, (module.id,))
        self.assertEqual(result.calls[0].target, fetch.id)

    def test_python_named_child_module_is_a_namespace_alias(self) -> None:
        package = symbol(
            "pkg/__init__.py",
            Language.PYTHON,
            "pkg",
            kind=SymbolKind.MODULE,
        )
        child_module = symbol(
            "pkg/sub.py",
            Language.PYTHON,
            "pkg.sub",
            kind=SymbolKind.MODULE,
        )
        fetch = symbol("pkg/sub.py", Language.PYTHON, "fetch")
        owner = symbol("app.py", Language.PYTHON, "run")
        raw = project(
            file_ir(
                package.file,
                Language.PYTHON,
                module="pkg",
                symbols=(package,),
            ),
            file_ir(
                child_module.file,
                Language.PYTHON,
                module="pkg.sub",
                symbols=(child_module, fetch),
            ),
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "pkg", "sub", None),),
                calls=(call(owner, "fetch", receiver="sub"),),
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(result.imports[-1].target_files, (child_module.file,))
        self.assertEqual(result.imports[-1].target_symbols, (child_module.id,))
        self.assertEqual(result.calls[0].target, fetch.id)

    def test_python_dotted_module_import_keeps_full_qualifier_path(self) -> None:
        module = symbol(
            "a/client.py",
            Language.PYTHON,
            "a.client",
            kind=SymbolKind.MODULE,
        )
        fetch = symbol(module.file, Language.PYTHON, "fetch")
        owner = symbol("app.py", Language.PYTHON, "run")
        raw = project(
            file_ir(
                module.file,
                Language.PYTHON,
                module="a.client",
                symbols=(module, fetch),
            ),
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "a.client", None, None),),
                calls=(
                    call(owner, "fetch", receiver="a.client"),
                    call(owner, "fetch", receiver="a", line=11),
                ),
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(result.calls[0].target, fetch.id)
        self.assertEqual(result.calls[1].status, ResolutionStatus.UNRESOLVED)

        external_owner = symbol("external.py", Language.PYTHON, "run")
        external = project(
            file_ir(
                external_owner.file,
                Language.PYTHON,
                module="external",
                symbols=(external_owner,),
                imports=(
                    ImportRef(
                        span(external_owner.file, 2),
                        "third.client",
                        None,
                        None,
                    ),
                ),
                calls=(call(external_owner, "fetch", receiver="third.client"),),
            )
        )
        self.assertEqual(
            resolve_project(external).calls[0].status,
            ResolutionStatus.EXTERNAL,
        )

    def test_external_wildcard_blocks_unrelated_family_fallback(self) -> None:
        decoy = symbol("other.py", Language.PYTHON, "fetch")
        owner = symbol("app.py", Language.PYTHON, "run")
        raw = project(
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                imports=(
                    ImportRef(
                        span(owner.file, 2),
                        "third_party",
                        None,
                        None,
                        wildcard=True,
                    ),
                ),
                calls=(call(owner, "fetch"),),
            ),
            file_ir(decoy.file, Language.PYTHON, module="other", symbols=(decoy,)),
        )
        result = resolve_project(raw)
        self.assertEqual(result.imports[0].status, ResolutionStatus.EXTERNAL)
        self.assertEqual(result.calls[0].status, ResolutionStatus.EXTERNAL)

    def test_c_include_is_an_open_scope_before_family_fallback(self) -> None:
        included = symbol("src/api.h", Language.C, "fetch")
        decoy = symbol("other.c", Language.C, "fetch")
        owner = symbol("src/main.c", Language.C, "run")
        raw = project(
            file_ir(
                owner.file,
                Language.C,
                module=None,
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "src/api.h", None, None),),
                calls=(call(owner, "fetch"),),
            ),
            file_ir(included.file, Language.C, module=None, symbols=(included,)),
            file_ir(decoy.file, Language.C, module=None, symbols=(decoy,)),
        )
        self.assertEqual(resolve_project(raw).calls[0].target, included.id)

    def test_c_relative_include_prefers_the_source_directory_over_project_root(
        self,
    ) -> None:
        root_fetch = symbol("api.h", Language.C, "fetch")
        local_fetch = symbol("src/api.h", Language.C, "fetch")
        owner = symbol("src/main.c", Language.C, "run")
        raw = project(
            file_ir(root_fetch.file, Language.C, module=None, symbols=(root_fetch,)),
            file_ir(local_fetch.file, Language.C, module=None, symbols=(local_fetch,)),
            file_ir(
                owner.file,
                Language.C,
                module=None,
                symbols=(owner,),
                imports=(
                    ImportRef(span(owner.file, 2), "./api.h", None, None),
                ),
                calls=(call(owner, "fetch"),),
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(result.imports[0].target_files, (local_fetch.file,))
        self.assertEqual(result.calls[0].target, local_fetch.id)

    def test_ambiguous_namespace_import_cannot_be_upgraded_by_one_member(self) -> None:
        fetch = symbol("api.ts", Language.TYPESCRIPT, "fetch")
        decoy = symbol("api.js", Language.JAVASCRIPT, "other")
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir(fetch.file, Language.TYPESCRIPT, module="api", symbols=(fetch,)),
            file_ir(decoy.file, Language.JAVASCRIPT, module="api", symbols=(decoy,)),
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(
                    ImportRef(
                        span(owner.file, 2),
                        "./api",
                        None,
                        "api",
                        wildcard=True,
                    ),
                ),
                calls=(call(owner, "fetch", receiver="api"),),
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(result.imports[0].status, ResolutionStatus.AMBIGUOUS)
        self.assertEqual(result.calls[0].status, ResolutionStatus.AMBIGUOUS)
        self.assertEqual(result.calls[0].candidates, (fetch.id,))

    def test_rust_inline_module_uses_crate_qualified_namespace(self) -> None:
        module = symbol(
            "src/lib.rs",
            Language.RUST,
            "net",
            kind=SymbolKind.MODULE,
        )
        fetch = symbol(
            module.file,
            Language.RUST,
            "fetch",
            container=("net",),
        )
        owner = symbol("src/app.rs", Language.RUST, "run")
        raw = project(
            file_ir(
                module.file,
                Language.RUST,
                module="src/lib",
                symbols=(module, fetch),
            ),
            file_ir(
                owner.file,
                Language.RUST,
                module="src/app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "crate::net", "fetch", None),),
                calls=(call(owner, "fetch"),),
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(result.imports[-1].target_symbols, (fetch.id,))
        self.assertEqual(result.calls[0].target, fetch.id)

    def test_go_path_c_include_lua_dots_and_rust_roots_are_explicit(self) -> None:
        cases = (
            (
                Language.GO,
                "cmd/app/main.go",
                "client",
                "example.com/lib",
                "example.com/lib/value.go",
                "lib",
            ),
            (
                Language.C,
                "src/main.c",
                None,
                "include/api.h",
                "include/api.h",
                None,
            ),
            (
                Language.LUA,
                "pkg/app.lua",
                "app",
                "pkg.util",
                "pkg/util.lua",
                "pkg.util",
            ),
            (
                Language.RUST,
                "crate/client.rs",
                "crate/client",
                "crate::net",
                "crate/net.rs",
                "crate/net",
            ),
        )
        for (
            language,
            caller_file,
            caller_module,
            imported,
            target_file,
            target_module,
        ) in cases:
            with self.subTest(language=language):
                owner = symbol(caller_file, language, "run")
                target = symbol(target_file, language, "item")
                raw = project(
                    file_ir(
                        caller_file,
                        language,
                        module=caller_module,
                        symbols=(owner,),
                        imports=(
                            ImportRef(span(caller_file, 2), imported, None, "dep"),
                        ),
                    ),
                    file_ir(
                        target_file, language, module=target_module, symbols=(target,)
                    ),
                )
                self.assertEqual(
                    resolve_project(raw).imports[0].status,
                    ResolutionStatus.RESOLVED,
                )

    def test_wildcard_keeps_only_public_targets_without_becoming_ambiguous(
        self,
    ) -> None:
        visible = symbol("lib.py", Language.PYTHON, "visible")
        hidden = symbol(
            "lib.py",
            Language.PYTHON,
            "hidden",
            visibility=Visibility.PRIVATE,
        )
        owner = symbol("app.py", Language.PYTHON, "run")
        imported = ImportRef(span(owner.file, 2), "lib", None, None, True)
        raw = project(
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                imports=(imported,),
            ),
            file_ir("lib.py", Language.PYTHON, module="lib", symbols=(hidden, visible)),
        )
        result = resolve_project(raw).imports[0]
        self.assertEqual(result.status, ResolutionStatus.RESOLVED)
        self.assertEqual(result.target_files, ("lib.py",))
        self.assertEqual(result.target_symbols, (visible.id,))

    def test_exact_project_module_missing_name_is_unresolved_not_external(self) -> None:
        owner = symbol("app.py", Language.PYTHON, "run")
        raw = project(
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "lib", "absent", None),),
            ),
            file_ir("lib.py", Language.PYTHON, module="lib"),
        )
        item = resolve_project(raw).imports[0]
        self.assertEqual(item.status, ResolutionStatus.UNRESOLVED)
        self.assertEqual(item.target_files, ("lib.py",))
        self.assertEqual(item.target_symbols, ())

    def test_relative_roots_do_not_guess_index_or_clamp_beyond_package(self) -> None:
        fetch = symbol("lib/index.ts", Language.TYPESCRIPT, "fetch")
        ts_owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        helper = symbol("helper.py", Language.PYTHON, "helper")
        py_owner = symbol("pkg/client.py", Language.PYTHON, "run")
        raw = project(
            file_ir(
                fetch.file, Language.TYPESCRIPT, module="lib/index", symbols=(fetch,)
            ),
            file_ir(
                ts_owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(ts_owner,),
                imports=(ImportRef(span(ts_owner.file, 2), "./lib", "fetch", None),),
            ),
            file_ir(helper.file, Language.PYTHON, module="helper", symbols=(helper,)),
            file_ir(
                py_owner.file,
                Language.PYTHON,
                module="pkg.client",
                symbols=(py_owner,),
                imports=(
                    ImportRef(span(py_owner.file, 2), "...helper", "helper", None),
                ),
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(
            [item.status for item in result.imports],
            [ResolutionStatus.EXTERNAL, ResolutionStatus.EXTERNAL],
        )

    def test_rust_crate_super_and_go_default_alias_are_syntax_exact(self) -> None:
        rust_cases = (
            ("src/app.rs", "crate::net", "src/net.rs"),
            ("src/a/client.rs", "super::net", "src/a/net.rs"),
        )
        for caller_file, imported, target_file in rust_cases:
            with self.subTest(imported=imported):
                owner = symbol(caller_file, Language.RUST, "run")
                target = symbol(target_file, Language.RUST, "item")
                raw = project(
                    file_ir(
                        caller_file,
                        Language.RUST,
                        module=None,
                        symbols=(owner,),
                        imports=(
                            ImportRef(span(caller_file, 2), imported, None, "dep"),
                        ),
                    ),
                    file_ir(target_file, Language.RUST, module=None, symbols=(target,)),
                )
                self.assertEqual(
                    resolve_project(raw).imports[0].status,
                    ResolutionStatus.RESOLVED,
                )

        owner = symbol("cmd/main.go", Language.GO, "run")
        target = symbol("example.com/lib/lib.go", Language.GO, "Fetch")
        go_project = project(
            file_ir(
                owner.file,
                Language.GO,
                module="main",
                symbols=(owner,),
                imports=(
                    ImportRef(span(owner.file, 2), "example.com/lib", None, None),
                ),
                calls=(call(owner, "Fetch", receiver="lib"),),
            ),
            file_ir(target.file, Language.GO, module="lib", symbols=(target,)),
        )
        self.assertEqual(resolve_project(go_project).calls[0].target, target.id)

    def test_ambiguous_modules_sort_files_and_wildcards_export_only_public(
        self,
    ) -> None:
        owner = symbol("app.py", Language.PYTHON, "run")
        left = symbol("z.py", Language.PYTHON, "left")
        right = symbol("a.py", Language.PYTHON, "right")
        internal = symbol(
            "a.py",
            Language.PYTHON,
            "internal",
            visibility=Visibility.INTERNAL,
        )
        raw = project(
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "lib", None, None, True),),
            ),
            file_ir(left.file, Language.PYTHON, module="lib", symbols=(left,)),
            file_ir(
                right.file, Language.PYTHON, module="lib", symbols=(right, internal)
            ),
        )
        resolved = resolve_project(raw).imports[0]
        self.assertEqual(resolved.status, ResolutionStatus.AMBIGUOUS)
        self.assertEqual(resolved.target_files, ("a.py", "z.py"))
        self.assertEqual(resolved.target_symbols, tuple(sorted((left.id, right.id))))


class ReexportResolutionTest(unittest.TestCase):
    def test_typescript_named_reexport_chain_preserves_original_id(self) -> None:
        fetch = symbol("api.ts", Language.TYPESCRIPT, "fetch")
        mid_alias = symbol(
            "mid.ts", Language.TYPESCRIPT, "load", kind=SymbolKind.REEXPORT
        )
        top_alias = symbol(
            "top.ts", Language.TYPESCRIPT, "publicLoad", kind=SymbolKind.REEXPORT
        )
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir("api.ts", Language.TYPESCRIPT, module="api", symbols=(fetch,)),
            file_ir(
                "mid.ts",
                Language.TYPESCRIPT,
                module="mid",
                symbols=(mid_alias,),
                imports=(
                    ImportRef(
                        span("mid.ts", 2), "./api", "fetch", "load", reexport=True
                    ),
                ),
            ),
            file_ir(
                "top.ts",
                Language.TYPESCRIPT,
                module="top",
                symbols=(top_alias,),
                imports=(
                    ImportRef(
                        span("top.ts", 2),
                        "./mid",
                        "load",
                        "publicLoad",
                        reexport=True,
                    ),
                ),
            ),
            file_ir(
                "app.ts",
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span("app.ts", 2), "./top", "publicLoad", "use"),),
                calls=(call(owner, "use"),),
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(result.imports[-1].target_symbols, (fetch.id,))
        self.assertEqual(result.calls[0].target, fetch.id)

    def test_named_reexport_uses_exact_private_declaration_not_wildcard_rules(
        self,
    ) -> None:
        private = symbol(
            "api.ts",
            Language.TYPESCRIPT,
            "foo",
            visibility=Visibility.PRIVATE,
        )
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir(
                private.file,
                Language.TYPESCRIPT,
                module="api",
                symbols=(private,),
            ),
            file_ir(
                "barrel.ts",
                Language.TYPESCRIPT,
                module="barrel",
                imports=(
                    ImportRef(
                        span("barrel.ts", 2),
                        "./api",
                        "foo",
                        None,
                        reexport=True,
                    ),
                ),
            ),
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "./barrel", "foo", None),),
                calls=(call(owner, "foo"),),
            ),
        )
        result = resolve_project(raw)
        downstream = next(
            item for item in result.imports if item.source_file == owner.file
        )
        self.assertEqual(downstream.target_symbols, (private.id,))
        self.assertEqual(result.calls[0].target, private.id)

    def test_wildcard_reexport_cycle_reaches_fixed_point(self) -> None:
        alpha = symbol("a.ts", Language.TYPESCRIPT, "alpha")
        beta = symbol("b.ts", Language.TYPESCRIPT, "beta")
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir(
                "b.ts",
                Language.TYPESCRIPT,
                module="b",
                symbols=(beta,),
                imports=(ImportRef(span("b.ts", 2), "./a", None, None, True, True),),
            ),
            file_ir(
                "a.ts",
                Language.TYPESCRIPT,
                module="a",
                symbols=(alpha,),
                imports=(ImportRef(span("a.ts", 2), "./b", None, None, True, True),),
            ),
            file_ir(
                "app.ts",
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span("app.ts", 2), "./a", "beta", None),),
                calls=(call(owner, "beta"),),
            ),
        )
        result = resolve_project(raw)
        imported_beta = next(
            item for item in result.imports if item.source_file == "app.ts"
        )
        self.assertEqual(imported_beta.target_symbols, (beta.id,))
        self.assertEqual(result.calls[0].target, beta.id)
        assert_cardinality(self, raw, result)

    def test_external_evidence_survives_local_reexport(self) -> None:
        alias = symbol(
            "bar.ts",
            Language.TYPESCRIPT,
            "x",
            kind=SymbolKind.REEXPORT,
        )
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir(
                "bar.ts",
                Language.TYPESCRIPT,
                module="bar",
                symbols=(alias,),
                imports=(
                    ImportRef(span("bar.ts", 2), "external", "x", None, reexport=True),
                ),
            ),
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "./bar", "x", None),),
                calls=(call(owner, "x"),),
            ),
        )
        result = resolve_project(raw)
        downstream = next(
            item for item in result.imports if item.source_file == "app.ts"
        )
        self.assertEqual(downstream.status, ResolutionStatus.EXTERNAL)
        self.assertEqual(result.calls[0].status, ResolutionStatus.EXTERNAL)

    def test_local_and_external_reexport_evidence_remains_ambiguous(self) -> None:
        local = symbol("local.ts", Language.TYPESCRIPT, "foo")
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir(
                local.file,
                Language.TYPESCRIPT,
                module="local",
                symbols=(local,),
            ),
            file_ir(
                "barrel.ts",
                Language.TYPESCRIPT,
                module="barrel",
                imports=(
                    ImportRef(
                        span("barrel.ts", 2),
                        "./local",
                        "foo",
                        None,
                        reexport=True,
                    ),
                    ImportRef(
                        span("barrel.ts", 3),
                        "third_party",
                        "foo",
                        None,
                        reexport=True,
                    ),
                ),
            ),
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "./barrel", "foo", None),),
                calls=(call(owner, "foo"),),
            ),
        )
        result = resolve_project(raw)
        downstream = next(
            item for item in result.imports if item.source_file == owner.file
        )
        self.assertEqual(downstream.status, ResolutionStatus.AMBIGUOUS)
        self.assertEqual(downstream.target_symbols, (local.id,))
        self.assertEqual(result.calls[0].status, ResolutionStatus.AMBIGUOUS)
        self.assertEqual(result.calls[0].candidates, (local.id,))

    def test_namespace_alias_and_namespace_reexport_keep_module_scope(self) -> None:
        top_fetch = symbol("api.ts", Language.TYPESCRIPT, "fetch")
        client = symbol("api.ts", Language.TYPESCRIPT, "Client", kind=SymbolKind.CLASS)
        client_fetch = symbol(
            "api.ts",
            Language.TYPESCRIPT,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        namespace = symbol(
            "barrel.ts",
            Language.TYPESCRIPT,
            "api",
            kind=SymbolKind.REEXPORT,
        )
        direct_owner = symbol("direct.ts", Language.TYPESCRIPT, "run")
        downstream_owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir(
                "api.ts",
                Language.TYPESCRIPT,
                module="api",
                symbols=(top_fetch, client, client_fetch),
            ),
            file_ir(
                namespace.file,
                Language.TYPESCRIPT,
                module="barrel",
                symbols=(namespace,),
                imports=(
                    ImportRef(
                        span(namespace.file, 2),
                        "./api",
                        None,
                        "api",
                        wildcard=True,
                        reexport=True,
                    ),
                ),
            ),
            file_ir(
                direct_owner.file,
                Language.TYPESCRIPT,
                module="direct",
                symbols=(direct_owner,),
                imports=(
                    ImportRef(
                        span(direct_owner.file, 2),
                        "./api",
                        None,
                        "api",
                        wildcard=True,
                    ),
                ),
                calls=(call(direct_owner, "fetch", receiver="api"),),
            ),
            file_ir(
                downstream_owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(downstream_owner,),
                imports=(
                    ImportRef(
                        span(downstream_owner.file, 2),
                        "./barrel",
                        "api",
                        None,
                    ),
                ),
                calls=(
                    call(
                        downstream_owner,
                        "fetch",
                        receiver="api",
                        line=11,
                    ),
                ),
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(
            [item.target for item in result.calls],
            [top_fetch.id, top_fetch.id],
        )
        self.assertNotIn(client_fetch.id, result.calls[0].candidates)

    def test_mixed_status_reexport_cycle_terminates_monotonically(self) -> None:
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir(
                "a.ts",
                Language.TYPESCRIPT,
                module="a",
                imports=(
                    ImportRef(
                        span("a.ts", 2),
                        "external",
                        "foo",
                        None,
                        reexport=True,
                    ),
                    ImportRef(
                        span("a.ts", 3),
                        "./b",
                        None,
                        None,
                        wildcard=True,
                        reexport=True,
                    ),
                ),
            ),
            file_ir(
                "b.ts",
                Language.TYPESCRIPT,
                module="b",
                imports=(
                    ImportRef(
                        span("b.ts", 2),
                        "./missing",
                        "foo",
                        None,
                        reexport=True,
                    ),
                    ImportRef(
                        span("b.ts", 3),
                        "./a",
                        None,
                        None,
                        wildcard=True,
                        reexport=True,
                    ),
                ),
            ),
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "./a", "foo", None),),
                calls=(call(owner, "foo"),),
            ),
        )
        result = resolve_project(raw)
        downstream = next(
            item for item in result.imports if item.source_file == owner.file
        )
        self.assertEqual(downstream.status, ResolutionStatus.EXTERNAL)
        self.assertEqual(result.calls[0].status, ResolutionStatus.EXTERNAL)

    def test_external_namespace_reexport_keeps_external_member_evidence(self) -> None:
        namespace = symbol(
            "barrel.ts",
            Language.TYPESCRIPT,
            "api",
            kind=SymbolKind.REEXPORT,
        )
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir(
                namespace.file,
                Language.TYPESCRIPT,
                module="barrel",
                symbols=(namespace,),
                imports=(
                    ImportRef(
                        span(namespace.file, 2),
                        "third_party",
                        None,
                        "api",
                        wildcard=True,
                        reexport=True,
                    ),
                ),
            ),
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "./barrel", "api", None),),
                calls=(call(owner, "fetch", receiver="api"),),
            ),
        )
        result = resolve_project(raw)
        downstream = next(
            item for item in result.imports if item.source_file == owner.file
        )
        self.assertEqual(downstream.status, ResolutionStatus.RESOLVED)
        self.assertEqual(result.calls[0].status, ResolutionStatus.EXTERNAL)

    def test_namespace_reexport_preserves_ambiguous_source_module(self) -> None:
        fetch = symbol("api.ts", Language.TYPESCRIPT, "fetch")
        other = symbol("api.js", Language.JAVASCRIPT, "other")
        namespace = symbol(
            "barrel.ts",
            Language.TYPESCRIPT,
            "ns",
            kind=SymbolKind.REEXPORT,
        )
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir(fetch.file, Language.TYPESCRIPT, module="api", symbols=(fetch,)),
            file_ir(other.file, Language.JAVASCRIPT, module="api", symbols=(other,)),
            file_ir(
                namespace.file,
                Language.TYPESCRIPT,
                module="barrel",
                symbols=(namespace,),
                imports=(
                    ImportRef(
                        span(namespace.file, 2),
                        "./api",
                        None,
                        "ns",
                        wildcard=True,
                        reexport=True,
                    ),
                ),
            ),
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "./barrel", "ns", None),),
                calls=(call(owner, "fetch", receiver="ns"),),
            ),
        )
        resolved = resolve_project(raw).calls[0]
        self.assertEqual(resolved.status, ResolutionStatus.AMBIGUOUS)
        self.assertEqual(resolved.candidates, (fetch.id,))

    def test_top_level_reexport_does_not_leak_into_inline_module_key(self) -> None:
        local = symbol("local.ts", Language.TYPESCRIPT, "foo")
        namespace = symbol(
            "barrel.ts",
            Language.TYPESCRIPT,
            "ns",
            kind=SymbolKind.MODULE,
        )
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir(local.file, Language.TYPESCRIPT, module="local", symbols=(local,)),
            file_ir(
                namespace.file,
                Language.TYPESCRIPT,
                module="barrel",
                symbols=(namespace,),
                imports=(
                    ImportRef(
                        span(namespace.file, 2),
                        "./local",
                        "foo",
                        None,
                        reexport=True,
                    ),
                ),
            ),
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "ns", "foo", None),),
            ),
        )
        imported = next(
            item
            for item in resolve_project(raw).imports
            if item.source_file == owner.file
        )
        self.assertEqual(imported.status, ResolutionStatus.UNRESOLVED)
        self.assertEqual(imported.target_symbols, ())

    def test_external_wildcard_reexport_blocks_downstream_family_fallback(self) -> None:
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        decoy = symbol("other.ts", Language.TYPESCRIPT, "fetch")
        raw = project(
            file_ir(
                "barrel.ts",
                Language.TYPESCRIPT,
                module="barrel",
                imports=(
                    ImportRef(
                        span("barrel.ts", 2),
                        "third_party",
                        None,
                        None,
                        wildcard=True,
                        reexport=True,
                    ),
                ),
            ),
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(
                    ImportRef(
                        span(owner.file, 2),
                        "./barrel",
                        None,
                        None,
                        wildcard=True,
                    ),
                ),
                calls=(call(owner, "fetch"),),
            ),
            file_ir(decoy.file, Language.TYPESCRIPT, module="other", symbols=(decoy,)),
        )
        self.assertEqual(
            resolve_project(raw).calls[0].status, ResolutionStatus.EXTERNAL
        )

    def test_named_reexport_worklist_is_linear_in_chain_length(self) -> None:
        files = [
            file_ir(
                "m0.ts",
                Language.TYPESCRIPT,
                module="m0",
                symbols=(symbol("m0.ts", Language.TYPESCRIPT, "x"),),
            )
        ]
        for index in range(1, 160):
            file = f"m{index}.ts"
            files.append(
                file_ir(
                    file,
                    Language.TYPESCRIPT,
                    module=f"m{index}",
                    symbols=(
                        symbol(
                            file, Language.TYPESCRIPT, "x", kind=SymbolKind.REEXPORT
                        ),
                    ),
                    imports=(
                        ImportRef(
                            span(file, 2),
                            f"./m{index - 1}",
                            "x",
                            None,
                            reexport=True,
                        ),
                    ),
                )
            )
        with patch(
            "hologram.resolve._import_module",
            wraps=resolver_module._import_module,
        ) as normalized:
            result = resolve_project(project(*files))
        self.assertTrue(
            all(item.status is ResolutionStatus.RESOLVED for item in result.imports)
        )
        self.assertLess(normalized.call_count, len(files) * 4)


class CallResolutionTest(unittest.TestCase):
    def test_alias_and_typed_receiver_select_exact_target(self) -> None:
        client_a = symbol(
            "a/client.py", Language.PYTHON, "Client", kind=SymbolKind.CLASS
        )
        fetch_a = symbol(
            client_a.file,
            Language.PYTHON,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        client_b = symbol(
            "b/client.py", Language.PYTHON, "Client", kind=SymbolKind.CLASS
        )
        fetch_b = symbol(
            client_b.file,
            Language.PYTHON,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        owner = symbol(
            "app.py",
            Language.PYTHON,
            "run",
            bindings=(Binding("client", "AClient"),),
        )
        raw = project(
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                imports=(
                    ImportRef(span(owner.file, 2), "a.client", "Client", "AClient"),
                ),
                calls=(call(owner, "fetch", receiver="client"),),
            ),
            file_ir(
                client_a.file,
                Language.PYTHON,
                module="a.client",
                symbols=(client_a, fetch_a),
            ),
            file_ir(
                client_b.file,
                Language.PYTHON,
                module="b.client",
                symbols=(client_b, fetch_b),
            ),
        )
        resolved = resolve_project(raw).calls[0]
        self.assertEqual(resolved.status, ResolutionStatus.RESOLVED)
        self.assertEqual(resolved.target, fetch_a.id)
        self.assertEqual(resolved.target.container_path, ("Client",))
        self.assertEqual(resolved.candidates, (resolved.target,))
        self.assertEqual(resolved.display_name, "Client.fetch")

    def test_imported_overload_is_narrowed_by_arity(self) -> None:
        api = symbol("lib/Api.java", Language.JAVA, "Api", kind=SymbolKind.CLASS)
        unary = symbol(
            api.file,
            Language.JAVA,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Api",),
            params=("int",),
        )
        binary = symbol(
            api.file,
            Language.JAVA,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Api",),
            params=("int", "int"),
        )
        owner = symbol("app/App.java", Language.JAVA, "run", kind=SymbolKind.METHOD)
        raw = project(
            file_ir(
                api.file,
                Language.JAVA,
                module="lib",
                symbols=(api, unary, binary),
            ),
            file_ir(
                owner.file,
                Language.JAVA,
                module="app",
                symbols=(owner,),
                imports=(
                    ImportRef(span(owner.file, 2), "lib.Api", "fetch", None),
                ),
                calls=(call(owner, "fetch", arity=1),),
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(result.imports[0].status, ResolutionStatus.AMBIGUOUS)
        self.assertEqual(result.calls[0].status, ResolutionStatus.RESOLVED)
        self.assertEqual(result.calls[0].target, unary.id)
        self.assertEqual(result.calls[0].candidates, (unary.id,))

    def test_same_name_without_import_stays_ambiguous(self) -> None:
        owner = symbol("app.py", Language.PYTHON, "run")
        left = symbol("a.py", Language.PYTHON, "fetch")
        right = symbol("b.py", Language.PYTHON, "fetch")
        raw = project(
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                calls=(call(owner, "fetch"),),
            ),
            file_ir(left.file, Language.PYTHON, module="a", symbols=(left,)),
            file_ir(right.file, Language.PYTHON, module="b", symbols=(right,)),
        )
        resolved = resolve_project(raw).calls[0]
        self.assertEqual(resolved.status, ResolutionStatus.AMBIGUOUS)
        self.assertIsNone(resolved.target)
        self.assertEqual(resolved.candidates, tuple(sorted((left.id, right.id))))
        self.assertIsNone(resolved.display_name)

    def test_same_file_container_self_bare_and_static_receiver_precedence(self) -> None:
        type_ = symbol("client.py", Language.PYTHON, "Client", kind=SymbolKind.CLASS)
        fetch = symbol(
            type_.file,
            Language.PYTHON,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        owner = symbol(
            type_.file,
            Language.PYTHON,
            "run",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        decoy = symbol(type_.file, Language.PYTHON, "fetch", line=8)
        calls = (
            call(owner, "fetch", line=10),
            call(owner, "fetch", receiver="self", line=11),
            call(owner, "fetch", receiver="Client", line=12),
        )
        raw = project(
            file_ir(
                type_.file,
                Language.PYTHON,
                module="client",
                symbols=(decoy, owner, type_, fetch),
                calls=calls,
            )
        )
        result = resolve_project(raw)
        self.assertEqual([item.target for item in result.calls], [fetch.id] * 3)
        self.assertEqual(
            [item.display_name for item in result.calls], ["Client.fetch"] * 3
        )

    def test_constructor_declarations_precede_type_fallback_and_arity_does_not_fall_through(
        self,
    ) -> None:
        owner = symbol("app.java", Language.JAVA, "run", kind=SymbolKind.METHOD)
        widget = symbol("app.java", Language.JAVA, "Widget", kind=SymbolKind.CLASS)
        ctor_one = symbol(
            "app.java",
            Language.JAVA,
            "Widget",
            kind=SymbolKind.CONSTRUCTOR,
            container=("Widget",),
            params=("int",),
        )
        ctor_two = symbol(
            "app.java",
            Language.JAVA,
            "Widget",
            kind=SymbolKind.CONSTRUCTOR,
            container=("Widget",),
            params=("int", "int"),
        )
        gadget = symbol("app.java", Language.JAVA, "Gadget", kind=SymbolKind.CLASS)
        calls = (
            call(owner, "Widget", kind=CallKind.CONSTRUCT, arity=2, line=10),
            call(owner, "Widget", kind=CallKind.CONSTRUCT, arity=3, line=11),
            call(owner, "Gadget", kind=CallKind.CONSTRUCT, arity=3, line=12),
        )
        raw = project(
            file_ir(
                owner.file,
                Language.JAVA,
                module="app",
                symbols=(owner, widget, ctor_one, ctor_two, gadget),
                calls=calls,
            )
        )
        result = resolve_project(raw).calls
        self.assertEqual(result[0].target, ctor_two.id)
        self.assertEqual(result[1].status, ResolutionStatus.UNRESOLVED)
        self.assertEqual(result[1].candidates, ())
        self.assertEqual(result[2].target, gadget.id)

    def test_type_namespace_is_not_shadowed_by_a_value_binding(self) -> None:
        client = symbol(
            "app.java", Language.JAVA, "Client", kind=SymbolKind.CLASS
        )
        owner = symbol(
            "app.java",
            Language.JAVA,
            "make",
            kind=SymbolKind.METHOD,
            container=("App",),
            bindings=(Binding("Client", "int"),),
        )
        type_reference = reference(
            owner.file,
            "Client",
            owner=owner,
            kind=ReferenceKind.TYPE,
            context=ReferenceContext.TYPE,
        )
        raw = project(
            file_ir(
                owner.file,
                Language.JAVA,
                module="app",
                symbols=(client, owner),
                calls=(
                    call(
                        owner,
                        "Client",
                        kind=CallKind.CONSTRUCT,
                        arity=0,
                    ),
                ),
                references=(type_reference,),
            )
        )
        result = resolve_project(raw)
        self.assertEqual(result.calls[0].target, client.id)
        self.assertEqual(result.references[0].target, client.id)

    def test_fully_qualified_constructor_uses_exact_project_type(self) -> None:
        client = symbol(
            "lib/Client.java", Language.JAVA, "Client", kind=SymbolKind.CLASS
        )
        owner = symbol("app/App.java", Language.JAVA, "run", kind=SymbolKind.METHOD)
        raw = project(
            file_ir(
                client.file,
                Language.JAVA,
                module="lib",
                symbols=(client,),
            ),
            file_ir(
                owner.file,
                Language.JAVA,
                module="app",
                symbols=(owner,),
                calls=(
                    call(
                        owner,
                        "lib.Client",
                        kind=CallKind.CONSTRUCT,
                        arity=0,
                    ),
                ),
            ),
        )
        resolved = resolve_project(raw).calls[0]
        self.assertEqual(resolved.status, ResolutionStatus.RESOLVED)
        self.assertEqual(resolved.target, client.id)
        self.assertEqual(resolved.candidates, (client.id,))

    def test_explicit_constructor_for_one_type_keeps_other_implicit_type_candidate(
        self,
    ) -> None:
        left = symbol("a.py", Language.PYTHON, "Foo", kind=SymbolKind.CLASS)
        left_ctor = symbol(
            left.file,
            Language.PYTHON,
            "Foo",
            kind=SymbolKind.CONSTRUCTOR,
            container=("Foo",),
        )
        right = symbol("b.py", Language.PYTHON, "Foo", kind=SymbolKind.CLASS)
        owner = symbol("app.py", Language.PYTHON, "run")
        raw = project(
            file_ir(
                left.file,
                Language.PYTHON,
                module="a",
                symbols=(left, left_ctor),
            ),
            file_ir(right.file, Language.PYTHON, module="b", symbols=(right,)),
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                calls=(call(owner, "Foo", kind=CallKind.CONSTRUCT),),
            ),
        )
        resolved = resolve_project(raw).calls[0]
        self.assertEqual(resolved.status, ResolutionStatus.AMBIGUOUS)
        self.assertEqual(
            resolved.candidates,
            tuple(sorted((left_ctor.id, right.id))),
        )

    def test_typed_receiver_follows_supers_cycle_safely_and_direct_scope_wins(
        self,
    ) -> None:
        type_a = symbol(
            "types.java", Language.JAVA, "A", kind=SymbolKind.CLASS, supers=("B",)
        )
        type_b = symbol(
            "types.java", Language.JAVA, "B", kind=SymbolKind.CLASS, supers=("A",)
        )
        inherited = symbol(
            "types.java",
            Language.JAVA,
            "ping",
            kind=SymbolKind.METHOD,
            container=("B",),
            params=("int",),
        )
        owner = symbol(
            "types.java",
            Language.JAVA,
            "run",
            kind=SymbolKind.METHOD,
            bindings=(Binding("value", "A"),),
        )
        raw = project(
            file_ir(
                owner.file,
                Language.JAVA,
                module="types",
                symbols=(type_a, type_b, inherited, owner),
                calls=(call(owner, "ping", receiver="value", arity=1),),
            )
        )
        self.assertEqual(resolve_project(raw).calls[0].target, inherited.id)

        direct_wrong_arity = symbol(
            "types.java",
            Language.JAVA,
            "ping",
            kind=SymbolKind.METHOD,
            container=("A",),
            params=("int", "int"),
        )
        with_direct = project(
            file_ir(
                owner.file,
                Language.JAVA,
                module="types",
                symbols=(type_a, type_b, inherited, direct_wrong_arity, owner),
                calls=(call(owner, "ping", receiver="value", arity=1),),
            )
        )
        blocked = resolve_project(with_direct).calls[0]
        self.assertEqual(blocked.status, ResolutionStatus.UNRESOLVED)
        self.assertEqual(blocked.candidates, ())

    def test_each_ambiguous_receiver_type_keeps_its_nearest_inherited_member(
        self,
    ) -> None:
        left = symbol("a.py", Language.PYTHON, "Client", kind=SymbolKind.CLASS)
        left_fetch = symbol(
            left.file,
            Language.PYTHON,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        base = symbol("b.py", Language.PYTHON, "Base", kind=SymbolKind.CLASS)
        base_fetch = symbol(
            base.file,
            Language.PYTHON,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Base",),
        )
        right = symbol(
            base.file,
            Language.PYTHON,
            "Client",
            kind=SymbolKind.CLASS,
            supers=("Base",),
        )
        owner = symbol(
            "app.py",
            Language.PYTHON,
            "run",
            bindings=(Binding("value", "Client"),),
        )
        raw = project(
            file_ir(left.file, Language.PYTHON, module="a", symbols=(left, left_fetch)),
            file_ir(
                base.file,
                Language.PYTHON,
                module="b",
                symbols=(base, base_fetch, right),
            ),
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                calls=(call(owner, "fetch", receiver="value"),),
            ),
        )
        resolved = resolve_project(raw).calls[0]
        self.assertEqual(resolved.status, ResolutionStatus.AMBIGUOUS)
        self.assertEqual(
            resolved.candidates,
            tuple(sorted((left_fetch.id, base_fetch.id))),
        )

    def test_external_and_unresolved_facts_are_retained(self) -> None:
        owner = symbol("app.py", Language.PYTHON, "run")
        calls = (
            call(owner, "fetch", receiver="remote", line=10),
            call(owner, "missing", line=11),
        )
        raw = project(
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                imports=(
                    ImportRef(span(owner.file, 2), "third_party", None, "remote"),
                ),
                calls=calls,
            )
        )
        result = resolve_project(raw)
        self.assertEqual(
            [item.status for item in result.calls],
            [ResolutionStatus.EXTERNAL, ResolutionStatus.UNRESOLVED],
        )
        self.assertEqual(len(result.calls), len(calls))
        self.assertTrue(
            all(item.fact is raw_fact for item, raw_fact in zip(result.calls, calls))
        )

    def test_local_shadow_and_unknown_receiver_are_non_symbol_evidence(self) -> None:
        target = symbol("lib.py", Language.PYTHON, "handler")
        owner = symbol(
            "app.py",
            Language.PYTHON,
            "run",
            bindings=(Binding("handler", "?"), Binding("client", "?")),
        )
        raw = project(
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                calls=(
                    call(owner, "handler"),
                    call(owner, "handler", receiver="client", line=11),
                ),
            ),
            file_ir(target.file, Language.PYTHON, module="lib", symbols=(target,)),
        )
        self.assertEqual(
            [item.status for item in resolve_project(raw).calls],
            [ResolutionStatus.UNRESOLVED, ResolutionStatus.UNRESOLVED],
        )

    def test_external_and_unresolved_explicit_aliases_stop_homonym_fallback(
        self,
    ) -> None:
        homonym = symbol("other.py", Language.PYTHON, "fetch")
        owner = symbol("app.py", Language.PYTHON, "run")
        missing_module_symbol = symbol("lib.py", Language.PYTHON, "different")
        raw = project(
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                imports=(
                    ImportRef(
                        span(owner.file, 2), "third_party", "fetch", "externalFetch"
                    ),
                    ImportRef(span(owner.file, 3), "lib", "absent", "fetch"),
                ),
                calls=(
                    call(owner, "externalFetch", line=10),
                    call(owner, "fetch", line=11),
                ),
            ),
            file_ir(homonym.file, Language.PYTHON, module="other", symbols=(homonym,)),
            file_ir(
                missing_module_symbol.file,
                Language.PYTHON,
                module="lib",
                symbols=(missing_module_symbol,),
            ),
        )
        result = resolve_project(raw)
        self.assertEqual(
            [item.status for item in result.calls],
            [ResolutionStatus.EXTERNAL, ResolutionStatus.UNRESOLVED],
        )

    def test_named_value_alias_cannot_access_sibling_module_export(self) -> None:
        foo = symbol("api.ts", Language.TYPESCRIPT, "foo")
        bar = symbol("api.ts", Language.TYPESCRIPT, "bar")
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir(
                foo.file,
                Language.TYPESCRIPT,
                module="api",
                symbols=(foo, bar),
            ),
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "./api", "foo", "x"),),
                calls=(call(owner, "bar", receiver="x"),),
            ),
        )
        resolved = resolve_project(raw).calls[0]
        self.assertEqual(resolved.status, ResolutionStatus.UNRESOLVED)
        self.assertEqual(resolved.candidates, ())

    def test_exact_noncallable_alias_blocks_callable_family_fallback(self) -> None:
        constant = symbol(
            "api.ts",
            Language.TYPESCRIPT,
            "value",
            kind=SymbolKind.CONSTANT,
        )
        decoy = symbol("other.ts", Language.TYPESCRIPT, "x")
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir(
                constant.file,
                Language.TYPESCRIPT,
                module="api",
                symbols=(constant,),
            ),
            file_ir(decoy.file, Language.TYPESCRIPT, module="other", symbols=(decoy,)),
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "./api", "value", "x"),),
                calls=(call(owner, "x"),),
            ),
        )
        resolved = resolve_project(raw).calls[0]
        self.assertEqual(resolved.status, ResolutionStatus.UNRESOLVED)
        self.assertEqual(resolved.candidates, ())

    def test_family_isolation_and_exact_family_compatibility(self) -> None:
        py_owner = symbol("app.py", Language.PYTHON, "run")
        py_target = symbol("lib.py", Language.PYTHON, "fetch")
        ts_target = symbol("lib.ts", Language.TYPESCRIPT, "fetch")
        js_target = symbol("lib.js", Language.JAVASCRIPT, "fetch")
        raw = project(
            file_ir(
                py_owner.file,
                Language.PYTHON,
                module="app",
                symbols=(py_owner,),
                calls=(call(py_owner, "fetch"),),
            ),
            file_ir(
                py_target.file, Language.PYTHON, module="lib", symbols=(py_target,)
            ),
            file_ir(
                ts_target.file, Language.TYPESCRIPT, module="lib", symbols=(ts_target,)
            ),
            file_ir(
                js_target.file, Language.JAVASCRIPT, module="lib", symbols=(js_target,)
            ),
        )
        resolved = resolve_project(raw).calls[0]
        self.assertEqual(resolved.target, py_target.id)
        self.assertNotIn(ts_target.id, resolved.candidates)
        self.assertNotIn(js_target.id, resolved.candidates)

    def test_current_container_traverses_supers_without_unrelated_fallback(
        self,
    ) -> None:
        type_a = symbol(
            "types.java", Language.JAVA, "A", kind=SymbolKind.CLASS, supers=("B",)
        )
        type_b = symbol(
            "types.java", Language.JAVA, "B", kind=SymbolKind.CLASS, supers=("C",)
        )
        type_c = symbol(
            "types.java", Language.JAVA, "C", kind=SymbolKind.CLASS, supers=("A",)
        )
        inherited = symbol(
            "types.java",
            Language.JAVA,
            "ping",
            kind=SymbolKind.METHOD,
            container=("C",),
        )
        owner = symbol(
            "types.java",
            Language.JAVA,
            "run",
            kind=SymbolKind.METHOD,
            container=("A",),
        )
        decoy_type = symbol("other.java", Language.JAVA, "Other", kind=SymbolKind.CLASS)
        decoy = symbol(
            "other.java",
            Language.JAVA,
            "ping",
            kind=SymbolKind.METHOD,
            container=("Other",),
        )
        raw = project(
            file_ir(
                owner.file,
                Language.JAVA,
                module="types",
                symbols=(type_a, type_b, type_c, inherited, owner),
                calls=(
                    call(owner, "ping", receiver="this"),
                    call(owner, "ping", line=11),
                ),
            ),
            file_ir(
                decoy.file,
                Language.JAVA,
                module="other",
                symbols=(decoy_type, decoy),
            ),
        )
        self.assertEqual(
            [item.target for item in resolve_project(raw).calls],
            [inherited.id, inherited.id],
        )

    def test_super_and_base_receivers_start_at_direct_parent(self) -> None:
        for language, receiver in (
            (Language.JAVA, "super"),
            (Language.CSHARP, "base"),
        ):
            with self.subTest(language=language):
                parent = symbol(
                    f"types.{language.value}",
                    language,
                    "Parent",
                    kind=SymbolKind.CLASS,
                )
                inherited = symbol(
                    parent.file,
                    language,
                    "ping",
                    kind=SymbolKind.METHOD,
                    container=("Parent",),
                )
                child = symbol(
                    parent.file,
                    language,
                    "Child",
                    kind=SymbolKind.CLASS,
                    supers=("Parent",),
                )
                shadow = symbol(
                    parent.file,
                    language,
                    "ping",
                    kind=SymbolKind.METHOD,
                    container=("Child",),
                )
                owner = symbol(
                    parent.file,
                    language,
                    "run",
                    kind=SymbolKind.METHOD,
                    container=("Child",),
                )
                raw = project(
                    file_ir(
                        parent.file,
                        language,
                        module="types",
                        symbols=(parent, inherited, child, shadow, owner),
                        calls=(call(owner, "ping", receiver=receiver),),
                    )
                )
                self.assertEqual(resolve_project(raw).calls[0].target, inherited.id)

    def test_namespace_qualified_binding_resolves_exact_exported_type(self) -> None:
        client = symbol("lib.ts", Language.TYPESCRIPT, "Client", kind=SymbolKind.CLASS)
        fetch = symbol(
            client.file,
            Language.TYPESCRIPT,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        owner = symbol(
            "app.ts",
            Language.TYPESCRIPT,
            "run",
            bindings=(Binding("value", "api.Client"),),
        )
        raw = project(
            file_ir(
                client.file, Language.TYPESCRIPT, module="lib", symbols=(client, fetch)
            ),
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(
                    ImportRef(
                        span(owner.file, 2),
                        "./lib",
                        None,
                        "api",
                        wildcard=True,
                    ),
                ),
                calls=(call(owner, "fetch", receiver="value"),),
            ),
        )
        self.assertEqual(resolve_project(raw).calls[0].target, fetch.id)

    def test_namespace_qualified_static_receiver_resolves_exported_type(self) -> None:
        client = symbol("lib.ts", Language.TYPESCRIPT, "Client", kind=SymbolKind.CLASS)
        fetch = symbol(
            client.file,
            Language.TYPESCRIPT,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw = project(
            file_ir(
                client.file,
                Language.TYPESCRIPT,
                module="lib",
                symbols=(client, fetch),
            ),
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(
                    ImportRef(
                        span(owner.file, 2),
                        "./lib",
                        None,
                        "svc",
                        wildcard=True,
                    ),
                ),
                calls=(call(owner, "fetch", receiver="svc.Client"),),
            ),
        )
        self.assertEqual(resolve_project(raw).calls[0].target, fetch.id)

    def test_external_typed_binding_and_exact_qualified_type_are_terminal(self) -> None:
        external_owner = symbol(
            "external.py",
            Language.PYTHON,
            "run",
            bindings=(Binding("client", "Client"),),
        )
        external = project(
            file_ir(
                external_owner.file,
                Language.PYTHON,
                module="external",
                symbols=(external_owner,),
                imports=(
                    ImportRef(span(external_owner.file, 2), "third", "Client", None),
                ),
                calls=(call(external_owner, "fetch", receiver="client"),),
            )
        )
        self.assertEqual(
            resolve_project(external).calls[0].status,
            ResolutionStatus.EXTERNAL,
        )

        left_type = symbol(
            "a.py",
            Language.PYTHON,
            "Client",
            kind=SymbolKind.CLASS,
            visibility=Visibility.PRIVATE,
        )
        left_fetch = symbol(
            left_type.file,
            Language.PYTHON,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        right_type = symbol("b.py", Language.PYTHON, "Client", kind=SymbolKind.CLASS)
        right_fetch = symbol(
            right_type.file,
            Language.PYTHON,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        owner = symbol(
            "app.py",
            Language.PYTHON,
            "run",
            bindings=(Binding("client", "a.Client"),),
        )
        exact = project(
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                calls=(call(owner, "fetch", receiver="client"),),
            ),
            file_ir(
                left_type.file,
                Language.PYTHON,
                module="a",
                symbols=(left_type, left_fetch),
            ),
            file_ir(
                right_type.file,
                Language.PYTHON,
                module="b",
                symbols=(right_type, right_fetch),
            ),
        )
        self.assertEqual(resolve_project(exact).calls[0].target, left_fetch.id)

    def test_ambiguous_typed_alias_remains_ambiguous_after_member_lookup(self) -> None:
        client = symbol("lib.py", Language.PYTHON, "Client", kind=SymbolKind.CLASS)
        fetch = symbol(
            client.file,
            Language.PYTHON,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        owner = symbol(
            "app.py",
            Language.PYTHON,
            "run",
            bindings=(Binding("client", "X"),),
        )
        raw = project(
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                imports=(
                    ImportRef(span(owner.file, 2), "lib", "Client", "X"),
                    ImportRef(
                        span(owner.file, 3),
                        "third_party",
                        "Client",
                        "X",
                    ),
                ),
                calls=(call(owner, "fetch", receiver="client"),),
            ),
            file_ir(
                client.file, Language.PYTHON, module="lib", symbols=(client, fetch)
            ),
        )
        resolved = resolve_project(raw).calls[0]
        self.assertEqual(resolved.status, ResolutionStatus.AMBIGUOUS)
        self.assertEqual(resolved.candidates, (fetch.id,))

    def test_dangling_owner_never_uses_family_fallback(self) -> None:
        missing_owner = symbol("partial.py", Language.PYTHON, "run")
        decoy = symbol("other.py", Language.PYTHON, "fetch")
        raw = project(
            file_ir(
                missing_owner.file,
                Language.PYTHON,
                module="partial",
                calls=(call(missing_owner, "fetch"),),
            ),
            file_ir(decoy.file, Language.PYTHON, module="other", symbols=(decoy,)),
        )
        resolved = resolve_project(raw).calls[0]
        self.assertEqual(resolved.status, ResolutionStatus.UNRESOLVED)
        self.assertEqual(resolved.candidates, ())

    def test_nested_callable_uses_nearest_enclosing_type(self) -> None:
        client = symbol("app.py", Language.PYTHON, "Client", kind=SymbolKind.CLASS)
        fetch = symbol(
            client.file,
            Language.PYTHON,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        nested = symbol(
            client.file,
            Language.PYTHON,
            "nested",
            container=("Client", "run"),
        )
        raw = project(
            file_ir(
                client.file,
                Language.PYTHON,
                module="app",
                symbols=(client, fetch, nested),
                calls=(call(nested, "fetch", receiver="self"),),
            )
        )
        self.assertEqual(resolve_project(raw).calls[0].target, fetch.id)


class ReferenceResolutionTest(unittest.TestCase):
    def test_recognized_dynamic_callback_keeps_possible_resolution(self) -> None:
        callback = symbol("app.py", Language.PYTHON, "onRefresh")
        raw_reference = reference(
            "app.py",
            "onRefresh",
            context=ReferenceContext.ANNOTATION,
            confidence=ReferenceConfidence.POSSIBLE,
        )
        raw = project(
            file_ir(
                "app.py",
                Language.PYTHON,
                module="app",
                symbols=(callback,),
                references=(raw_reference,),
            )
        )
        resolved = resolve_project(raw).references[0]
        self.assertEqual(resolved.status, ResolutionStatus.RESOLVED)
        self.assertIs(resolved.fact, raw_reference)
        self.assertEqual(resolved.fact.context, ReferenceContext.ANNOTATION)
        self.assertEqual(resolved.fact.confidence, ReferenceConfidence.POSSIBLE)
        self.assertEqual(resolved.target.name, "onRefresh")

    def test_possible_config_callback_keeps_all_ambiguous_candidates(self) -> None:
        left = symbol("a.py", Language.PYTHON, "handler")
        right = symbol("b.py", Language.PYTHON, "handler")
        raw_reference = reference(
            "config.py",
            "handler",
            context=ReferenceContext.CONFIG,
            confidence=ReferenceConfidence.POSSIBLE,
        )
        raw = project(
            file_ir(
                "config.py",
                Language.PYTHON,
                module="config",
                references=(raw_reference,),
            ),
            file_ir(left.file, Language.PYTHON, module="a", symbols=(left,)),
            file_ir(right.file, Language.PYTHON, module="b", symbols=(right,)),
        )
        resolved = resolve_project(raw).references[0]
        self.assertEqual(resolved.status, ResolutionStatus.AMBIGUOUS)
        self.assertEqual(resolved.fact.context, ReferenceContext.CONFIG)
        self.assertEqual(resolved.fact.confidence, ReferenceConfidence.POSSIBLE)
        self.assertEqual(
            [candidate.name for candidate in resolved.candidates],
            ["handler", "handler"],
        )

    def test_bad_config_qualifier_does_not_fall_through_to_global_name(self) -> None:
        target = symbol("lib.ts", Language.TYPESCRIPT, "handler")
        raw_references = tuple(
            reference(
                "app.ts",
                "handler",
                qualifier="missing",
                context=context,
                confidence=(
                    ReferenceConfidence.POSSIBLE
                    if context
                    in {
                        ReferenceContext.ANNOTATION,
                        ReferenceContext.CONFIG,
                        ReferenceContext.REFLECTION,
                    }
                    else ReferenceConfidence.DEFINITE
                ),
                line=10 + index,
            )
            for index, context in enumerate(ReferenceContext)
        )
        raw = project(
            file_ir(
                "app.ts",
                Language.TYPESCRIPT,
                module="app",
                references=raw_references,
            ),
            file_ir(target.file, Language.TYPESCRIPT, module="lib", symbols=(target,)),
        )
        self.assertEqual(
            [item.status for item in resolve_project(raw).references],
            [ResolutionStatus.UNRESOLVED] * len(raw_references),
        )

    def test_module_alias_reference_targets_namespace_owner_not_exports(self) -> None:
        module = symbol(
            "api.ts",
            Language.TYPESCRIPT,
            "api",
            kind=SymbolKind.MODULE,
        )
        fetch = symbol(module.file, Language.TYPESCRIPT, "fetch")
        owner = symbol("app.ts", Language.TYPESCRIPT, "run")
        raw_reference = reference(owner.file, "api", owner=owner)
        raw = project(
            file_ir(
                module.file,
                Language.TYPESCRIPT,
                module="api",
                symbols=(module, fetch),
            ),
            file_ir(
                owner.file,
                Language.TYPESCRIPT,
                module="app",
                symbols=(owner,),
                imports=(
                    ImportRef(
                        span(owner.file, 2),
                        "./api",
                        None,
                        "api",
                        wildcard=True,
                    ),
                ),
                references=(raw_reference,),
            ),
        )
        resolved = resolve_project(raw).references[0]
        self.assertEqual(resolved.status, ResolutionStatus.RESOLVED)
        self.assertEqual(resolved.target, module.id)

    def test_qualified_reference_uses_typed_receiver_and_preserves_fact(self) -> None:
        type_ = symbol("lib.py", Language.PYTHON, "Client", kind=SymbolKind.CLASS)
        target = symbol(
            type_.file,
            Language.PYTHON,
            "fetch",
            kind=SymbolKind.METHOD,
            container=("Client",),
        )
        owner = symbol(
            "app.py",
            Language.PYTHON,
            "run",
            bindings=(Binding("client", "Client"),),
        )
        raw_reference = reference(
            owner.file,
            "fetch",
            owner=owner,
            qualifier="client",
        )
        raw = project(
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner,),
                imports=(ImportRef(span(owner.file, 2), "lib", "Client", None),),
                references=(raw_reference, raw_reference),
            ),
            file_ir(type_.file, Language.PYTHON, module="lib", symbols=(type_, target)),
        )
        result = resolve_project(raw)
        self.assertEqual(
            [item.target for item in result.references],
            [target.id, target.id],
        )
        self.assertTrue(all(item.fact is raw_reference for item in result.references))
        assert_cardinality(self, raw, result)


class DeterminismAndExtractionTest(unittest.TestCase):
    def test_permuted_project_files_have_byte_for_byte_equal_resolution(self) -> None:
        owner = symbol("app.py", Language.PYTHON, "run")
        left = symbol("a.py", Language.PYTHON, "fetch")
        right = symbol("b.py", Language.PYTHON, "fetch")
        app = file_ir(
            owner.file,
            Language.PYTHON,
            module="app",
            symbols=(owner,),
            calls=(call(owner, "fetch"),),
        )
        a = file_ir(left.file, Language.PYTHON, module="a", symbols=(left,))
        b = file_ir(right.file, Language.PYTHON, module="b", symbols=(right,))
        self.assertEqual(
            resolve_project(project(app, a, b)), resolve_project(project(b, app, a))
        )

    def test_duplicate_equal_raw_occurrences_keep_cardinality_order_and_identity(
        self,
    ) -> None:
        owner = symbol("app.py", Language.PYTHON, "run")
        target = symbol("app.py", Language.PYTHON, "fetch")
        duplicate = call(owner, "fetch")
        raw = project(
            file_ir(
                owner.file,
                Language.PYTHON,
                module="app",
                symbols=(owner, target),
                calls=(duplicate, duplicate),
            )
        )
        result = resolve_project(raw)
        self.assertEqual(len(result.calls), 2)
        self.assertIs(result.calls[0].fact, duplicate)
        self.assertIs(result.calls[1].fact, duplicate)
        self.assertEqual(result.calls[0], result.calls[1])

    def test_listed_language_fixtures_resolve_and_keep_extractor_decoys_out(
        self,
    ) -> None:
        def snapshot(path: Path, language: Language, root: Path) -> SourceFile:
            raw = path.read_bytes()
            return SourceFile(
                path,
                path.relative_to(root).as_posix(),
                language,
                SourceRole.PRODUCTION,
                raw,
                hashlib.sha256(raw).hexdigest(),
            )

        python_root = FIXTURES / "python"
        python_sources = tuple(
            snapshot(path, Language.PYTHON, python_root)
            for path in sorted(python_root.rglob("*.py"))
        )
        python_project = extract_project(Path("/repo"), python_sources)
        python_result = resolve_project(python_project)
        fetch_calls = [
            item for item in python_result.calls if item.fact.name == "fetch"
        ]
        self.assertEqual(len(fetch_calls), 1)
        app_fetch = next(
            item for item in fetch_calls if item.fact.span.file == "app.py"
        )
        self.assertEqual(app_fetch.status, ResolutionStatus.RESOLVED)
        self.assertEqual(app_fetch.target.file, "a/client.py")
        assert_cardinality(self, python_project, python_result)

        java_root = FIXTURES / "java"
        java_sources = tuple(
            snapshot(path, Language.JAVA, java_root)
            for path in sorted(java_root.rglob("*.java"))
        )
        java_project = extract_project(Path("/repo"), java_sources)
        java_result = resolve_project(java_project)
        fetch = next(
            symbol_
            for file in java_project.files
            for symbol_ in file.symbols
            if symbol_.name == "fetch" and symbol_.kind is SymbolKind.METHOD
        )
        self.assertIn("Bean", fetch.annotations)
        main = next(
            symbol_
            for file in java_project.files
            for symbol_ in file.symbols
            if symbol_.name == "main"
        )
        self.assertIn("static", main.modifiers)
        callbacks = [
            item
            for item in java_result.references
            if item.fact.name == "fetch"
            and item.fact.context is ReferenceContext.ANNOTATION
            and item.fact.confidence is ReferenceConfidence.POSSIBLE
        ]
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0].target, fetch.id)
        self.assertFalse(
            any(
                item.fact.context is ReferenceContext.STRING
                and item.fact.name == "fetch"
                for item in java_result.references
            )
        )
        assert_cardinality(self, java_project, java_result)

        ts_root = FIXTURES / "typescript"
        ts_sources = tuple(
            snapshot(path, Language.TYPESCRIPT, ts_root)
            for path in sorted(ts_root.rglob("*.ts"))
        )
        ts_project = extract_project(Path("/repo"), ts_sources)
        ts_result = resolve_project(ts_project)
        configs = [
            item
            for item in ts_result.references
            if item.fact.name == "onReady"
            and item.fact.context is ReferenceContext.CONFIG
        ]
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].fact.confidence, ReferenceConfidence.POSSIBLE)
        self.assertEqual(configs[0].status, ResolutionStatus.RESOLVED)
        self.assertFalse(
            any(
                item.fact.name == "onReady"
                and item.fact.context is ReferenceContext.STRING
                for item in ts_result.references
            )
        )
        assert_cardinality(self, ts_project, ts_result)


if __name__ == "__main__":
    unittest.main()
