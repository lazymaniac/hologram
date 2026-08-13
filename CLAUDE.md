# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`hologram/` — a Python package (no runtime deps beyond optional tree-sitter
grammars) that compresses a codebase into a compact markdown map for LLM context.
`hologram.py` at the repo root is a two-line shim kept so checkout-installed git
hooks and `python3 hologram.py …` keep working; never put logic in it. Releases
also ship the package as a single-file zipapp (`hologram.pyz`).
`benchmark/bench.py` — a headless-agent harness that measures whether the map
actually helps. `tests/` — unittest suites over fixture corpora.

Read `README.md` for the output notation (the digest legend) before changing rendering.

## Commands

```bash
.venv/bin/python -m unittest discover -s tests            # full suite
.venv/bin/python -m unittest tests.test_simple_mode        # one module
.venv/bin/python -m unittest tests.test_simple_mode.RenderUnitTest.test_name   # one test
```

Run from the repo root; tests insert the root on `sys.path` themselves. Plain `python3`
works too — tests for languages whose tree-sitter grammar isn't installed skip via
`unittest.skipUnless(hologram.has_parser(...))`.

Self-hosting (this repo's own map is the generated block at the bottom of this file —
embedding is the only delivery mode; there is no digest file):

```bash
python3 hologram.py build --root .            # rebuild the embedded block
python3 hologram.py check --root .            # exit 0 fresh / 1 stale
python3 hologram.py diff HEAD~1 --root .      # API drift vs a revision
```

Git hooks (post-commit/merge/checkout) rebuild the block automatically, so a map change
shows up as part of the next commit's diff.

Benchmark (costs real money — headless `claude -p` sessions):

```bash
.venv/bin/python benchmark/bench.py run benchmark/tasks/spring.json --only <id> --reps 1
.venv/bin/python benchmark/bench.py report
```

## Architecture

One pipeline, three stages. Module map: `symbols.py` (registry + `Symbol` +
text utils) ← `treesitter.py` (grammar/parser registry, AST helpers) ←
`extract/<lang>.py` ← `gather.py` (scan + state hash) ← `render.py`;
`embed.py` and `bootstrap.py` are side branches; `cli.py` imports everything.
`__init__.py` re-exports the public API plus the private names tests and CI
reach — `_PARSERS` must stay re-exported *by reference* (modules import the
dict and mutate it, never rebind) because tests monkeypatch entries through
`hologram._PARSERS`.

1. **scan** — `scan_files` (in `gather.py`) returns git-tracked source files when
   the root is a repo (so `.gitignore` prunes vendored trees), else a
   denylist-pruned walk. Order is deterministic; everything downstream depends on
   that.
2. **extract** — `extract_file` dispatches on extension via `LANG_EXTENSIONS` →
   `EXTRACTORS` → `extract/<lang>.py:_extract_<lang>`. Each extractor is
   independent and emits the same language-neutral `Symbol` dataclass
   (name/kind/signature/params/calls/supers/raises/`bindings`/`size`). Python uses
   stdlib `ast`; everything else uses tree-sitter; Helm uses a narrow template
   scanner.
3. **render** — `render_simple` owns *all* layout: the directory trie, same-shape type
   grouping, receiver resolution through `bindings`, transitive reduction of call
   chains, prefix-factored private names, the test index, markers (`✓ ×0 ⋮N !E`).
   No extractor formats output.

`build_digest` = `_gather` (scan + extract + state hash) → `_dep_lines` →
`render_simple`.

Consequences worth knowing before editing:

- **The state hash includes the tool's own bytes** (`_generator_fingerprint` hashes
  every `.py` in the package via `importlib.resources`, so checkout/wheel/zipapp
  agree). Any edit to the package makes every previously generated map stale
  everywhere — that's
  deliberate (extraction/rendering changes must invalidate old maps), but it means the
  post-commit hook rewrites this file's block on every commit that touches the tool.
  `_state_hash` recomputes the same value without parsing, which is what makes `check`
  and `--if-stale` cheap; it must stay byte-identical in method to the hash `_gather`
  accumulates. Freshness is read back out of the embedded block by `embedded_digest` +
  `_digest_state` — CLAUDE.md is the only place a generated map is stored.
