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
python3 tools/run_tests.py --profile core                  # dependency-free suite
.venv/bin/python tools/run_tests.py --profile full         # all grammars, zero skips
.venv/bin/python -m unittest tests.test_simple_mode        # one module
.venv/bin/python -m unittest tests.test_simple_mode.RenderUnitTest.test_name   # one test
```

Run from the repo root; tests insert the root on `sys.path` themselves. The core
profile allows optional-language skips. The full profile preflights every parser
and rejects any skip.

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
.venv/bin/python benchmark/bench.py run benchmark/tasks/spring.json --only <id> --reps 1 --allow-unsafe-host
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
   stdlib `ast`; languages registered in `_GRAMMAR_MODULES` use tree-sitter;
   HTML, CSS, Helm, Make, Bash, and Lua use narrow scanners.
3. **render** — `render_simple` owns *all* layout: exact-file/path-compressed tries,
   lossless cross-file grouping for conventional one-type files, receiver resolution
   through `bindings`, transitive reduction of call chains, name factoring, compact
   test/tool/benchmark landmarks, and markers (`×0 ~N !E`). No extractor formats
   output.

`build_digest` = `_gather` (scan + extract + state hash) → level-invariant call/
usage/helper precomputation → full and semantic-floor renders → globally ranked,
dependency-closed whole-fact restoration when a budget binds. Every budget trial is
a complete `render_simple` candidate; no fact is byte-truncated.

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
  `_digest_state`. Current maps put volatile LOC/state/settings in the footer to keep
  the semantic prefix cache-stable; readers must continue accepting legacy
  metadata-bearing headers. CLAUDE.md is the only place a generated map is stored.
- **Determinism is the contract.** No LLM, no timestamps, no set iteration leaking into
  output. Under a fixed Python/parser toolchain, the same sources and settings
  produce the same digest; the state hash does not detect dependency upgrades.
- **Output size is a representation decision, never truncation.** When adding a fact to
  the digest, pay for it by compressing elsewhere (names not types, fields not field
  types, lossless prefix/suffix factoring, one landmark per test/support file). A
  private inventory name may disappear only when the same exact target remains visible
  in a retained call chain. Format decisions are checked against fixture token
  baselines; `tools/measure_tokens.py` provides optional o200k measurement.
- **The repeated extension is declared, never dropped.** `_dominant_suffix` picks the one
  extension worth stating on the title line (`# hologram ·.py`) and `_hoist_tree_keys`
  removes it from file trie nodes only. It refuses a stem another node already owns
  (`extract/` beside `extract.py`) and a stem still holding a dot (`shell.component` would
  read as an extension), and it counts only leaves it can actually strip, so no
  declaration is made where it does not pay. Grouped landmarks
  (`{ItemId,OrderId}.java(R{value})`) keep their extension on purpose: payload that names
  files is what tells a reader the bare node above it is their directory rather than the
  file itself. Any change here must keep every rendered node resolvable to exactly one
  real path.
- **Three facts were removed on purpose; do not reintroduce them.** Constructors state
  that a type can be constructed, which its own header already implies — 4.6% of a large
  map for it. `✓` marked reachability from a test: it discriminated between 0.3% and 42%
  of callables depending on the corpus, and two thirds of its cost was the grouping it
  prevented rather than the marker. Individual test case names were a third of a large
  map for a fact read in the file the landmark points at. The reachability behind `✓` is
  still computed — the budget ranks a tested call path above every other chain — it is
  simply not rendered.
- **The map is push-only by design.** Do not add a query/retrieval step, generated
  side-index, or coaching that requires the agent to remember a tool call. The product
  distinction is maximum useful business-logic semantics already present at session
  start.
- **Two floors, one ladder.** `_FLOOR_LEVEL` (L7) is the deepest level at which *which
  facts* render changes; `_MAX_LEVEL` (L8) renders exactly those facts stripped of
  every non-project annotation (types, decorators, throws, `~N`/`×0`, field lists,
  supers/implementors/sealed). L8 is a representation change, never another fact drop,
  so a bundle level is `_FLOOR_LEVEL` and only rendering reads `_MAX_LEVEL`. A budget
  takes L8 only when L7 overflows; `BudgetStats.skeleton_tokens` keeps meaning L7.
- **Budget means digest budget.** `adaptive-bundles-v2` starts from a compact pushed
  floor, ranks optional facts by tested/cross-file value, fan-in, and file breadth,
  closes method-call dependencies, and verifies every admission by full rendering.
  Wrapper and coaching costs are exposed separately by `managed_context_cost`, CLI
  output, stats JSON, and benchmark rows; evolve these schemas additively.
