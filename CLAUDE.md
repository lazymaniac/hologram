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
3. **render** — `render_simple` owns *all* layout: the directory trie, same-shape type
   grouping, receiver resolution through `bindings`, transitive reduction of call
   chains, prefix-factored private names, the test index, markers (`✓ ×0 ~N !E`).
   No extractor formats output.

`build_digest` = `_gather` (scan + extract + state hash) → level-invariant call/
usage/helper precomputation → one or more complete `render_simple` candidates.

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
  output. Under a fixed Python/parser toolchain, the same sources and settings
  produce the same digest; the state hash does not detect dependency upgrades.
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
This is a hologram map of this repository: a deterministic index of its public API — signatures, fields, call chains, private names, test locations. Read it before exploring to find what exists and open the right file first. Line 2 is the legend. Before writing tests or helpers, check `? tests` for existing coverage and *-marked helpers. When `hologram review` reports findings, address them before finishing: reuse the named original instead of a duplicate; consolidate re-covered tests.

```
# hologram · 15,861 LOC · state a590c58f03cb
· C/R/I{fields} · f(args):Ret > project calls · -=private · ?=tests · ✓=tested · ~N=lines · !E=throws · @=route/annotation · = consts · p{a,b}=pa,pb · {a,b}s=as,bs · +N=more
benchmark
 claude_runner(prompt,ws,model,max_turns,effort):RunnerOutcome > _effort_invocation,_run_process
 drop_workspace(corpus,ws) ✓
 judge_reuse(before,after,expect_reuse):dict ✓ > bench._sig_lines,_fn_name,_chain
 judge_scope(ws,expect,test_only,setup_sha):bool | None ✓ > _added_lines
 load_tasks(path):Config ~52 ✓ !SystemExit,TypeError,ValueError > Config,_validate_config,Task
 main(argv):int ~191 ✓ !SystemExit > load_tasks,_validate_matrix,_preflight_runner,_counterbalanced_schedule,_run_review_hook,_read_rows,_atomic_replace,_append_jsonl_atomic,run_one,_resumable_block,_setup_failure_row,report
 make_workspace(corpus,ws,condition,lang,budget):Path ~76 ✓ !ValueError,RuntimeError > _safe_context_targets,_record_clean_setup,_block_span,_contained_target
 parse_transcript(text):dict ~88 ✓ > _acted_on_findings,_review_tool_results
 report(rows,anon):str ~129 ✓ > _latest_cells,_anonymous_task_labels,_percent_cell,_matched_section,_review_section,_group_key,_infra_reason,_automatic_acceptance_applicable,_reuse_applicable,_short_identity,_median_mad,_numbers,_task_label
 run_one(corpus,task,condition,rep,results_dir,model,max_turns,runner,lang,budget,effort,config_revision,experiment_id,cell_id,pair_id,order_seed,condition_order,order_index,runner_mode,host_execution_acknowledged,expected_corpus_revision,experiment_conditions,experiment_reps,experiment_tasks,execute_acceptance,runner_provenance,wave_id,wave_started_at,execution_index,block_index):dict ~240 ✓ !ValueError,RuntimeError > _validate_config,_cell_spec,_utc_now,Config,_adhoc_config_revision,_runtime_provenance,_experiment_spec,make_workspace,_setup_sha,_digest_of,_embedded_map_info,_invoke_runner,_persist_outcome,judge_reuse,judge_scope,parse_transcript,_semantic_result,_empty_review_measurement,_run_acceptance,RunnerOutcome,_classify_acceptance,_resolved_model,drop_workspace,_install_review_capture,_review_final_state
 Config(R{corpus,tasks,model,max_turns,lang,budget,effort,revision})
 RunnerOutcome(R{stdout,stderr,returncode,duration_seconds,timed_out,status,error})
  ok():bool @property
  - __post_init__
 Task(R{id,kind,prompt,accept_cmd,expect_reuse,expect_answer,expect_in_new_code,scope_in_tests,max_turns,effort,manual_only,judge,accept_pass_codes,accept_fail_codes,semantic_judge})
 - bench.py: _safe_context_targets,_validate_config,_validate_matrix,{_adhoc_config,_task,_judge_config,_corpus}_revision,
             _identity,_content_text,_review_{tool_results,final_state,section},_acted_on_findings,_sig_lines,_fn_name,
             _chain,_setup_sha,_record_clean_setup,_added_lines,_effort_invocation,_terminate_process_group,
             _run_{process,acceptance,review_hook},_as_text,_terminal_{result,transcript_error,protocol_error,cell},
             _apply_terminal_status,{_invoke,_preflight,_dry}_runner,_classify_acceptance,_excerpt,_persist_outcome,
             _fsync_directory,_artifact_matches,_resume_evidence_intact,_embedded_map_info,_digest_of,_utc_now,
             _runtime_provenance,_experiment_spec,_cell_spec,_counterbalanced_schedule,_infra_reason,_resumable_block,
             _read_rows,_atomic_replace,_append_jsonl_atomic,_empty_review_measurement,_install_review_capture,
             _read_review_capture,_agent_commit_ids,_resolved_model,_semantic_result,
             {_legacy_cell,_group,_legacy_pair}_key,_latest_cells,_numbers,_median_mad,_median_mad_n,_percent_cell,
             _automatic_acceptance_applicable,_reuse_applicable,_short_identity,_anonymous_task_labels,_task_label,
             _matched_section,_eligible_review_measurement,_setup_failure_row
hologram
 build_digest(root,langs,targets,budget):str ✓ > _build_digest
 build_digest_with_stats(root,langs,targets,budget):tuple[str,BudgetStats] > _build_digest
 const_signature(name,value_text):str ✓
 context_targets(root):list[Path]
 detect_language(path):str | None ✓
 embed_digest(path,digest) > _embed_block,_block_span,_seed_content
 embedded_digest(path):str ✓ > _block_span
 estimate_tokens(text):int ✓
 has_parser(lang):bool
 main() !SystemExit > run_cli
 render_report(findings,rev):str ✓
 render_simple(root,symbols,files,state,zero_usage,langs,targets,file_tokens,detail,budget,loc,resolved,helpers,budget_selection,budget_catalog,budget_retained):str ~654 ✓ > _tree_lines,_test_index_lines,_legend_line,BudgetBundle,_resolved_project_calls,_bundle_estimated_chars,_decorator_notes,_total_loc,_helper_class_ids,_edge_suffix,_is_production_symbol,_bundle_key,_essential_method,_private_lines,_is_test_path,_strip_exc
 report_data(findings,rev):dict[str,object] ✓
 review_snapshots(old,new,old_digest,checks):list[Finding] ~199 ✓ > _prod_api,_raw_call_targets,_test_edges,_prod_callables,_zero_usage_names,_map_line_for,_describe,_finding,_key,_is_test_path,_is_production_symbol,_decorator_notes,review._symbol_identity,_api_delta
 run_cli(argv):int ~321 ✓ !SystemExit > context_targets,_state_hash,scan_files,_missing_parser_langs,_uninstall,_bootstrap_or_die,run_review,build_digest_with_stats,_install_hooks,_dead_hook_scripts,_warn_if_large,embed_digest,_contained_target,_resolve_target_restriction,_digest_langs,_digest_targets,_revision_source_paths,embedded_digest,_digest_budget,_strip_block,_digest_state,run_review_data,estimate_tokens
 run_review(root,rev,langs,checks):str > render_report,run_review_findings
 run_review_data(root,rev,langs,checks):dict[str,object] > report_data,run_review_findings
 run_review_findings(root,rev,langs,checks):list[Finding] !SystemExit > snapshot,review_snapshots,build_digest,_git_env
 scan_files(root):list[Path] > detect_language,_git_env
 snapshot(root,langs):Snapshot > _gather,Snapshot
 split_params(raw):list[str] > _split_top_commas,tight_type
 strip_comments_and_strings(text):str
 summarize_budget(requested_budget,full_tokens,selected_tokens,skeleton_tokens,effective_detail,bundles,retained,selection_trials,selection_candidates,search_truncated,stop_reason):BudgetStats > BudgetStats
 tight_annotation(text):str > tight_type
 tight_type(t):str
 BudgetBundle(R{detail,category,key,estimated_chars})
  name():str @property
 BudgetStats(R{policy_version,requested_budget,full_tokens,selected_tokens,skeleton_tokens,effective_detail,utilization,fits,retained_categories,dropped_categories,retained_bundles,dropped_bundles,selection_trials,selection_candidates,search_truncated,stop_reason})
  as_dict():dict[str,object]
 Finding(R{check,subject,detail,kind,path,_discriminator,id})
  to_dict():dict[str,str | None]
  - __post_init__
 Snapshot(R{symbols,file_tokens,usage_tokens})
 Symbol(R{name,kind,file,line,signature,params,param_names,returns,visibility,container,lang,fields,calls,supers,permits,raises,bindings,decorators,size})
 = cli.py: HOOK_NAMES
 = embed.py: CONTEXT_FILES,CONTEXT_DIRS
 = render.py: KIND_LETTER
 = symbols.py: LANG_EXTENSIONS,DENYLIST_DIRS,TYPE_KINDS,ROUTE_DECORATORS,MARKER_DECORATORS
 - bootstrap.py: _pyz_path,_tool_anchor,_venv_python,_missing_parser_langs,_venv_has_grammars,_bootstrap_or_die
 - cli.py: _hook_python,_sh_dq,_tool_invocation,_managed_hook_line,_dead_hook_scripts,_remove_managed_hook_lines,
           _install_hooks,_strip_block,_uninstall,_warn_if_large,_contained_target,_resolve_target_restriction,
           _revision_source_paths
 - embed.py: _embed_block,_block_span,_seed_content,_target_candidates
 - gather.py: _git_env,_generator_fingerprint,_new_state_hash,_gather,_state_hash,_digest_{state,langs,budget,targets},
              _framework_invoked,_zero_usage_names
 - render.py: _test_stem,_is_test_path,_is_production_symbol,{_tree,_private,_braced,_test_index}_lines,_strip_exc,
              _total_loc,_symbol_identity,_target_descriptions,_raw_call_targets,_resolved_project_calls,_BudgetSelection,
              _bundle_key,_bundle_estimated_chars,_essential_method,_decorator_notes,_factored_name_tokens,
              _helper_class_ids,_informative_targets,_edge_suffix,_legend_line,_build_digest
 - review.py: _key,_symbol_identity,_finding,_prod_callables,_ApiAtom,_api_{atom,signature,values,atom_label,delta},
              _prod_api,_test_edges,_sig_lines,_map_line_for,_describe,_finding_order
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
  - misc.py: _lua_call_entry,_extract_{lua,bash,css,html,helm,make},_bash_call_entry,_css_symbols,
             _make_{extend_unique,continues,without_comment,syntax_without_comment,rule_parts,recipe_prefixes,reference_end,reference_name,var_refs}
  - php.py: _php_{vis,var_name,params,return,call_entry,local_bindings,raises,attributes,fn_symbol},_extract_php
  - python.py: _py_{param_facts,calls,raises,bindings,decorators,fn_symbol},_extract_python
  - ruby.py: _rb_{call_entry,params,method_symbol,fields,walk},_extract_ruby
  - rust.py: _rs_{vis,params,param_names,call_entry,local_bindings,attributes,fn_symbol},_extract_rust
  - scala.py: _sc_{vis,params,return,call_entry,local_bindings,raises,fn_symbol},_extract_scala
  - swift.py: _sw_{vis,params,return,call_entry,local_bindings,raises,fn_symbol},_extract_swift
  - ts.py: _ts_{exported,params,param_names,return,call_entry,calls,decorators,param_bindings,class_bindings,param_bindings_one,local_bindings,fn_symbol,unwrap_hoc,fc_props,route_entries,top_level_arrows,aliases_and_reexports},
           _extract_{ts,tsx,sfc}
tools
 main(argv):int
? tests ·.py
 test_bench{TaskLoaderTest>load_tasks+1,TranscriptMetricsTest>parse_transcript,DuplicationDetectorTest>judge_reuse,
            ExperimentIdentityTest>_experiment_spec+7,WorkspaceTest>make_workspace+2,
            BudgetConditionTest>make_workspace+3,ActedOnFindingsTest>parse_transcript,
            ReviewConditionTest>make_workspace+3,StructuredReviewMeasurementTest>Finding+9,
            CoachConditionTest>make_workspace+1,RunOneTest>_infra_reason+3,ScopeJudgeTest>judge_scope,
            ReportTest>_review_section+1,CliTest>bench.main+6}
 test_cli{{CliOptionSurface,CliBuild,InitHooks,InitLang,HookQuoting,TargetOption,LangFilterPersistence,Bootstrap,PrintCommand,Uninstall,UninstallPython,SizeWarning}Test>run_cli,
          ManagedHookLineTest>_sh_dq+1,LegacyHookMigrationTest>run_cli+1,PostCommitHookE2ETest>run_cli,
          BudgetTest>build_digest+4,HookPythonSelectionTest}
 test_extract_langs{{PythonExtract,TypeScriptExtract,ArrowFunction,DecoratorExtract}Test>extract_file}
 test_freshness_and_markers{StateAndCheckTest>run_cli+1,{TestedMarker,SizeMarker,TestIndex}Test>build_digest,
                            DisplayNameTest>render_simple+1,TestHelperTest>build_digest+2,
                            {Embed,ContextTargets,DiffCommand}Test>run_cli}
 test_more_langs{{Go,CSharp,Bash,Css,Html,Helm,Kotlin,Tsx,Sfc,Ruby,Swift,Scala}ExtractTest>extract_file,
                 {Rust,Cpp,Lua,Angular,Php}ExtractTest>extract_file+1,
                 {CExtract,HtmlNestedBlocks,TsGaps,TsLossRecovery}Test>extract_file,ReactComponentTest>extract_file+1,
                 MakefileExtractTest>extract_file+3}
 test_review{{Dup,Recover,Place}CheckTest>review_snapshots,DeadOrphanApiTest>review_snapshots+1,
             ReportAndCliTest>render_report+4} > Snapshot +1
 test_simple_mode{CallExtractionTest>extract_file,
                  {SimpleDigest,SameShapeGrouping,FieldNames,ReconstructablePath,Legend,ConstExtract,TightFormat}Test>build_digest,
                  RenderUnitTest>Symbol+2,{EnumValues,InterfaceMethod,QualifiedCall,Throws}Test>extract_file+1,
                  LanguageFilterTest>build_digest+2,RelationsTest>extract_file+2,
                  {InterfaceImplementors,TransitiveReduction,GroupExtras,CompactMapContract,TestIndexDiet}Test>Symbol+1,
                  SecretRedactionTest>build_digest+1,{RouteRender,CtorSuppression,DunderPrivate}Test>render_simple+1,
                  VoidOmissionTest>extract_file,PrivateMembersTest>Symbol+2,ZeroUsageMarkerTest>build_digest+2,
                  PrecomputedRenderTest>Symbol+4} > Symbol
 test_treesitter{TreeSitterJavaTest,MissingParserErrorTest}
```
<!-- hologram:end -->
