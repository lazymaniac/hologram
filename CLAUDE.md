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
   test/tool/benchmark landmarks, and markers (`✓ ×0 ~N !E`). No extractor formats
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
- **The map is push-only by design.** Do not add a query/retrieval step, generated
  side-index, or coaching that requires the agent to remember a tool call. The product
  distinction is maximum useful business-logic semantics already present at session
  start.
- **Two floors, one ladder.** `_FLOOR_LEVEL` (L7) is the deepest level at which *which
  facts* render changes; `_MAX_LEVEL` (L8) renders exactly those facts stripped of
  every non-project annotation (types, decorators, throws, `~N`/`✓`/`×0`, field lists,
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
· C/R/I{fields} · f(args):Ret > calls · ✓=tested · ~N=lines · !E=throws · p{a,b}s=pas,pbs · +N=more
hologram
 cli
  main() !SystemExit > run_cli
  run_cli(argv):int ~398 ✓ !SystemExit > context_targets,_state_hash,scan_files,_missing_parser_langs,_warn_if_large,_parse_features,_uninstall,_bootstrap_or_die,run_review,build_digest_with_stats,managed_context_cost,_pick_features,_install_hooks,_dead_hook_scripts,embed_digest,_contained_target,_resolve_target_restriction,_digest_{langs,targets,features},_revision_source_paths,embedded_digest,_digest_budget,_strip_block,_digest_state,run_review_data
  = HOOK_NAMES
 embed
  context_targets(root):list[Path]
  embed_digest(path,digest) ✓ > _embed_block,_block_span,_seed_content
  embedded_digest(path):str ✓ > _block_span
  managed_context_cost(digest,include_coaching):ManagedContextCost ✓ > estimate_tokens,ManagedContextCost,_embed_block
  ManagedContextCost(R{{digest,wrapper,coaching,managed_block}_tokens})
  = CONTEXT_{FILES,DIRS}
 extract/__init__
  extract_file(path,root,text):list[Symbol] ✓ !SystemExit > detect_language,has_parser,_grammar_pkgs
  = EXTRACTORS
 gather
  scan_files(root):list[Path] > detect_language,_git_env
 render
  build_digest(root,langs,targets,budget,features):str ✓ > _build_digest
  build_digest_with_stats(root,langs,targets,budget,features):tuple[str,BudgetStats] ✓ > _build_digest
  estimate_tokens(text):int ✓
  render_simple(root,symbols,files,state,zero_usage,langs,targets,file_tokens,detail,budget,loc,source_tokens,resolved,helpers,features,budget_{selection,catalog,retained}):str ~994 ✓ > _target_descriptions,_dominant_suffix,{_tree,_test_index}_lines,_legend_line,estimate_tokens,BudgetBundle,_resolved_project_calls,_bundle_estimated_chars,_corpus_size,_hoist_tree_keys,_test_support_ids,_is_production_symbol,_bundle_key,_essential_method,_localized,_decorator_notes,_factored_name_tokens,_private_lines,_is_test_case_method_symbol,_source_role,_shared_suffix,_is_{test_suite_symbol,classless_test_case_symbol,test_path},_strip_exc,_hoisted,_symbol_identity
  summarize_budget(requested_budget,{full,selected,skeleton}_tokens,effective_detail,bundles,retained,selection_{trials,candidates},search_truncated,stop_reason):BudgetStats ~41 ✓ > BudgetStats
  BudgetBundle(R{detail,category,key,estimated_chars,source_file,semantic_tier,distinct_file_fanin,reason})
   name():str @property
  BudgetStats(R{policy_version,requested_budget,{full,selected,skeleton}_tokens,effective_detail,utilization,fits,{retained,dropped}_categories,{retained,dropped}_bundles,selection_{trials,candidates},search_truncated,stop_reason,{retained,dropped}_reasons})
   as_dict():dict[str,object]
  = KIND_LETTER
 review
  render_report(findings,rev):str ✓
  report_data(findings,rev):dict[str,object] ✓
  review_snapshots(old,new,old_digest,checks):list[Finding] ~199 ✓ > _prod_api,_raw_call_targets,_test_edges,_prod_callables,_zero_usage_names,_map_line_for,_describe,_finding,_key,_is_{test_path,production_symbol},_decorator_notes,_symbol_identity,_api_delta
  run_review(root,rev,langs,checks):str > render_report,run_review_findings
  run_review_data(root,rev,langs,checks):dict[str,object] > report_data,run_review_findings
  run_review_findings(root,rev,langs,checks):list[Finding] !SystemExit > snapshot,review_snapshots,build_digest,_git_env
  snapshot(root,langs):Snapshot > _gather,Snapshot
  Finding(R{check,subject,detail,kind,path,_discriminator,id})
   to_dict():dict[str,str | None]
  Snapshot(R{symbols,{file,usage}_tokens})
 symbols
  const_signature(name,value_text):str ✓
  detect_language(path):str | None ✓
  split_params(raw):list[str] > _split_top_commas,tight_type
  strip_comments_and_strings(text):str
  tight_annotation(text):str > tight_type
  tight_type(t):str
  Symbol(R{name,kind,file,line,signature,params,param_names,returns,visibility,container,lang,fields,calls,supers,permits,raises,bindings,decorators,size})
  = LANG_EXTENSIONS,DENYLIST_DIRS,TYPE_KINDS,FEATURES,{ROUTE,MARKER,WIRED}_DECORATORS
 treesitter
  has_parser(lang):bool
? tests
 test_adaptive_budget:AdaptiveBudgetSemanticTest,
   test_{boundary_packing_is_deterministic_complete_and_owner_safe,tested_and_cross_file_paths_precede_local_private_leaves,skeleton_drops_the_test_index_and_a_case_restores_its_file,structure_floor_strips_every_non_project_annotation,budget_under_the_semantic_floor_lands_on_the_structure_floor,selected_member_chain_keeps_owning_method_line,equal_tier_selection_preserves_breadth_across_files,selected_private_inventory_keeps_whole_names_without_duplication,private_helper_remains_fallback_when_full_chain_cannot_fit,every_budget_that_holds_the_skeleton_is_a_hard_ceiling},
   BudgetStatsContractTest,
   test_{stats_are_json_native_and_account_for_every_bundle,trial_cap_reports_unfillable_slack_without_fake_utilization,resolved_payload_ranking_reaches_small_chain_before_cap,smaller_whole_facts_fill_tier_before_oversized_methods,suppressed_record_constructor_is_not_counted_as_a_bundle,duplicate_ownerless_method_is_one_rendered_bundle,tiny_map_is_emitted_whole_when_no_candidate_can_fit,zero_budget_means_unlimited_and_reports_no_utilization}
 test_bench:TaskLoaderTest,
   test_{loads_tasks_and_defaults,missing_required_field_raises,rejects_malformed_expectation_shapes,rejects_task_id_too_long_for_artifact_names,rejects_overlapping_or_malformed_acceptance_codes,loads_manual_and_judge_metadata_for_provenance,every_publishable_task_file_validates},
   TranscriptMetricsTest,test_{counts_and_usage,empty_transcript_gives_zeroes,result_text_and_distinct_files_read},
   DuplicationDetectorTest,test_{reuse_detected,duplicate_detected,new_lines_listed,no_change_is_clean},
   ExperimentIdentityTest,
   test_{setup_failure_marks_structured_review_not_run,counterbalanced_schedule_records_rotating_orders,atomic_jsonl_append_preserves_complete_rows,atomic_append_rejects_preexisting_partial_line,only_fully_observed_cells_are_terminal_for_resume},
   WorkspaceTest,
   test_{condition_a_embeds_the_map,condition_b_is_control,workspace_is_isolated,outside_paths_in_context_files_are_confined,context_symlink_cannot_escape_throwaway_clone,corpus_claude_md_preserved_and_setup_committed},
   BudgetConditionTest,test_{workspace_map_carries_budget_stamp,body_budget_text_cannot_spoof_benchmark_metadata},
   ActedOnFindingsTest,
   test_{edit_of_named_file_then_commit_counts,findings_after_last_commit_do_not_count,seen_without_edit_does_not_count,pathless_findings_fall_back_to_any_edit,shipped_head_parent_revision_counts_as_real_event},
   ReviewConditionTest,test_ar_workspace_has_review_hook_corpus_untouched,
   {test_assistant_mention_is_not,test_user_tool_result_is}_a_review_event,StructuredReviewMeasurementTest,
   test_{missing_hook_event_is_incomplete_not_zero,stale_event_cannot_cover_a_rewritten_commit,ar_no_commit_run_has_complete_zero_measurement,ar_capture_install_failure_stops_before_runner,final_review_runs_before_acceptance_command,final_review_failure_is_measurement_only},
   CoachConditionTest,test_{legacy_long_note_cost_uses_actual_managed_block,ac_keeps_coaching_a_strips_it},RunOneTest,
   test_{full_cycle_with_fake_runner,noop_does_not_satisfy_any_diff_acceptance,new_file_counts_as_change_for_acceptance,runner_timeout_never_accepts_and_is_recorded,terminal_runner_error_never_accepts,turn_limit_is_an_observation_not_an_infra_failure,structured_runner_requires_terminal_result,inconsistent_structured_runner_cannot_claim_ok,terminal_result_requires_usage_and_turns,new_source_file_participates_in_reuse_judging,rerun_artifacts_are_immutable,acceptance_nonzero_captures_stdout_and_stderr,manual_and_semantic_verdicts_are_explicit,acceptance_timeout_is_recorded,acceptance_timeout_kills_background_processes,transcript_saved},
   ScopeJudgeTest,
   test_{name_in_new_file_matches,unchanged_line_does_not_match,empty_expectation_is_none,test_only_excludes_prod_additions},
   ReportTest,
   test_{dry_ar_is_excluded_from_review_measurements,aggregates_by_condition,empty_rows,kind_split_and_answer_column,anon_report_has_no_symbol_names,infrastructure_failures_are_not_averaged,manual_rows_are_pending_not_automatic_successes,latest_duplicate_cell_is_reported_once,matches_every_planned_treatment_against_control,report_makes_dry_and_paid_runner_modes_visible},
   CliTest,
   test_{public_help_hides_internal_review_hook,run_writes_jsonl_and_report_reads_it,dry_run_never_executes_acceptance_shell,resume_skips_only_exact_compatible_terminal_cells,resume_uses_latest_attempt_not_any_older_success,runner_circuit_breaker_stops_matrix}
 test_budget_integrity:WholeMapBudgetSelectionTest,
   test_{unlimited_and_generous_normal_builds_render_once,negative_library_budget_is_rejected,budget_70_does_not_grow_a_tiny_map,semantic_floor_refills_whole_facts_within_budget},
   EntrypointFloorTest,
   test_{framework_methods_survive_cold_and_skeleton_levels,make_targets_are_external_entrypoints_at_the_floor,private_framework_entrypoints_survive_every_level,split_entrypoint_merges_decorator_and_definition_facts,duplicate_private_top_level_routes_are_file_qualified,duplicate_private_route_owners_are_file_qualified},
   OwnerIdentityTest,
   test_{same_named_types_do_not_share_methods,coldness_is_file_qualified,same_named_types_do_not_lend_methods_across_files,make_prerequisites_resolve_to_the_local_makefile,split_go_receiver_attaches_to_unique_package_type,split_cpp_definition_keeps_its_call_chain,split_go_resolution_stays_with_its_package,bash_calls_resolve_within_each_script_owner,make_under_test_directory_remains_production_entrypoint},
   MeaningfulDunderTest,
   test_{protocol_dunders_remain_in_private_inventory,orphan_private_member_keeps_file_scoped_inventory,repeated_orphan_module_owners_use_exact_file_nodes_once,orphan_module_qualified_calls_keep_their_chain},
   ConstructorIntegrityTest,test_only_record_constructor_is_structurally_redundant
 test_cli:CliOptionSurfaceTest,test_{read_only_commands_hide_ignored_options,push_only_surface_has_no_pull_query_command},
   CliBuildTest,test_build_embeds_map_in_claude_md,InitHooksTest,
   test_init_{installs_hooks_idempotently,replaces_hook_line_from_older_versions,preserves_custom_wrapped_hologram_command,chains_existing_hook},
   InitLangTest,test_lang_flag_baked_into_hooks,HookQuotingTest,
   test_{dollar_in_repo_path_is_escaped_in_hook_line,reinit_replaces_escaped_line_not_duplicates},ManagedHookLineTest,
   test_{review_only_line_recognized_and_near_misses_rejected,escaped_root_review_line_recognized,marker_recognizes_moved_root_but_not_foreign_commands},
   LegacyHookMigrationTest,
   test_{init_removes_managed_precommit_but_preserves_foreign_lines,uninstall_removes_legacy_review_only_hook,init_cleans_marked_legacy_hook_after_repository_moves},
   PostCommitHookE2ETest,test_findings_print_after_commit_and_never_block,BudgetTest,
   test_{ladder_is_deterministic_and_stamped,if_stale_rebuilds_when_only_the_budget_changed,untested_chains_drop_before_tested,levels_monotonic,explicit_skeleton_floor,floor_warning_only_below_skeleton,cold_type_fan_in,budget_stamp_recalled_and_cleared},
   TargetOptionTest,
   test_{restrict_stamps_recalls_and_prunes,target_all_restores_autodetect,unknown_and_ambiguous_targets_error,named_target_created_when_missing,recalled_target_must_be_supported_and_contained,target_symlink_cannot_escape_repository,target_symlink_inside_repository_is_rejected,hard_linked_target_is_rejected_without_clobbering_alias,negative_budget_is_rejected},
   LangFilterPersistenceTest,test_{filter_stamped_recalled_and_scoped,lang_all_clears_stored_filter},BootstrapTest,
   test_{missing_parser_langs_detects_gap,cli_exits_with_instructions_when_bootstrap_exhausted,review_uses_the_same_missing_parser_bootstrap,review_and_diff_bootstrap_language_present_only_in_revision},
   PrintCommandTest,test_print_writes_digest_to_stdout_and_touches_nothing,UninstallTest,
   test_uninstall_{removes_hooks_and_blocks_preserving_prose,keeps_foreign_hook_lines,preserves_managed_rule_dir_file},
   test_keep_blocks_limits_to_hooks,UninstallPythonTest,
   test_{generated_rule_file_is_preserved_without_provenance,managed_basename_with_user_prose_is_not_deleted},
   SizeWarningTest,test_{warns_over_threshold_but_embeds_exactly,zero_disables_warning},FeatureSelectionCliTest,
   test_{selection_stamped_recalled_and_cleared,none_is_a_selection_not_an_absent_one,comma_and_repeat_forms_agree,unknown_feature_names_the_known_ones,if_stale_rebuilds_when_only_the_selection_changed},
   FeaturePickerTest,
   test_{replies_toggle_by_number_and_name,an_unreadable_reply_changes_nothing,checklist_marks_the_current_selection,without_a_terminal_it_names_the_equivalent_flag},
   HookPythonSelectionTest,test_hook_uses_tool_venv_python_when_present
 test_extract_langs:PythonExtractTest,
   test_{classes_and_methods,function_signature_from_annotations,quoted_annotation_binds_like_the_bare_form,nested_and_whole_string_references_resolve,value_strings_and_unparseable_text_stay_verbatim},
   TypeScriptExtractTest,test_{interface_class_function,method_and_returns,exported_symbols_public},ArrowFunctionTest,
   test_{exported_arrow_is_public_fn,unexported_arrow_is_private,const_non_function_not_extracted,class_field_arrow_is_method,nested_closures_not_top_level},
   DecoratorExtractTest,
   test_{python_decorators_captured,java_annotations_captured,string_annotation_args_keep_interior_spacing,ts_decorators_captured_for_class_and_method}
 test_freshness_and_markers:DigestMetadataCompatibilityTest,
   test_{legacy_header_and_current_footer_parse_identically,semantic_text_cannot_spoof_footer_metadata,state_and_loc_changes_touch_only_final_metadata_line},
   StateAndCheckTest,
   test_{state_stamp_matches_state_hash,footer_states_the_corpus_cost_and_the_map_cost,unreadable_file_is_skipped_by_gather_and_state_alike,generator_change_invalidates_state,fingerprint_covers_every_package_source,check_fresh_then_stale,check_stale_when_no_block_embedded,build_if_stale_skips_when_fresh},
   TestedMarkerTest,test_{symbol_named_in_tests_gets_check,untested_symbol_unmarked},SizeMarkerTest,
   test_large_body_marked_small_not,TestIndexTest,
   test_{test_files_and_classless_test_functions_are_listed,index_states_no_call_targets},DisplayNameTest,
   test_display_name_strings_are_not_rendered,TestHelperTest,
   test_{directory_only_test_path_class_becomes_helper,declared_fixtures_and_shared_helper_functions_are_named,teardown_markers_earn_no_name,no_helpers_no_sigil_no_clause,shared_base_detected_via_references,digest_is_deterministic_with_helpers},
   EmbedTest,
   test_{embed_creates_block_and_preserves_existing,embed_note_stays_short_and_identifiable,managed_context_cost_accounts_for_every_component,managed_context_cost_normalizes_embedded_trailing_whitespace,embed_is_idempotent_and_refreshes,large_digest_is_embedded_exactly_without_degradation,block_carries_a_note_explaining_what_it_is,embedded_digest_roundtrips_the_exact_digest,prose_mentioning_end_marker_does_not_duplicate_block,cli_build_embeds,cli_build_preserves_user_content,check_and_if_stale_follow_the_embedded_block},
   ContextTargetsTest,test_defaults_to_claude_md_when_repo_has_none,
   {test_detects_existing,test_detects_new}_agent_files_and_rule_dirs,
   test_continue_rule_seeded_with_front_matter_clinerules_not,test_build_embeds_into_every_present_context_file,
   test_check_is_stale_when_one_target_lags,DiffCommandTest,test_diff_shows_added_symbol
 test_more_langs:GoExtractTest,
   test_{consts,struct_with_fields_and_interface,method_receiver_and_visibility,receiver_binding_resolves_calls},
   RustExtractTest,test_{attributes_and_consts,struct_enum_trait,trait_impl_becomes_super,impl_methods_and_visibility},
   CSharpExtractTest,test_{record_enum_interface_class,methods_ctor_visibility_calls,attributes_routes_and_consts},
   CExtractTest,test_{typedef_struct_and_enum,static_fn_private_prototype_public},CppExtractTest,
   test_{class_access_sections,out_of_line_definition_merges_calls,ctor},BashExtractTest,
   test_{script_root_node_and_both_definition_forms,variables_with_values_secret_redacted,underscore_prefix_private,call_chains,sizes},
   LuaExtractTest,test_{module_functions_and_methods,local_function_private},CssExtractTest,
   test_{selectors,pseudo_classes_not_selectors,custom_properties_and_keyframes,dedup},HtmlNestedBlocksTest,
   {test_script_functions,test_style_selectors}_extracted,test_ids_and_custom_elements_still_present,HtmlExtractTest,
   test_ids_and_custom_elements,HelmExtractTest,test_{chart_values_defines,plain_yaml_outside_chart_ignored},
   KotlinExtractTest,
   test_{annotations_and_consts,data_class_enum_interface,top_level_fn,local_bindings_resolve_receivers},TsGapsTest,
   test_{type_aliases,object_literal_api,reexports},ReactComponentTest,
   test_{jsx_usage_becomes_calls_intrinsics_dropped,memo_wrapped_default_export_recovered,fc_type_argument_replaces_untyped_props,component_render_tree_in_digest},
   AngularExtractTest,
   test_{component_selector_and_injectable,di_and_output_bindings,route_config_extracted,digest_shows_selector_and_routes,templateurl_external_html_joins,duplicate_selector_produces_no_edge},
   TsLossRecoveryTest,
   test_{interface_methods_extracted,untyped_field_bound_through_new_expression,decorated_and_readonly_ctor_params_parse},
   TsxExtractTest,test_jsx_component_arrow_extracted,SfcExtractTest,test_component_symbol_and_script_contents,
   RubyExtractTest,test_{attr_and_ivar_fields,module_class_and_methods,private_section_toggles_visibility},PhpExtractTest,
   test_{attributes_and_class_consts,interface_class_supers_fields,methods_visibility_typed_params_returns,ctor_bindings_calls_throws},
   SwiftExtractTest,test_{throw_types_become_raises,protocol_class_struct_kinds_and_supers,methods_visibility_init_ctor},
   ScalaExtractTest,test_{throw_new_becomes_raises,case_class_trait_object_kinds},MakefileExtractTest,
   test_{dollar_run_parity_controls_make_expansion,custom_recipe_prefix_and_reset_are_honored,named_makefile_detected_without_extension}
 test_release_privacy:ReleasePrivacyTest,
   test_{clean_tree_passes,tracked_local_task_is_rejected,private_tree_path_is_redacted_when_payload_also_matches,untracked_publishable_file_is_scanned,index_and_worktree_payloads_are_both_scanned,index_and_worktree_symlink_targets_are_scanned_and_redacted,publishable_filename_is_scanned_without_echoing_it,denylist_match_does_not_echo_protected_text,zip_and_tar_payloads_are_scanned,private_archive_member_is_redacted_when_payload_also_matches,archive_member_names_and_link_targets_are_scanned,zip_symlink_target_is_scanned_and_redacted,tar_symlink_and_hardlink_targets_are_scanned_and_redacted,empty_zip_directory_name_is_scanned,empty_tar_directory_private_name_is_scanned_and_redacted,zip_comments_and_extra_fields_are_scanned_with_redacted_labels,tar_owner_and_pax_metadata_are_scanned_with_redacted_labels,gzip_wrapper_filename_is_scanned_with_redacted_label,nested_archive_payload_is_rejected_with_redacted_location,compressed_archive_blob_is_rejected_in_tree_and_history,history_scan_finds_removed_content_without_echoing_it,history_scan_includes_removed_filenames_via_tree_objects,release_workflow_runs_no_privacy_audit}
 test_review:DupCheckTest,
   test_{name_similar_non_calling_addition_flagged,id_is_stable_when_only_rendered_pointer_changes,a_file_landmark_is_never_a_symbol_pointer,delegation_is_not_duplicate,short_and_stoplisted_names_skipped},
   RecoverCheckTest,{test_recovering_different_class,test_same_class_growth_not}_flagged,DeadOrphanApiTest,
   test_{dead_on_arrival_flagged,dead_id_survives_a_signature_only_attempt,orphaned_test_reference_flagged,orphan_suppressed_when_test_updated,dead_suppressed_when_a_test_file_mentions_the_name,api_summary,api_detects_public_type_shape_and_kind_changes,api_detects_mapped_routes_raises_ctors_and_constants,unmapped_decorator_does_not_create_api_drift,api_preserves_every_same_name_overload,makefile_under_tests_is_reviewed_as_production_api},
   PlaceCheckTest,test_{strong_affinity_advises_move,split_mass_stays_silent},ReportAndCliTest,
   test_{empty_report_is_empty_string,human_report_format_does_not_expose_structured_metadata,structured_report_is_sorted_and_json_serializable,finding_id_ignores_wording_and_normalizes_paths,wording_edits_preserve_baseline_versus_final_identity,cli_review_end_to_end,cli_review_json_is_machine_readable_even_when_clean,review_survives_git_hook_environment,history_only_change_never_alters_digest}
 test_simple_mode:CallExtractionTest,{test_java_method,test_python_function}_calls_recorded,
   test_call_extraction_has_no_display_cap,SimpleDigestTest,
   test_{signatures_present,calls_follow_signature_inline,no_docs_no_sections,packages_compressed_group_labels,calls_inline_no_calls_word},
   FixtureTokenCeilingTest,test_{javamini,pymini,tsmini,polyglot,webmini},{SameShapeGrouping,RenderUnit}Test,
   test_{tree_shares_prefixes_once,named_fields_replace_types},EnumValuesTest,
   test_{java_enum_constants_extracted,enum_values_rendered,python_enum_values_extracted},InterfaceMethodTest,
   test_{bodyless_interface_methods_extracted,interface_methods_rendered},QualifiedCallTest,
   test_receiver_kept_for_qualified_calls,FieldNamesTest,test_declared_field_names_shown,ReconstructablePathTest,
   test_{tree_labels_keep_real_path_segments,dotted_stem_keeps_its_extension},LanguageFilterTest,
   test_{only_requested_language_included,cli_lang_flag},RelationsTest,{test_implements,test_sealed_permits}_extracted,
   test_relations_rendered,{InterfaceImplementors,Legend}Test,test_{legend_line_present,no_query_or_regeneration_prose},
   ConstExtractTest,test_{python_module_constants,java_static_final_values},SecretRedactionTest,
   test_helper_is_the_single_shared_gate,RouteRenderTest,
   test_{spring_route_and_class_prefix,jaxrs_verb_and_path_pair,flask_verb_from_methods_kwarg,angular_component_selector,no_decorators_no_legend_clause,symfony_route_with_methods_array},
   ThrowsTest,{test_java_throws_clause,test_python_raise_types}_extracted,TransitiveReductionTest,
   test_{implied_edge_dropped,direct_only_edge_kept,cycle_members_both_kept},VoidOmissionTest,
   test_{python_none_return_omitted,java_void_omitted_in_signature},{GroupExtras,PrivateMembers}Test,
   test_private_names_packed_by_default,FeatureSelectionTest,test_structure_survives_an_empty_selection,
   CompactMapContractTest,test_cross_language_test_path_patterns,TightFormatTest,
   test_{ascii_return_sep_and_tight_commas,zero_usage_marker},ZeroUsageMarkerTest,test_rust_route_handler_not_marked_dead,
   {PrecomputedRender,TestIndexDiet}Test,
   test_{stem_mismatch_still_uses_exact_file,case_suite_names_factor_losslessly,mixed_extensions_keep_everything,determinism},
   CtorSuppressionTest,test_ctor_with_{extra_fact_kept,different_args_kept},test_grouped_same_shape_ctor_suppressed,
   DunderPrivateTest,
   test_{dunder_methods_out_helper_stays,class_with_only_init_loses_its_line,module_level_dunder_function_stays}
 test_stats_cli:BudgetStatsCliTest,
   test_{json_reports_exact_selection_without_writing_context,human_summary_names_fit_and_token_counts}
 test_treesitter:TreeSitterJavaTest,
   test_{types_methods_params_returns,record_components_and_supers,sealed_permits,interface_bodyless_methods,enum_constants,calls_with_receivers,ctor_extracted},
   MissingParserErrorTest,test_extract_file_errors_without_parser
tools
 check_release_privacy: main(argv):int;audit_tree(root,terms):list[str];audit_artifact(path,terms):list[str] +1
 measure_tokens: main(argv):int
 run_tests: main(argv):int
benchmark
 bench: main(argv):int;run_one(...);report(rows,anon):str +11
‥ optional facts omitted — NEVER guess; read source before relying on them
· 20,310 LOC · input 227,944 · output 5,967 tokens · state c7716b67fad6 · budget 6000 A7
```
<!-- hologram:end -->