- **Features gate eligibility; the budget gates cost.** `FEATURES` in `symbols.py` is
  the catalog `--features` accepts; `render_simple`'s local `_on()` gates each fact
  class at its render site. A deselected class must never reach `_keep_bundle`, so the
  budget search cannot restore what the user removed — gate before cataloging, not
  after. The always-on remainder (package trie, type headers, public signatures — names
  and arguments; `types` carries the annotations) is the map's identity and is not
  selectable. `_legend_line` derives clauses from the rendered
  body precisely so a selection prunes the legend for free; keep it that way rather than
  passing the selection in. `--features all` and an omitted flag are the same map and
  stamp nothing; the empty selection stamps `none` because absent and empty differ.
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
rebuilds; the block itself is generated — edit the package/source files, never the
logic-free `hologram.py` shim or the block.

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

`bench.py` builds a detached local clone per run: **A** = map only (the shipped
coaching sentence removed), **AC** = map plus shipped coaching, **AR** = shipped
`init` behavior with map, coaching, and live hooks, **B** = control. Private task
configurations, prompts, paths, transcripts, results, and derived aggregates
stay outside tracked files and release artifacts. Anonymization alone does not
grant publication permission.

<!-- hologram:start — generated, do not edit; refreshed by git hooks -->
Hologram project map: exact files, signatures, fields, and retained call paths. Read it before searching source. Line 2 is the legend; omissions are marked. Before adding tests/helpers, check `? tests` and `*` helpers. Address `hologram review` findings before finishing; reuse named originals and consolidate duplicate coverage.

