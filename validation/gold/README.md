# Static-validation gold protocol

This directory contains reviewed truth for the revisions in
`validation/corpora.toml`. The census and sample select files; facts and
exclusions define the closed reviewed surface within those files. A generated
Hologram map is never a source of truth.

## Review rules

1. A curator reads the pinned source, not Hologram output. Candidate worksheets
   may be produced from independent language AST/CST traversal, but they must
   not import Hologram parser, resolver, analysis, or render code.
2. Facts describe direct syntax and source-grounded relations only. A target is
   recorded only when declarations, imports, lexical scope, and receiver type
   provide one stable project symbol.
3. Every sampled callable records its complete direct-call list, including an
   empty list. Calls preserve lexical order and duplicates. Calls inside a
   nested named or anonymous callable belong to that nested scope, never its
   parent.
4. Dynamic or ambiguous calls are exclusions, never guessed targets. External
   calls are also recorded as source-call exclusions so the reviewed call scope
   remains auditable.
5. Every sampled declaration records the complete applicable non-call relation
   set. An empty set closes that declaration's reviewed relation scope.
6. Generated/vendor files are recorded as file-level exclusions if one remains
   in the frozen candidate policy.
7. A second reviewer checks every source anchor, complete/empty scope, ordered
   call list, strong-zero decision, and exclusion against the same clean pinned
   revision.
8. Gold is corrected only when pinned source proves the previous record wrong;
   score pressure is not evidence.

## Canonical identity and IDs

A subject is compact UTF-8 JSON for the six-part `SymbolId` array:

```text
[language,file,container_path,kind,name,signature_key]
```

The array uses no insignificant whitespace. Callable signature keys are `(`,
the comma-joined structural parameter types, and `)`. Other symbol kinds use an
empty signature key. The source line on every fact is the subject declaration
line. This includes `call` and `call_order`, because the canonical rendered map
retains caller provenance but not individual call-site lines. Path-derived
Python and TypeScript-family module declarations are grounded by their frozen
path and whole-file module span; their synthesized module name need not occur
on the anchor line.

A fact ID is:

```text
corpus:path:line:category:first16_sha256
```

The digest is over sorted compact JSON containing exactly `expected`, `subject`,
and `value`. An exclusion ID has category `exclusion`, uses line `0` for a
file-level exclusion, and hashes exactly `reason` and `scope`.

## Declaration and fact closure

Every supported declaration has one positive `declaration`, `kind`,
`container`, `visibility`, and `signature` fact. Signatures have the structural
shape:

```json
{"params":["Request"],"raises":[],"returns":"Result","text":"handle(Request):Result"}
```

Every `fn`, `method`, or `ctor` has one `call_order` fact. Its `targets` array
contains only unambiguous internal project targets. One `call` fact is emitted
for each occurrence; its ordinal is the zero-based position in that filtered
target array. Target arrays are full `SymbolId` arrays and may point to a
non-sampled file in the same pinned corpus.

Positive non-call relation kinds are `super`, `permit`, `component`,
`reexport`, and `dependency`. Language-specific extends and implements syntax
normalizes to `super`; construction remains an ordered call. Symbol-valued
relations require one internal target. External or ambiguous type targets use
an exact exclusion. Public natural duplicate groups are not closed-world and
therefore are not curated here.

For public corpora, negative facts are permitted only for `strong_x0`. Every
production declaration whose reviewed visibility is neither public nor
protected, other than a reexport, receives a true/false strong decision or a
narrow category exclusion. Review searches the entire pinned corpus, including
tests, generated sources, strings, configuration, reflection, decorators, and
runtime discovery. `expected=true` means the whole review finds no reachability
evidence; source-proven reachability uses `expected=false`; genuinely uncertain
runtime reachability is excluded with its concrete evidence.

## Exclusion scopes

The literal scope `file` excludes one complete census file and requires
`line=null`. Other scopes are canonical compact JSON arrays:

```text
["fact",category,SymbolId,value]
["category",category,SymbolId]
["source_call",caller-SymbolId,source-ordinal,raw-callee]
["candidate",syntax-kind,identifier]
```