- **Determinism is the contract.** No LLM, no timestamps, no set iteration leaking into
  output. Same sources → same digest, so a digest diff always means the code changed.
- **Output size is a representation decision, never truncation.** When adding a fact to
  the digest, pay for it by compressing elsewhere (names not types, fields not field
  types, factored prefixes, file/class-only test index). Format decisions were measured
  with an o200k tokenizer.
- **Language depth varies on purpose** — see the README table. Don't promise a language
  more than its extractor delivers.

### Embedding into agent context files

`context_targets` detects which agents a repo already uses: every entry of
`CONTEXT_FILES` that exists as a file (CLAUDE.md, AGENTS.md, AGENT.md, GEMINI.md,
QWEN.md, CONVENTIONS.md, `.clinerules`, `.cursorrules`, `.windsurfrules`,
`.roorules`, `.rules`, `.github/copilot-instructions.md`) plus one managed file
inside each rule *directory* in `CONTEXT_DIRS` (`.cursor/rules/hologram.mdc`,
`.clinerules/hologram.md`, `.junie/guidelines.md`, `.continue/rules/hologram.md`,
`.kiro/steering/hologram.md`, …). Existing files are never created speculatively —
only attached to; a repo with none of them gets CLAUDE.md. New rule files that need
front matter to be loaded get it from `_seed_content` — path-tail seeds
(`_DIR_SEEDS`) first, suffix seeds (`_SEEDS`) as fallback, so Continue's
`hologram.md` is seeded while `.clinerules/hologram.md` stays bare.

`embed_digest` splices the map into each target between the managed `hologram:start` /
`hologram:end` HTML-comment markers, preserving everything outside them, and
`embedded_digest` reads it back. `_block_span` locates the end marker *after* the start
one, so prose that mentions a marker before the block no longer duplicates the block on
rebuild. The block opens with `_EMBED_NOTE`, a short in-band explanation of what the map
is for the agent reading it.

`check` is stale if *any* target lags, so all of a repo's agents move together.
Hand-written guidance in a context file (everything outside the block) survives
rebuilds; the block itself is generated — edit `hologram.py` or the sources, not the
block.

### Parser bootstrap