```
# hologram ·.py
· C/R/I{fields} · f(args):Ret > calls · -=private · ~N=lines · !E=throws · p{a,b}s=pas,pbs · +N=more
hologram
 bootstrap
  - _pyz_path,_tool_anchor,_venv_{python,has_grammars}
 cli
  main() !SystemExit > run_cli
  run_cli(argv):int ~398 !SystemExit > context_targets,_state_hash,scan_files,_missing_parser_langs,_warn_if_large,_parse_features,_uninstall,_bootstrap_or_die,run_review,build_digest_with_stats,managed_context_cost,_pick_features,_install_hooks,_dead_hook_scripts,embed_digest,_contained_target,_resolve_target_restriction,_digest_{langs,targets,features},_revision_source_paths,embedded_digest,_digest_budget,_strip_block,_digest_state,run_review_data
  = HOOK_NAMES
  - _hook_python,_sh_dq,_tool_invocation,_managed_hook_line,_remove_managed_hook_lines,_feature_checklist,
    _apply_feature_reply
 embed
  context_targets(root):list[Path]
  embed_digest(path,digest) > _embed_block,_block_span,_seed_content
  embedded_digest(path):str > _block_span
  managed_context_cost(digest,include_coaching):ManagedContextCost > estimate_tokens,ManagedContextCost,_embed_block
  ManagedContextCost(R{{digest,wrapper,coaching,managed_block}_tokens})
  = CONTEXT_{FILES,DIRS}
  - _target_candidates
 extract
  __init__
   extract_file(path,root,text):list[Symbol] !SystemExit > detect_language,has_parser,_grammar_pkgs
   = EXTRACTORS
  c_cpp
   - _c_{fn_declarator,params,param_names,field_names,call_entry,static,enum_symbol},_extract_c,_cpp_raises,_extract_cpp
  csharp
   - _cs_{vis,attributes,modifier_names,params,param_names,call_entry,local_bindings,raises},_extract_cs
  go
   - _go_{vis,type_text,params,param_names,result,call_entry,local_bindings},_extract_go
  java
   - _ast_modifiers,_java_annotations,_ast_param_types,_java_param_names,_ast_vis,
     _java_{call_entry,calls,param_bindings,class_bindings,local_bindings,method_symbol},_extract_java
  kotlin
   - _kt_{vis,params,param_names,return,call_entry,raises,local_bindings,annotations,const_symbols,fn_symbol},
     _extract_kotlin
  misc
   - _lua_call_entry,_extract_lua,_bash_call_entry,_extract_bash,_css_symbols,_extract_{css,html,helm},
     _make_{extend_unique,continues,without_comment,syntax_without_comment,rule_parts,recipe_prefixes,reference_end,reference_name,var_refs},
     _extract_make
  php
   - _php_{vis,var_name,params,return,call_entry,local_bindings,raises,attributes,fn_symbol},_extract_php
  python
   - _py_{subscript_head,resolve_forward_refs,annotation,param_facts,calls,raises,bindings,decorators,fn_symbol},
     _extract_python
  ruby
   - _rb_{call_entry,params,method_symbol,fields,walk},_extract_ruby
  rust
   - _rs_{vis,params,param_names,call_entry,local_bindings,attributes,fn_symbol},_extract_rust
  scala
   - _sc_{vis,params,return,call_entry,local_bindings,raises,fn_symbol},_extract_scala
  swift
   - _sw_{vis,params,return,call_entry,local_bindings,raises,fn_symbol},_extract_swift
  ts
   - _ts_{exported,params,param_names,return,call_entry,calls,decorators,param_bindings,class_bindings,param_bindings_one,local_bindings,fn_symbol,unwrap_hoc,fc_props,route_entries,top_level_arrows,aliases_and_reexports},
     _extract_{ts,tsx,sfc}
 gather
  scan_files(root):list[Path] > detect_language,_git_env
  - _generator_fingerprint,_new_state_hash,_digest_metadata_line,_framework_invoked
 render
  build_digest(root,langs,targets,budget,features):str > _build_digest
  build_digest_with_stats(root,langs,targets,budget,features):tuple[str,BudgetStats] > _build_digest
  estimate_tokens(text):int
  render_simple(root,symbols,files,state,zero_usage,langs,targets,file_tokens,detail,budget,loc,source_tokens,resolved,helpers,features,budget_{selection,catalog,retained}):str ~940 > _dominant_suffix,_target_descriptions,{_tree,_test_index}_lines,_legend_line,estimate_tokens,BudgetBundle,_resolved_project_calls,_bundle_estimated_chars,_corpus_size,_hoist_tree_keys,_test_support_ids,_is_production_symbol,_bundle_key,_essential_method,_localized,_decorator_notes,_factored_name_tokens,_private_lines,_is_test_case_method_symbol,_source_role,_shared_suffix,_is_{test_suite_symbol,classless_test_case_symbol,test_path},_strip_exc,_hoisted,_symbol_identity
  summarize_budget(requested_budget,{full,selected,skeleton}_tokens,effective_detail,bundles,retained,selection_{trials,candidates},search_truncated,stop_reason):BudgetStats ~41 > BudgetStats
  BudgetBundle(R{detail,category,key,estimated_chars,source_file,semantic_tier,distinct_file_fanin,reason})
   name():str @property
  BudgetStats(R{policy_version,requested_budget,{full,selected,skeleton}_tokens,effective_detail,utilization,fits,{retained,dropped}_categories,{retained,dropped}_bundles,selection_{trials,candidates},search_truncated,stop_reason,{retained,dropped}_reasons})
   as_dict():dict[str,object]
  = KIND_LETTER
  - _test_stem,_total_loc,_trim_local,_BudgetSelection,_decorator_bases,_is_test_fixture_symbol,_test_reference_counter,
    _helper_class_ids
 review
  render_report(findings,rev):str
  report_data(findings,rev):dict[str,object]
  review_snapshots(old,new,old_digest,checks):list[Finding] ~199 > _prod_api,_raw_call_targets,_test_edges,_prod_callables,_zero_usage_names,_map_line_for,_describe,_finding,_key,_is_{test_path,production_symbol},_decorator_notes,_symbol_identity,_api_delta
  run_review(root,rev,langs,checks):str > render_report,run_review_findings
  run_review_data(root,rev,langs,checks):dict[str,object] > report_data,run_review_findings
  run_review_findings(root,rev,langs,checks):list[Finding] !SystemExit > snapshot,review_snapshots,build_digest,_git_env
  snapshot(root,langs):Snapshot > _gather,Snapshot
  Finding(R{check,subject,detail,kind,path,_discriminator,id})
   to_dict():dict[str,str | None]
   - __post_init__
  Snapshot(R{symbols,{file,usage}_tokens})
  - _ApiAtom,_api_{atom,signature,values,atom_label},_sig_lines,_finding_order
 symbols
  const_signature(name,value_text):str
  detect_language(path):str | None
  split_params(raw):list[str] > _split_top_commas,tight_type
  strip_comments_and_strings(text):str
  tight_annotation(text):str > tight_type
  tight_type(t):str
  Symbol(R{name,kind,file,line,signature,params,param_names,returns,visibility,container,lang,fields,calls,supers,permits,raises,bindings,decorators,size})
  = LANG_EXTENSIONS,DENYLIST_DIRS,TYPE_KINDS,FEATURES,{ROUTE,MARKER,WIRED}_DECORATORS
  - _parse_throws,_base_type,_heritage
 treesitter
  has_parser(lang):bool
  - _load_parser,_ast_{text,field,collect,calls},_body_lines
? tests
 test_adaptive_budget
 test_bench
 test_budget_integrity
 test_cli
 test_extract_langs
 test_freshness_and_markers
 test_more_langs
 test_release_privacy
 test_review
 test_simple_mode
 test_stats_cli
 test_treesitter
tools
 check_release_privacy: main(argv):int;audit_tree(root,terms):list[str];audit_artifact(path,terms):list[str] +1
 measure_tokens: main(argv):int
 run_tests: main(argv):int
benchmark
 bench: main(argv):int;run_one(...);report(rows,anon):str +11
· 19,933 LOC · input 223,264 · output 1,768 tokens · state 5e249e0941cd · budget 6000
```
<!-- hologram:end -->
