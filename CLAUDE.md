# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`hologram.py` — one self-contained Python file (no runtime deps beyond optional
tree-sitter grammars) that compresses a codebase into a compact markdown map for LLM
context. `benchmark/bench.py` — a headless-agent harness that measures whether the map
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

Self-hosting (this repo's own map lives in `PROJECT_DIGEST.md`, gitignored, and in the
generated block at the bottom of this file):

```bash
python3 hologram.py build --root .            # rebuild file + embedded block
python3 hologram.py check --root .            # exit 0 fresh / 1 stale
python3 hologram.py diff HEAD~1 --root .      # API drift vs a revision
```

Git hooks (post-commit/merge/checkout) rebuild both copies automatically, so a digest
change shows up as part of the next commit's diff.

Benchmark (costs real money — headless `claude -p` sessions):

```bash
.venv/bin/python benchmark/bench.py run benchmark/tasks/spring.json --only <id> --reps 1
.venv/bin/python benchmark/bench.py report
```

## Architecture

One pipeline, three stages, all in `hologram.py`:

1. **scan** — `scan_files` returns git-tracked source files when the root is a repo
   (so `.gitignore` prunes vendored trees), else a denylist-pruned walk. Order is
   deterministic; everything downstream depends on that.
2. **extract** — `extract_file` dispatches on extension via `LANG_EXTENSIONS` →
   `EXTRACTORS` → `_extract_<lang>`. Each extractor is independent and emits the same
   language-neutral `Symbol` dataclass (name/kind/signature/params/calls/supers/
   raises/`bindings`/`size`). Python uses stdlib `ast`; everything else uses
   tree-sitter; Helm uses a narrow template scanner.
3. **render** — `render_simple` owns *all* layout: the directory trie, same-shape type
   grouping, receiver resolution through `bindings`, transitive reduction of call
   chains, prefix-factored private names, the test index, markers (`✓ ×0 ⋮N !E`).
   No extractor formats output.

`build_digest` = `_gather` (scan + extract + state hash) → `_dep_lines` →
`render_simple`.

Consequences worth knowing before editing:

- **The state hash includes the tool's own bytes** (`_generator_fingerprint`). Any edit
  to `hologram.py` makes every previously generated digest stale everywhere — that's
  deliberate (extraction/rendering changes must invalidate old maps), but it means the
  post-commit hook rewrites `PROJECT_DIGEST.md` and this file's block on every commit
  that touches the tool. `_state_hash` recomputes the same value without parsing, which
  is what makes `check` and `--if-stale` cheap; it must stay byte-identical in method to
  the hash `_gather` accumulates.
- **Determinism is the contract.** No LLM, no timestamps, no set iteration leaking into
  output. Same sources → same digest, so a digest diff always means the code changed.
- **Output size is a representation decision, never truncation.** When adding a fact to
  the digest, pay for it by compressing elsewhere (names not types, fields not field
  types, factored prefixes, file/class-only test index). Format decisions were measured
  with an o200k tokenizer.
- **Language depth varies on purpose** — see the README table. Don't promise a language
  more than its extractor delivers.

### Embedding into CLAUDE.md

`embed_digest` splices the map between the managed `hologram:start` / `hologram:end`
HTML-comment markers, preserving everything outside them. Hand-written guidance in this
file (i.e. everything above the block) survives rebuilds; the block itself is generated —
edit `hologram.py` or the sources, not the block. `--no-embed` removes it via
`remove_embedded_digest`.

Never write either marker literally in prose above the block: `embed_digest` splits on
the *first* end marker in the file, so a literal mention before the real block makes the
next rebuild duplicate content. Refer to them as `hologram:start` / `hologram:end`, as
above.

### Parser bootstrap

When a scanned language needs a tree-sitter grammar that isn't importable,
`_bootstrap_or_die` re-execs into `.venv` next to `hologram.py` if that venv has the
grammars, else offers (interactive only) to create it and pip-install them, guarded by
`HOLOGRAM_BOOTSTRAPPED` against exec loops. `_install_hooks` writes hook lines using
that venv's python when it exists, and `_managed_hook_line` recognizes hook lines from
older versions so reinstalls replace rather than duplicate.

## Adding a language

Register in four places, then test: `LANG_EXTENSIONS` (extension → lang),
`_GRAMMAR_MODULES` + `_PARSERS` (tree-sitter module/pip package), `EXTRACTORS`
(lang → `_extract_<lang>`). The extractor's only job is producing `Symbol`s —
visibility, `bindings` (var/param/field → declared type, which is what turns
`engine.evaluate` into `PricingEngine.evaluate`), and `size`. Add a fixture under
`tests/fixtures/polyglot/` and a test in `tests/test_more_langs.py` guarded by a
`skipUnless(hologram.has_parser(...))` decorator.

Note `DENYLIST_DIRS` includes `fixtures`, `testdata`, and `resources` — tests that build
a digest over fixture trees pass the fixture root directly rather than relying on a scan
from above it.

## Benchmark conditions

`bench.py` builds a detached worktree per run: **A** = digest on disk plus query
instructions (pull model), **B** = control, **C** = digest embedded in CLAUDE.md (push
model). Results published in-repo cover public corpora only; the private-corpus numbers
stay out of tracked files — keep it that way when editing `README.md` or
`benchmark/*.md`.