When a scanned language needs a tree-sitter grammar that isn't importable,
`_bootstrap_or_die` re-execs into the `.venv` next to `_tool_anchor()` (the package's
parent, or the `.pyz`'s directory when zipped) if that venv has the grammars, else
offers (interactive only) to create it and pip-install them, guarded by
`HOLOGRAM_BOOTSTRAPPED` against exec loops. `_install_hooks` writes hook lines via
`_tool_invocation()` — checkout shim path, `-m hologram` for pip installs, or the
`.pyz` path — and `_managed_hook_line` recognizes all forms including v0.1 lines so
reinstalls replace rather than duplicate; `build`/`init` warn when a managed line
points at a script that no longer exists.

## Adding a language

Register in four places, then test: `LANG_EXTENSIONS` in `symbols.py`
(extension → lang), `_GRAMMAR_MODULES` + `_PARSERS` in `treesitter.py`
(tree-sitter module/pip package), `EXTRACTORS` in `extract/__init__.py`
(lang → `_extract_<lang>`, one new module under `extract/`), plus the
`[grammars]` extra in `pyproject.toml`. Dump the fixture's tree before writing
the extractor — don't trust node names from memory. The extractor's only job is
producing `Symbol`s —
visibility, `bindings` (var/param/field → declared type, which is what turns
`engine.evaluate` into `PricingEngine.evaluate`), `decorators` (verbatim,
sigil-stripped — the render layer's allowlists in `symbols.py` decide what
earns tokens), and `size`. Add a fixture under
`tests/fixtures/polyglot/` and a test in `tests/test_more_langs.py` guarded by a
`skipUnless(hologram.has_parser(...))` decorator.

Note `DENYLIST_DIRS` includes `fixtures`, `testdata`, and `resources` — tests that build
a digest over fixture trees pass the fixture root directly rather than relying on a scan
from above it.

## Benchmark conditions

`bench.py` builds a detached worktree per run: **A** = map embedded in the workspace's
`CLAUDE.md`, **B** = control. The pull model (map as a file the agent chooses to grep)
was measured false and is gone from the tool, the harness, and the docs — don't
reintroduce it. Private-corpus numbers stay out of tracked files; keep it that way when
editing `README.md` or `benchmark/*.md`.

<!-- hologram:start — generated, do not edit; refreshed by git hooks -->
This is a hologram map of this repository: a deterministic index of its public API — signatures, fields, call chains, private names, test locations. Read it before exploring to find what exists and open the right file first. Line 2 is the legend.

```
# hologram · 8,021 LOC · state 993ef5888621
· C/R/I{fields} · f(args):Ret > project calls · -=private · ?=tests · ✓=tested · ~N=lines · !E=throws · = consts · p{a,b}=pa,pb · {a,b}s=as,bs
benchmark
 claude_runner(prompt,ws,model,max_turns):str
 drop_workspace(corpus,ws) ✓
 judge_reuse(before,after,expect_reuse):dict ✓ > _sig_lines,_fn_name,_chain
 load_tasks(path):Config ✓ !SystemExit > Config,Task
 bench.py:main(argv):int ~44 ✓ > load_tasks,report,run_one
 make_workspace(corpus,ws,condition):Path ✓
 parse_transcript(text):dict ✓
 report(rows):str ✓
 run_one(corpus,task,condition,rep,results_dir,model,max_turns,runner):dict ✓ > make_workspace,_digest_of,judge_reuse,parse_transcript,drop_workspace
 Config(R{corpus,tasks,model,max_turns})
 Task(R{id,kind,prompt,accept_cmd,expect_reuse})
 - bench.py: _sig_lines,_fn_name,_chain,_digest_of,_dry_runner
hologram
 build_digest(root,langs):str ✓ > _gather,_dep_lines,render_simple,_zero_usage_names
 const_signature(name,value_text):str ✓
 context_targets(root):list[Path]
 detect_language(path):str | None
 embed_digest(path,digest) > _embed_block,_block_span,_seed_content
 embedded_digest(path):str ✓ > _block_span
 estimate_tokens(text):int
 has_parser(lang):bool
 cli.py:main() !SystemExit > run_cli
 render_simple(root,symbols,files,state,deps,zero_usage,langs):str ~231 ✓ > _resolved_project_calls,_total_loc,_tree_lines,_test_index_lines,_legend_line,_decorator_notes,_private_lines,_is_test_path,_strip_exc
 run_cli(argv):int ~124 ✓ !SystemExit > context_targets,_state_hash,scan_files,_missing_parser_langs,build_digest,_warn_if_large,_uninstall,_bootstrap_or_die,_install_hooks,_dead_hook_scripts,embed_digest,_digest_langs,embedded_digest,_digest_state,estimate_tokens
 scan_files(root):list[Path] > detect_language
 split_params(raw):list[str] > _split_top_commas,tight_type
 strip_comments_and_strings(text):str
 tight_type(t):str
 Symbol(R{name,kind,file,line,signature,params,param_names,returns,visibility,container,lang,fields,calls,supers,permits,raises,bindings,decorators,size})
 = cli.py: HOOK_NAMES
 = embed.py: CONTEXT_FILES,CONTEXT_DIRS
 = render.py: KIND_LETTER
 = symbols.py: LANG_EXTENSIONS,DENYLIST_DIRS,TYPE_KINDS,ROUTE_DECORATORS,MARKER_DECORATORS
 - bootstrap.py: _pyz_path,_tool_anchor,_venv_python,_missing_parser_langs,_venv_has_grammars,_bootstrap_or_die
 - cli.py: _hook_python,_sh_dq,_tool_invocation,_managed_hook_line,_dead_hook_scripts,_install_hooks,_uninstall,
           _warn_if_large
 - embed.py: _embed_block,_block_span,_seed_content
 - gather.py: _generator_fingerprint,_new_state_hash,_gather,_state_hash,_digest_state,_digest_langs,_framework_invoked,
              _zero_usage_names
 - render.py: _is_test_path,{_tree,_dep,_private,_braced,_test_index}_lines,_strip_exc,_total_loc,_symbol_identity,
              _target_descriptions,_resolved_project_calls,_decorator_notes,_factored_name_tokens,_legend_line
 - symbols.py: _parse_throws,_split_top_commas,_base_type,_heritage
 - treesitter.py: _load_parser,_grammar_pkgs,_ast_{text,field,collect,calls},_body_lines
 extract
  extract_file(path,root,text):list[Symbol] ✓ !SystemExit > detect_language,has_parser,_grammar_pkgs
  = __init__.py: EXTRACTORS
  - c_cpp.py: _c_{fn_declarator,params,param_names,field_names,call_entry,static,enum_symbol},_extract_c,_cpp_raises,
              _extract_cpp
  - csharp.py: _cs_{vis,attributes,modifier_names,params,param_names,call_entry,local_bindings,raises},_extract_cs
  - go.py: _go_{vis,type_text,params,param_names,result,call_entry,local_bindings},_extract_go
  - java.py: _ast_{modifiers,param_types,vis},
             _java_{annotations,param_names,call_entry,calls,param_bindings,class_bindings,local_bindings,method_symbol},
             _extract_java
  - kotlin.py: _kt_{vis,params,param_names,return,call_entry,raises,local_bindings,annotations,const_symbols,fn_symbol},
               _extract_kotlin
  - misc.py: _lua_call_entry,_extract_{lua,bash,css,html,helm},_bash_call_entry,_css_symbols
  - php.py: _php_{vis,var_name,params,return,call_entry,local_bindings,raises,attributes,fn_symbol},_extract_php
  - python.py: _py_{param_facts,calls,raises,bindings,decorators,fn_symbol},_extract_python
  - ruby.py: _rb_{call_entry,params,method_symbol,walk},_extract_ruby
  - rust.py: _rs_{vis,params,param_names,call_entry,local_bindings,attributes,fn_symbol},_extract_rust
  - scala.py: _sc_{vis,params,return,call_entry,local_bindings,fn_symbol},_extract_scala
  - swift.py: _sw_{vis,params,return,call_entry,local_bindings,fn_symbol},_extract_swift
  - ts.py: _ts_{exported,params,param_names,return,call_entry,calls,decorators,param_bindings,class_bindings,param_bindings_one,local_bindings,fn_symbol,unwrap_hoc,fc_props,route_entries,top_level_arrows,aliases_and_reexports},
           _extract_{ts,tsx,sfc}
tools
 measure_tokens.py:main(argv):int
? tests
 test_bench.py{{TaskLoader,TranscriptMetrics,DuplicationDetector,Workspace,RunOne,Report,Cli}Test}
 test_cli.py{{CliBuild,InitHooks,InitLang,HookQuoting,LangFilterPersistence,Bootstrap,PrintCommand,Uninstall,SizeWarning,HookPythonSelection}Test}
 test_extract_langs.py{{Python,TypeScript,Decorator}ExtractTest,ArrowFunctionTest}
 test_freshness_and_markers.py{{StateAndCheck,TestedMarker,SizeMarker,TestIndex,DepsMap,Embed,ContextTargets,DiffCommand}Test}
 test_more_langs.py{{Go,Rust,CSharp,Cpp,Bash,Lua,Css,Html,Helm,Kotlin,Angular,Tsx,Sfc,Ruby,Php,Swift,Scala}ExtractTest,
                    {CExtract,HtmlNestedBlocks,TsGaps,ReactComponent,TsLossRecovery}Test}
 test_simple_mode.py{{CallExtraction,SimpleDigest,SameShapeGrouping,RenderUnit,EnumValues,InterfaceMethod,QualifiedCall,FieldNames,ReconstructablePath,LanguageFilter,Relations,InterfaceImplementors,Legend,ConstExtract,SecretRedaction,RouteRender,Throws,TransitiveReduction,VoidOmission,GroupExtras,PrivateMembers,CompactMapContract,TightFormat,ZeroUsageMarker}Test}
 test_treesitter.py{TreeSitterJavaTest,MissingParserErrorTest}
```
<!-- hologram:end -->