`fact` and `category` are scoring scopes and may suppress only the identified
fact or subject/category. `source_call` and `candidate` document reviewed source
that has no comparable fact; they never suppress observed positives. The source
ordinal counts every direct syntactic call in the caller before target
filtering, so repeated calls on one line remain distinct. Structured exclusions
require a positive source line.

Stable reason tokens include `external_call_target`,
`unresolved_dynamic_target`, `ambiguous_call_target`,
`external_relation_target`, `ambiguous_relation_target`,
`ambiguous_declaration_identity`, `shadowed_callable_declaration`,
`discarded_callable_owner`, `reexport_only_no_supported_declaration`, and
`ordinary_yaml_not_helm`. A runtime-reachability reason appends the specific
evidence after `runtime_reachability_ambiguous:`.

## Python public-sample rules

Python candidates are the path-derived module; named classes and functions;
class annotated fields; enum members; and direct module/class all-uppercase
assignments. Named nested definitions inherit their lexical container. Lambdas,
ordinary local variables, destructuring, and dynamic attributes are outside the
stable declaration grammar. `self` and `cls` are omitted from callable
parameters. Missing parameter annotations use `?`; comma whitespace in type
expressions is removed. A leading underscore gives private visibility. Enum
bases normalize the declaration to `enum`; other internal source-resolved bases
become `super` relations. Class fields and enum members become `component`
relations.

## Lua public-sample rules

Lua candidates are named function declarations, functions assigned to a static
identifier/member path, stable function-valued fields in a directly returned
table, chunk-level static table assignments, and chunk-level literal
constants. A simple callable path is a `fn`; a member path or returned-table
field is a `method`. Lexical named owners extend the container without a source
line or ordinal. Parameters use `?`, with `...` retained for varargs. Local and
lexically nested declarations are private; exported member paths are public.
When the same callable identity is assigned repeatedly, the last source
definition is the effective declaration and earlier definitions receive exact
shadow exclusions. Truly anonymous callbacks, callback-local tables, and
callables whose stable owner was discarded receive narrow candidate
exclusions; their calls never roll into a parent callable.

## TypeScript-family public-sample rules

Every TypeScript or TSX file has a path-derived public module. Candidates also
include named namespaces/modules; classes, interfaces, enums, and type aliases;
enum members; named functions; constructors, methods, accessors, and direct
class/interface properties; top-level `const`/`let`/`var`; callable variables;
and object-literal APIs with named callable members. Object members nested
inside a type-alias shape are part of that alias signature, not separate
declarations. Only source-bearing `export ... from` syntax creates a `reexport`
subject; local/default export syntax changes the original declaration's
visibility. Exported declarations are public and other module declarations are
private. Anonymous callback scopes are unsupported owners: declarations and
calls inside them are excluded narrowly and are not attributed to the parent.
Extends and implements clauses normalize to `super`; direct type members become
`component` relations.

## Java public-sample rules

Java candidates are each source package module; named classes, interfaces,
enums, records, annotation types, and named nested/local types; explicit
methods, constructors, and compact record constructors; declared fields,
`static final` constants, enum constants, and record header components. Record
components are private `field` declarations and `component` relations. No
implicit default/record constructor or accessor is synthesized. Java source
visibility rules determine `pub`, `protected`, `private`, or `internal`, with
interface members public and enum constructors private by default. Structural
parameter, return, and `throws` types are whitespace-normalized. Extends and
implements normalize to `super`, and `permits` to `permit`. Lambdas and
anonymous-class members have no stable declaration identity and receive narrow
candidate exclusions; calls in those scopes never roll into an enclosing
callable.

## Review execution

Set all five `HOLOGRAM_VALIDATION_*` path variables to clean exact checkouts and
run:

```bash
.venv/bin/python -m unittest \
  tests.test_validation_corpus.ValidationGoldCoverageTest -v
```

The source-anchor test skips only when none of the checkout variables is set.
If any is set, all five exact, clean pinned roots are mandatory. After a second
review, freeze the per-file row counts and SHA-256 digests in the coverage test.
Do not inspect or use benchmark archive state while curating static gold.