<!-- hologram:start — generated, do not edit; refreshed by git hooks -->
```
# hologram · 5,097 LOC · state a050fef13513
· C/R/I{fields} E{values} T:target · f(args):Ret > project calls · -=private · ?=tests · ×0=no static use · ✓=tested · ⋮N=lines · !E=throws · p{a,b}=pa,pb
build_digest(root,langs):str ✓ > _gather,_dep_lines,render_simple,_zero_usage_names
detect_language(path):str | None
embed_digest(claude_path,digest):str ✓ > _embed_block
estimate_tokens(text):int
extract_file(path,root,text):list[Symbol] ✓ !SystemExit > detect_language,has_parser,_grammar_pkgs
has_parser(lang):bool ✓
remove_embedded_digest(claude_path):bool
render_simple(root,symbols,files,state,deps,zero_usage):str ⋮192 ✓ > _resolved_project_calls,_total_loc,_tree_lines,_test_index_lines,_private_lines,_is_test_path,_strip_exc
run_cli(argv):int ⋮105 ✓ !SystemExit > scan_files,_missing_parser_langs,build_digest,_digest_state,_state_hash,_embedded_digest_matches,_bootstrap_or_die,_install_hooks,embed_digest,remove_embedded_digest,estimate_tokens
scan_files(root):list[Path] > detect_language
split_params(raw):list[str] > _split_top_commas,tight_type
strip_comments_and_strings(text):str
tight_type(t):str
Symbol(C{name,kind,file,line,signature,params,param_names,returns,visibility,container,lang,fields,calls,supers,permits,raises,bindings,size})
- hologram.py: _parse_throws,_split_top_commas,_base_type,_heritage,_load_parser,_grammar_pkgs,
               _ast_{text,field,collect,calls,modifiers,param_types,vis},_body_lines,
               _java_{param_names,call_entry,calls,param_bindings,class_bindings,local_bindings,method_symbol},
               _extract_{java,ts,tsx,sfc,go,rust,cs,kotlin,c,cpp,lua,html,helm,python},
               _ts_{exported,params,param_names,return,call_entry,calls,param_bindings,class_bindings,param_bindings_one,local_bindings,fn_symbol,top_level_arrows,aliases_and_reexports},
               _go_{vis,type_text,params,param_names,result,call_entry,local_bindings},
               _rs_{vis,params,param_names,call_entry,local_bindings,fn_symbol},
               _cs_{vis,params,param_names,call_entry,local_bindings},
               _kt_{vis,params,param_names,return,call_entry,fn_symbol},
               _c_{fn_declarator,params,param_names,field_names,call_entry,static,enum_symbol},_lua_call_entry,
               _py_{param_facts,calls,raises,bindings,fn_symbol},_generator_fingerprint,_new_state_hash,_gather,
               _state_hash,_digest_state,_zero_usage_names,_is_test_path,_tree_lines,_strip_exc,_dep_lines,_total_loc,
               _symbol_identity,_target_descriptions,_resolved_project_calls,_factored_name_tokens,_factored_names,
               _private_lines,_braced_lines,_test_index_lines,_embed_block,_embedded_digest_matches,_venv_python,
               _missing_parser_langs,_venv_has_grammars,_bootstrap_or_die,_hook_python,_managed_hook_line,_install_hooks
benchmark
 claude_runner(prompt,ws,model,max_turns):str
 drop_workspace(corpus,ws) ✓
 judge_reuse(before,after,expect_reuse):dict ✓ > _sig_lines,_fn_name,_chain
 load_tasks(path):Config ✓ !SystemExit > Config,Task
 main(argv):int ⋮44 ✓ > load_tasks,report,run_one
 make_workspace(corpus,ws,condition):Path ✓
 parse_transcript(text):dict ⋮42 ✓
 report(rows):str ✓
 run_one(corpus,task,condition,rep,results_dir,model,max_turns,runner):dict ✓ > make_workspace,_digest_of,judge_reuse,parse_transcript,drop_workspace
 Config(C{corpus,tasks,model,max_turns})
 Task(C{id,kind,prompt,accept_cmd,expect_reuse})
 - bench.py: _sig_lines,_fn_name,_chain,_digest_of,_dry_runner
? tests
 test_bench.py{TaskLoaderTest,TranscriptMetricsTest,DuplicationDetectorTest,WorkspaceTest,RunOneTest,ReportTest,CliTest}
 test_cli.py{CliBuildTest,InitHooksTest,InitLangTest,BootstrapTest,HookPythonSelectionTest}
 test_extract_langs.py{PythonExtractTest,TypeScriptExtractTest,ArrowFunctionTest}
 test_freshness_and_markers.py{StateAndCheckTest,TestedMarkerTest,SizeMarkerTest,TestIndexTest,DepsMapTest,EmbedTest,
                               DiffCommandTest}
 test_more_langs.py{GoExtractTest,RustExtractTest,CSharpExtractTest,CExtractTest,CppExtractTest,LuaExtractTest,
                    HtmlExtractTest,HelmExtractTest,KotlinExtractTest,TsGapsTest,TsxExtractTest,SfcExtractTest}
 test_simple_mode.py{CallExtractionTest,SimpleDigestTest,SameShapeGroupingTest,RenderUnitTest,EnumValuesTest,
                     InterfaceMethodTest,QualifiedCallTest,FieldNamesTest,ReconstructablePathTest,LanguageFilterTest,
                     RelationsTest,LegendTest,ThrowsTest,TransitiveReductionTest,VoidOmissionTest,GroupExtrasTest,
                     PrivateMembersTest,CompactMapContractTest,TightFormatTest,ZeroUsageMarkerTest}
 test_treesitter.py{TreeSitterJavaTest,MissingParserErrorTest}
```
<!-- hologram:end -->
