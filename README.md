# hologram

Compresses any codebase into **one markdown file** you can attach to any LLM session for
instant whole-project context — structure, signatures, call flows — without the model
re-grepping the repo every session.

Deterministic (no LLM in generation), project-agnostic, zero maintenance after `init`.
Extraction is AST-based everywhere: **tree-sitter** for Java and TypeScript/JavaScript,
stdlib `ast` for Python.

## Install / use

Standalone, self-sufficient tool — clone anywhere (e.g. `~/workspace/hologram`) and point
it at any repo:

```bash
python3 ~/workspace/hologram/hologram.py init --root /path/to/repo   # once per repo: git hooks + .gitignore + first build
python3 ~/workspace/hologram/hologram.py build --root /path/to/repo  # manual rebuild (hooks do this automatically)
python3 ~/workspace/hologram/hologram.py build --root . --lang java --out DIGEST.md
```

The first time it meets a Java or TS/JS repo it offers to install its own dependencies
(creates a `.venv` next to `hologram.py`, pip-installs the tree-sitter grammars, and
re-launches itself — one `y` and it just runs). Once that venv exists, every later run
re-execs into it automatically, so plain `python3 hologram.py …` always works. In
non-interactive contexts it prints the exact install command instead. Python-only repos
need no dependencies at all (stdlib `ast`). Installed git hooks auto-prefer the venv.

Output: `PROJECT_DIGEST.md` at the repo root (gitignored). Attach it to any LLM chat or
agent session. After `init`, post-commit/merge/checkout hooks keep it fresh.

## Output

Signatures only — no docs, no prose. The LLM reads semantics from names:

```
src/main/java/com/private-corpus
  kernel
    arithmetic
      MathOps(C)
        add(Rational,Rational):Rational > Rational.of,multiply
      Rational(R: BigInteger,BigInteger)
        of(BigInteger,BigInteger):Rational > divide,negate,Rational
    domainkind
      ForceKind(E: ASSERTED,ENTAILED,SUPPORTED,KIND_D,HYPOTHETICAL)
```

Format is deliberately tight — `name(params):Ret`, no space after commas — measured with a real
tokenizer (o200k) to be ~5% cheaper than the pretty variant with zero information loss.

- Package paths render as a path-compressed tree of real path segments (walk the tree to
  reconstruct any file's directory): shared prefixes stated once.
- Types with identical shape collapse into one entry
  (`AggregateId, ArtifactId, …(R: UUID)` with shared methods shown once as `⟨X⟩`).
- After `>`: the functions it calls, first-call order. Receivers are **type-resolved**
  from declared params/fields/locals: a call through a project-typed variable renders as
  `Type.method`; calls through platform-typed variables are dropped entirely (no
  `bigint.signum` noise even when a project method shares the name). Project-wide
  ubiquitous helpers (logging/guards) are dropped too.
- Call lists are **transitively reduced**: an entry already reachable through a sibling
  entry is omitted (SCC-safe, so cycles never disappear). Reachability is preserved
  exactly; follow the chain.
- Types with the same shape group even when their method sets diverge: shared methods
  print once as `⟨X⟩`, each member's extra methods print on its own `Name: …` line.
- Class components show constructor dependencies (`Service(C: Registry, Clock)`); interface
  methods (bodyless) and enum constants are listed. Test code excluded.
- Relations: `: X` = extends/implements; `(I sealed: A | B)` = permitted subtypes.
- `!E` after a signature = declared throws / raised exceptions, `Exception` suffix implied
  (`!UnknownItem` = `UnknownItemException`). No `:Ret` = returns void/None.
- `×N` on a type = referenced from N other files (only shown when N ≥ 10).
- A one-line legend at the top of every digest explains the notation to any LLM.

## Architecture

Single file, one pipeline: scan (git-tracked files only when in a repo) → extract → render.
Language-specific code is confined to three extractors that all produce the same `Symbol`
records; everything downstream is language-neutral.

## Honest trade-offs

**Where it earns its keep**

- Real compression on real code: a 77k-LOC / 727-file Java codebase digests to ~17k
  tokens — whole-project context that fits comfortably alongside an actual task.
- Deterministic and diffable: no LLM in the loop, same input → same output, so the
  digest can live in code review and its changes mean something.
- AST-grade everywhere: tree-sitter (Java, TS/JS) and stdlib `ast` (Python). No regex
  parsing of source, so exotic formatting doesn't break extraction.
- Call chains are semantically filtered: receivers resolve through declared types, so
  a platform call that happens to share a project method's name doesn't pollute the graph.
- Fast enough to forget about: ~0.8s for 77k LOC on a laptop, run from git hooks.
- Zero infrastructure: one file, self-installs its grammars, no config, no server.

**Where it falls short**

- Three languages. Java, Python, TypeScript/JavaScript — no Go, Rust, Kotlin, C#, C++.
  The grammar table makes adding one cheap, but each needs its own extractor mapping.
- TS extraction misses arrow-function exports (`export const f = () => …`) and
  standalone `type`/`const` declarations — class/interface/enum/`function` only. On
  idiomatic modern TS that is a real coverage gap.
- Public surface only. Private helpers, function bodies, and implementation complexity
  are invisible — a class with 500 lines of private logic looks identical to a stub.
  The digest tells an LLM *what exists*, not *how it works*.
- Name quality is load-bearing. No docs or comments survive; if the code calls things
  `Manager2` and `doStuff`, the digest faithfully compresses that emptiness.
- Call chains are capped (12 per function), transitively reduced (direct-vs-indirect
  is deliberately blurred), and fall back to name matching when a receiver's type can't
  be resolved — so the graph is honest but not complete.
- No token budget. Output grows with the codebase; a millions-LOC monorepo will produce
  a digest too large for small context windows, and nothing trims it.
- Tests are excluded, so behavioral specs living in test names don't appear.
- Freshness is commit-grained: hooks fire on commit/merge/checkout, so uncommitted
  changes aren't reflected until the next trigger (or a manual build).
- The core claim — that LLMs navigate a repo better with this digest attached — is
  plausible and motivated the format, but hasn't been benchmarked rigorously.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

(Runs under plain `python3` too; Java/TS tests skip when the tree-sitter grammars are absent.)

## License

MIT — see [LICENSE](LICENSE).
