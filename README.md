# mdl-digest

Compresses any codebase into **one token-budgeted markdown file** you can attach to any LLM
session for instant whole-project context — purpose, structure, flows, invariants, existing
capabilities — without the model re-grepping the repo every session.

Deterministic (no LLM in generation), project- and language-agnostic, zero maintenance after
`init`. Python 3.11+ stdlib only.

## Install / use

Standalone tool — clone anywhere (e.g. `~/workspace/mdl-digest`) and point it at any repo:

```bash
python3 ~/workspace/mdl-digest/digest.py init --root /path/to/repo   # once per repo: git hooks + .gitignore + first build
python3 ~/workspace/mdl-digest/digest.py build --root /path/to/repo  # manual rebuild (hooks do this automatically)
python3 ~/workspace/mdl-digest/digest.py build --root . --lang java --out DIGEST.md
```

Optional (better Java fidelity): `python3 -m venv .venv && .venv/bin/pip install tree-sitter
tree-sitter-java` inside this project — hooks and the regen header auto-prefer that venv.

Output: `PROJECT_DIGEST.md` at the repo root (gitignored). Attach it to any LLM chat or agent
session. After `init`, post-commit/merge/checkout hooks keep it fresh; a per-blob cache under
`.git/mdl-digest/` keeps rebuilds fast (~1s warm on a 70k-LOC repo, ~3s cold at 500k LOC).

## Output (default: simple layout)

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
- After `>`: the functions it calls, first-call order, receiver-qualified
  (`Rational.of`, `registry.resolve`) — filtered to project-defined names (platform noise
  like `requireNonNull`/`toString` dropped) minus project-wide ubiquitous helpers.
- Class components show constructor dependencies (`Service(C: Registry, Clock)`); interface
  methods (bodyless) and enum constants are listed. Test code excluded.
- Relations: `: X` = extends/implements; `(I sealed: A | B)` = permitted subtypes.
- `!E` after a signature = declared throws (Java) / raised exceptions (Python).
- `×N` on a type = referenced from N other files (only shown when N ≥ 10).
- A one-line legend at the top of every digest explains the notation to any LLM.
- No token budgeting in this layout (yet) — the complete listing is the product.

`--full` renders the earlier rich sectioned layout (MODULES/API/ARCHETYPES/LINEAGE/…)
under a hard `--budget` cap.

## Architecture: engine vs packs

- `digest.py` — language-neutral engine: scan (git-tracked files only when in a repo) →
  extract → group → render. Python via stdlib `ast`; Java via tree-sitter when the optional
  `tree-sitter`+`tree-sitter-java` packages are importable (AST-grade calls/throws/relations),
  regex fallback otherwise — the tool never *requires* non-stdlib deps. A `.venv` next to
  `digest.py` is auto-preferred by the installed git hooks. TS via regex.
- `packs/*.toml` — data-only detector packs used by the `--full` layout.

## Tests

```bash
python3 -m unittest discover -s tests            # regex-fallback path
.venv/bin/python -m unittest discover -s tests   # tree-sitter path
```
