# Agent Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure whether coding agents given a hologram digest duplicate less code, read fewer files, and spend fewer tokens than agents without one — turning the README's "unbenchmarked" caveat into numbers.

**Architecture:** One new self-contained script `benchmark/bench.py` (mirrors the project's single-file style) with subcommands `run` and `report`. `run` executes a task matrix — each cell is a headless `claude -p` session inside a disposable git worktree of a corpus repo, in condition A (digest + agent instructions present) or B (control, neither) — and appends one JSON line of metrics per run to `benchmark/results/runs.jsonl`. Metrics come from three deterministic sources: the stream-json transcript (tool-call counts, tokens, turns), a per-task acceptance shell command, and a digest-diff duplication detector that reuses hologram itself. `report` aggregates the JSONL into a markdown comparison table. The `claude` invocation is injected as a callable so every orchestration path is testable without spending tokens.

**Tech Stack:** Python 3.11 stdlib only (json, subprocess, tempfile, difflib, statistics, argparse), `claude` CLI ≥ 2.x in `-p --output-format stream-json` mode, hologram.py from this repo, git worktrees.

**Cost note for the human running it:** the full matrix (10 tasks × 2 conditions × 3 reps = 60 headless sessions, `--max-turns 40`, sonnet) is realistically 40–150M tokens of API usage depending on the corpus. Task 9's smoke run (2 tasks × 2 × 1 = 4 sessions) exists so you can sanity-check the harness for ~2–5% of that before committing to the matrix.

---

## File structure

```
benchmark/
  bench.py              # the whole harness: task loading, workspace setup, run, metrics, report
  tasks/
    private-corpus.json           # task definitions for the private-corpus corpus (drafts in Task 8; verify before running)
  results/              # gitignored; runs.jsonl + per-run transcripts land here
  README.md             # runbook: smoke run, full matrix, reading the report
tests/
  test_bench.py         # all harness tests (fixtures inline; no claude calls, no network)
docs/superpowers/plans/
  2026-08-08-agent-benchmark.md   # this plan
```

`bench.py` is deliberately one file with pure functions at the top and orchestration at the bottom, same shape as `hologram.py`. Everything that touches the network or the `claude` binary goes through the injected `runner` callable; everything else is unit-tested.

---

### Task 1: Scaffold `bench.py` — task-file loader

**Files:**
- Create: `benchmark/bench.py`
- Create: `benchmark/tasks/.gitkeep` (placeholder until Task 8)
- Create: `tests/test_bench.py`
- Modify: `.gitignore` (add `benchmark/results/`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bench.py
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmark"))

import bench  # noqa: E402


class TaskLoaderTest(unittest.TestCase):
    def _taskfile(self, tmp: Path) -> Path:
        p = tmp / "tasks.json"
        p.write_text(json.dumps({
            "corpus": "~/workspace/private-corpus",
            "model": "sonnet",
            "max_turns": 40,
            "tasks": [
                {"id": "weighted-avg", "kind": "reuse",
                 "prompt": "Add a weighted average.",
                 "accept_cmd": "grep -rq weightedAverage {ws}/src",
                 "expect_reuse": ["normalize", "add"]},
                {"id": "find-lifecycle", "kind": "navigate",
                 "prompt": "Where is claim lifecycle handled?",
                 "accept_cmd": "true",
                 "expect_reuse": []},
            ],
        }))
        return p

    def test_loads_tasks_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = bench.load_tasks(self._taskfile(Path(tmp)))
        self.assertEqual(cfg.model, "sonnet")
        self.assertEqual(cfg.max_turns, 40)
        self.assertEqual(len(cfg.tasks), 2)
        self.assertEqual(cfg.tasks[0].id, "weighted-avg")
        self.assertEqual(cfg.tasks[0].expect_reuse, ["normalize", "add"])
        self.assertTrue(str(cfg.corpus).startswith("/"))  # ~ expanded

    def test_missing_required_field_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text(json.dumps({"corpus": ".", "tasks": [{"id": "x"}]}))
            with self.assertRaises(SystemExit):
                bench.load_tasks(p)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_bench -v`
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'bench'`

- [ ] **Step 3: Write the loader**

```python
# benchmark/bench.py
#!/usr/bin/env python3
"""bench: measure agents with vs without a hologram digest.

Subcommands: run (execute the task matrix headlessly), report (aggregate results).
Every claude invocation goes through an injectable runner so the harness is
testable without spending tokens.
"""

from __future__ import annotations

import argparse
import difflib
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HOLOGRAM = Path(__file__).resolve().parents[1] / "hologram.py"


@dataclass
class Task:
    id: str
    kind: str                 # "reuse" | "navigate"
    prompt: str
    accept_cmd: str           # shell; {ws} is replaced with the workspace path
    expect_reuse: list[str] = field(default_factory=list)


@dataclass
class Config:
    corpus: Path
    tasks: list[Task]
    model: str = "sonnet"
    max_turns: int = 40


def load_tasks(path: Path) -> Config:
    data = json.loads(path.read_text())
    try:
        tasks = [Task(id=t["id"], kind=t["kind"], prompt=t["prompt"],
                      accept_cmd=t["accept_cmd"],
                      expect_reuse=t.get("expect_reuse", []))
                 for t in data["tasks"]]
        return Config(corpus=Path(data["corpus"]).expanduser().resolve(),
                      tasks=tasks,
                      model=data.get("model", "sonnet"),
                      max_turns=int(data.get("max_turns", 40)))
    except KeyError as e:
        raise SystemExit(f"task file {path}: missing field {e}")


if __name__ == "__main__":
    raise SystemExit(0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest tests.test_bench -v`
Expected: 2 tests PASS

- [ ] **Step 5: Add `benchmark/results/` to `.gitignore`**

Append to `.gitignore`:

```
benchmark/results/
```

- [ ] **Step 6: Commit**

```bash
git add benchmark/bench.py benchmark/tasks/.gitkeep tests/test_bench.py .gitignore
git commit -m "feat(bench): task-file loader"
```

---

### Task 2: Transcript metrics parser

Parses the stream-json output of `claude -p --output-format stream-json --verbose`. We count tool_use blocks by name from `"type": "assistant"` events and take token totals + turn count from the final `"type": "result"` event.

**Files:**
- Modify: `benchmark/bench.py` (append after `load_tasks`)
- Modify: `tests/test_bench.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_bench.py

TRANSCRIPT = "\n".join([
    json.dumps({"type": "system", "subtype": "init"}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.java"}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "thinking..."},
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "normalize"}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {}}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write", "input": {}}]}}),
    json.dumps({"type": "result", "num_turns": 7,
                "usage": {"input_tokens": 91000, "output_tokens": 4200}}),
    "not-json-noise",
])


class TranscriptMetricsTest(unittest.TestCase):
    def test_counts_and_usage(self):
        m = bench.parse_transcript(TRANSCRIPT)
        self.assertEqual(m["reads"], 1)
        self.assertEqual(m["searches"], 1)
        self.assertEqual(m["edits"], 2)          # Edit + Write
        self.assertEqual(m["turns"], 7)
        self.assertEqual(m["tokens_in"], 91000)
        self.assertEqual(m["tokens_out"], 4200)

    def test_empty_transcript_gives_zeroes(self):
        m = bench.parse_transcript("")
        self.assertEqual(m, {"reads": 0, "searches": 0, "edits": 0,
                             "turns": 0, "tokens_in": 0, "tokens_out": 0})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_bench.TranscriptMetricsTest -v`
Expected: ERROR — `bench` has no attribute `parse_transcript`

- [ ] **Step 3: Implement the parser**

```python
# append to benchmark/bench.py

_READ_TOOLS = {"Read"}
_SEARCH_TOOLS = {"Grep", "Glob"}
_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}


def parse_transcript(text: str) -> dict:
    """Tool-call counts and usage from a claude stream-json transcript.
    Tolerant of non-JSON noise lines."""
    m = {"reads": 0, "searches": 0, "edits": 0,
         "turns": 0, "tokens_in": 0, "tokens_out": 0}
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content", []):
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                if name in _READ_TOOLS:
                    m["reads"] += 1
                elif name in _SEARCH_TOOLS:
                    m["searches"] += 1
                elif name in _EDIT_TOOLS:
                    m["edits"] += 1
        elif ev.get("type") == "result":
            usage = ev.get("usage") or {}
            m["turns"] = int(ev.get("num_turns", 0))
            m["tokens_in"] = int(usage.get("input_tokens", 0))
            m["tokens_out"] = int(usage.get("output_tokens", 0))
    return m
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest tests.test_bench.TranscriptMetricsTest -v`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add benchmark/bench.py tests/test_bench.py
git commit -m "feat(bench): stream-json transcript metrics parser"
```

---

### Task 3: Digest-diff duplication detector

Deterministic core of the benchmark. After a run, we build the workspace digest and compare it to the pre-run digest. New signature lines are candidate implementations. For each `expect_reuse` symbol we decide: did the agent **reuse** it (a new line's call chain — the part after `>` — names it), or **duplicate** it (a new function whose name is similar to the expected symbol but whose chain does not call it)?

**Files:**
- Modify: `benchmark/bench.py` (append)
- Modify: `tests/test_bench.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_bench.py

BEFORE_DIGEST = """# corpus @x 2026-08-08 · 100 LOC · state aaa · regen: x
· legend: …
src
 math
  MathOps(C)
   add(Rational,Rational):Rational
   normalize(List<Rational>):List<Rational> > add
"""

AFTER_REUSED = BEFORE_DIGEST + """\
  Averages(C)
   weightedAverage(List<Rational>):Rational > MathOps.add,normalize
"""

AFTER_DUPLICATED = BEFORE_DIGEST + """\
  Averages(C)
   normalizeWeights(List<Rational>):List<Rational>
   weightedAverage(List<Rational>):Rational > normalizeWeights
"""


class DuplicationDetectorTest(unittest.TestCase):
    def test_reuse_detected(self):
        v = bench.judge_reuse(BEFORE_DIGEST, AFTER_REUSED, ["normalize", "add"])
        self.assertEqual(sorted(v["reused"]), ["add", "normalize"])
        self.assertEqual(v["duplicated"], [])

    def test_duplicate_detected(self):
        v = bench.judge_reuse(BEFORE_DIGEST, AFTER_DUPLICATED, ["normalize"])
        self.assertEqual(v["reused"], [])
        self.assertEqual(v["duplicated"], ["normalizeWeights"])

    def test_new_lines_listed(self):
        v = bench.judge_reuse(BEFORE_DIGEST, AFTER_REUSED, [])
        self.assertTrue(any("weightedAverage" in ln for ln in v["new_lines"]))

    def test_no_change_is_clean(self):
        v = bench.judge_reuse(BEFORE_DIGEST, BEFORE_DIGEST, ["normalize"])
        self.assertEqual(v, {"new_lines": [], "reused": [], "duplicated": []})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_bench.DuplicationDetectorTest -v`
Expected: ERROR — no attribute `judge_reuse`

- [ ] **Step 3: Implement the detector**

```python
# append to benchmark/bench.py

_SIG_LINE = None  # signature lines are indented and contain '(' before any ':'


def _sig_lines(digest: str) -> list[str]:
    out = []
    for ln in digest.splitlines():
        s = ln.strip()
        if s and not s.startswith(("#", "·", "-", "»", "?")) and "(" in s:
            out.append(s)
    return out


def _fn_name(sig_line: str) -> str:
    return sig_line.split("(", 1)[0].strip().lstrip("-").split(",")[-1]


def _chain(sig_line: str) -> list[str]:
    if " > " not in sig_line:
        return []
    return [c.strip() for c in sig_line.split(" > ", 1)[1].split(",")]


def judge_reuse(before: str, after: str, expect_reuse: list[str]) -> dict:
    """Compare digests around a run. reused = expected symbols named in a new
    line's call chain. duplicated = new functions name-similar to an expected
    symbol that do NOT call it."""
    old = set(_sig_lines(before))
    new_lines = [ln for ln in _sig_lines(after) if ln not in old]
    reused: list[str] = []
    duplicated: list[str] = []
    for target in expect_reuse:
        tshort = target.rsplit(".", 1)[-1].lower()
        hit = any(tshort in (c.rsplit(".", 1)[-1].lower() for c in _chain(ln))
                  for ln in new_lines)
        if hit:
            reused.append(target)
            continue
        for ln in new_lines:
            name = _fn_name(ln)
            sim = difflib.SequenceMatcher(None, name.lower(), tshort).ratio()
            if sim >= 0.6 and name.lower() != tshort:
                duplicated.append(name)
                break
    return {"new_lines": new_lines,
            "reused": sorted(set(reused)),
            "duplicated": sorted(set(duplicated))}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest tests.test_bench.DuplicationDetectorTest -v`
Expected: 4 tests PASS. If `test_duplicate_detected` fails on the similarity
threshold, print `difflib.SequenceMatcher(None, "normalizeweights", "normalize").ratio()`
(it is ≈0.72) and adjust the assertion comment, not the threshold, unless the ratio
is genuinely below 0.6.

- [ ] **Step 5: Remove the dead `_SIG_LINE = None` marker line, rerun the four tests, commit**

```bash
git add benchmark/bench.py tests/test_bench.py
git commit -m "feat(bench): digest-diff reuse/duplication detector"
```

---

### Task 4: Workspace builder (worktree + condition setup)

Condition A gets a freshly built `PROJECT_DIGEST.md` plus a `CLAUDE.md` containing the README's agent snippet. Condition B gets a `CLAUDE.md` with the same header but no digest section, and no digest file — parity in everything except the treatment.

**Files:**
- Modify: `benchmark/bench.py` (append)
- Modify: `tests/test_bench.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_bench.py
import subprocess


def _mini_corpus(tmp: Path) -> Path:
    repo = tmp / "corpus"
    repo.mkdir()
    (repo / "svc.py").write_text(
        "def normalize(xs: list) -> list:\n    return xs\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b",
                    "commit", "-qm", "seed"], cwd=repo, check=True)
    return repo


class WorkspaceTest(unittest.TestCase):
    def test_condition_a_has_digest_and_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsA", "A")
            try:
                self.assertTrue((ws / "PROJECT_DIGEST.md").exists())
                self.assertIn("PROJECT_DIGEST.md", (ws / "CLAUDE.md").read_text())
                self.assertTrue((ws / "svc.py").exists())
            finally:
                bench.drop_workspace(repo, ws)

    def test_condition_b_is_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsB", "B")
            try:
                self.assertFalse((ws / "PROJECT_DIGEST.md").exists())
                self.assertNotIn("PROJECT_DIGEST", (ws / "CLAUDE.md").read_text())
            finally:
                bench.drop_workspace(repo, ws)

    def test_workspace_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            ws = bench.make_workspace(repo, Path(tmp) / "wsA", "A")
            try:
                (ws / "svc.py").write_text("changed")
                self.assertIn("normalize", (repo / "svc.py").read_text())
            finally:
                bench.drop_workspace(repo, ws)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_bench.WorkspaceTest -v`
Expected: ERROR — no attribute `make_workspace`

- [ ] **Step 3: Implement workspace setup**

```python
# append to benchmark/bench.py

_AGENT_SNIPPET = """## Project map: PROJECT_DIGEST.md

`PROJECT_DIGEST.md` at the repo root is a generated inventory of this codebase:
every public signature, type relation, and call chain, plus private member names.
Line 2 is the legend.

Consult it BEFORE:
- writing any new function, class, or helper — search for an existing one first
  and reuse it (`×N` marks widely-used utilities, `✓` marks test-exercised ones)
- placing new code — the package tree, the `· deps a→b` lines, and grouped
  families are the house structure; extend it rather than inventing a parallel one
- exploratory grepping — look here first, then grep for the specific thing this
  file says exists
"""

_BASE_CLAUDE_MD = """# Working notes

Complete the requested task directly. Keep changes minimal and idiomatic.
"""


def make_workspace(corpus: Path, ws: Path, condition: str) -> Path:
    """Detached git worktree of the corpus, prepared for one condition.
    A = digest + agent instructions; B = control."""
    subprocess.run(["git", "-C", str(corpus), "worktree", "add", "--detach",
                    "-f", str(ws), "HEAD"], check=True, capture_output=True)
    claude_md = _BASE_CLAUDE_MD
    if condition == "A":
        subprocess.run([sys.executable, str(HOLOGRAM), "build",
                        "--root", str(ws), "--quiet"], check=True)
        claude_md += "\n" + _AGENT_SNIPPET
    (ws / "CLAUDE.md").write_text(claude_md)
    return ws


def drop_workspace(corpus: Path, ws: Path) -> None:
    subprocess.run(["git", "-C", str(corpus), "worktree", "remove", "--force",
                    str(ws)], capture_output=True)
    shutil.rmtree(ws, ignore_errors=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest tests.test_bench.WorkspaceTest -v`
Expected: 3 tests PASS. Note: `sys.executable` in tests is the venv python, which
has the grammars; the mini corpus is Python-only so even plain python3 works.

- [ ] **Step 5: Commit**

```bash
git add benchmark/bench.py tests/test_bench.py
git commit -m "feat(bench): condition-aware workspace builder"
```

---

### Task 5: Run orchestration with injectable runner

`run_one` wires everything: workspace → pre-digest → runner (the only claude touchpoint) → post-digest → acceptance → metrics dict. The default runner shells out to `claude`; tests inject a fake.

**Files:**
- Modify: `benchmark/bench.py` (append)
- Modify: `tests/test_bench.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_bench.py

class RunOneTest(unittest.TestCase):
    def test_full_cycle_with_fake_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(
                id="avg", kind="reuse",
                prompt="Add average() that reuses normalize.",
                accept_cmd="grep -q average {ws}/svc.py",
                expect_reuse=["normalize"])

            def fake_runner(prompt: str, ws: Path, model: str, max_turns: int) -> str:
                # the "agent" appends a function that calls normalize
                (ws / "svc.py").write_text(
                    (ws / "svc.py").read_text()
                    + "\ndef average(xs: list) -> float:\n"
                      "    return sum(normalize(xs)) / len(xs)\n")
                return TRANSCRIPT

            row = bench.run_one(repo, task, "A", rep=0,
                                results_dir=Path(tmp) / "results",
                                model="sonnet", max_turns=40,
                                runner=fake_runner)
        self.assertEqual(row["task"], "avg")
        self.assertEqual(row["condition"], "A")
        self.assertTrue(row["accepted"])
        self.assertEqual(row["reused"], ["normalize"])
        self.assertEqual(row["duplicated"], [])
        self.assertEqual(row["reads"], 1)
        self.assertEqual(row["tokens_in"], 91000)

    def test_transcript_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            task = bench.Task(id="noop", kind="navigate", prompt="look around",
                              accept_cmd="true", expect_reuse=[])
            results = Path(tmp) / "results"
            bench.run_one(repo, task, "B", rep=1, results_dir=results,
                          model="sonnet", max_turns=40,
                          runner=lambda *a, **k: TRANSCRIPT)
            self.assertTrue((results / "noop-B-1.jsonl").exists())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_bench.RunOneTest -v`
Expected: ERROR — no attribute `run_one`

- [ ] **Step 3: Implement orchestration and the real runner**

```python
# append to benchmark/bench.py

def claude_runner(prompt: str, ws: Path, model: str, max_turns: int) -> str:
    """The only function that spends tokens. Runs claude headless in the
    workspace; returns the raw stream-json transcript."""
    r = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
         "--max-turns", str(max_turns), "--model", model,
         "--dangerously-skip-permissions"],
        cwd=ws, capture_output=True, text=True, timeout=1800)
    return r.stdout


def _digest_of(ws: Path) -> str:
    out = ws / ".bench-digest.md"
    subprocess.run([sys.executable, str(HOLOGRAM), "build", "--root", str(ws),
                    "--out", str(out), "--quiet"], check=True)
    text = out.read_text()
    out.unlink()
    return text


def run_one(corpus: Path, task: Task, condition: str, rep: int,
            results_dir: Path, model: str, max_turns: int,
            runner=claude_runner) -> dict:
    results_dir.mkdir(parents=True, exist_ok=True)
    ws = results_dir / f"ws-{task.id}-{condition}-{rep}"
    make_workspace(corpus, ws, condition)
    try:
        before = _digest_of(ws)
        transcript = runner(task.prompt, ws, model, max_turns)
        (results_dir / f"{task.id}-{condition}-{rep}.jsonl").write_text(transcript)
        after = _digest_of(ws)
        verdict = judge_reuse(before, after, task.expect_reuse)
        accepted = subprocess.run(
            task.accept_cmd.format(ws=ws), shell=True,
            capture_output=True).returncode == 0
        metrics = parse_transcript(transcript)
        return {"task": task.id, "kind": task.kind, "condition": condition,
                "rep": rep, "accepted": accepted,
                "reused": verdict["reused"], "duplicated": verdict["duplicated"],
                "new_lines": len(verdict["new_lines"]), **metrics}
    finally:
        drop_workspace(corpus, ws)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest tests.test_bench.RunOneTest -v`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add benchmark/bench.py tests/test_bench.py
git commit -m "feat(bench): run orchestration with injectable runner"
```

---

### Task 6: Report aggregator

**Files:**
- Modify: `benchmark/bench.py` (append)
- Modify: `tests/test_bench.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_bench.py

class ReportTest(unittest.TestCase):
    def test_aggregates_by_condition(self):
        rows = [
            {"task": "t1", "kind": "reuse", "condition": "A", "rep": 0,
             "accepted": True, "reused": ["normalize"], "duplicated": [],
             "new_lines": 1, "reads": 3, "searches": 2, "edits": 2,
             "turns": 9, "tokens_in": 100, "tokens_out": 10},
            {"task": "t1", "kind": "reuse", "condition": "B", "rep": 0,
             "accepted": True, "reused": [], "duplicated": ["normalize2"],
             "new_lines": 2, "reads": 9, "searches": 8, "edits": 2,
             "turns": 15, "tokens_in": 300, "tokens_out": 30},
        ]
        md = bench.report(rows)
        self.assertIn("| A |", md)
        self.assertIn("| B |", md)
        # duplication rate: A 0%, B 100% of reuse-kind runs
        self.assertIn("0%", md)
        self.assertIn("100%", md)
        self.assertIn("reads", md)

    def test_empty_rows(self):
        self.assertIn("no runs", bench.report([]))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_bench.ReportTest -v`
Expected: ERROR — no attribute `report`

- [ ] **Step 3: Implement**

```python
# append to benchmark/bench.py

def report(rows: list[dict]) -> str:
    if not rows:
        return "no runs recorded\n"
    lines = ["| condition | runs | accepted | duplication (reuse tasks) | "
             "reads | searches | turns | tokens in | tokens out |",
             "|---|---|---|---|---|---|---|---|---|"]
    for cond in sorted({r["condition"] for r in rows}):
        rs = [r for r in rows if r["condition"] == cond]
        reuse = [r for r in rs if r["kind"] == "reuse"]
        dup_rate = (100 * sum(1 for r in reuse if r["duplicated"]) / len(reuse)
                    if reuse else 0)
        acc = 100 * sum(1 for r in rs if r["accepted"]) / len(rs)

        def mean(key):  # noqa: ANN001
            return statistics.fmean(r[key] for r in rs)

        lines.append(
            f"| {cond} | {len(rs)} | {acc:.0f}% | {dup_rate:.0f}% | "
            f"{mean('reads'):.1f} | {mean('searches'):.1f} | "
            f"{mean('turns'):.1f} | {mean('tokens_in'):,.0f} | "
            f"{mean('tokens_out'):,.0f} |")
    lines.append("")
    lines.append("Per-task duplication (reuse tasks):")
    for r in sorted(rows, key=lambda r: (r["task"], r["condition"], r["rep"])):
        if r["kind"] == "reuse":
            mark = "DUP:" + ",".join(r["duplicated"]) if r["duplicated"] \
                else ("reused:" + ",".join(r["reused"]) if r["reused"] else "—")
            lines.append(f"- {r['task']} [{r['condition']}#{r['rep']}] {mark}")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest tests.test_bench.ReportTest -v`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add benchmark/bench.py tests/test_bench.py
git commit -m "feat(bench): markdown report aggregator"
```

---

### Task 7: CLI wiring

**Files:**
- Modify: `benchmark/bench.py` (replace the `if __name__ == "__main__"` stub)
- Modify: `tests/test_bench.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_bench.py

class CliTest(unittest.TestCase):
    def test_run_writes_jsonl_and_report_reads_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _mini_corpus(Path(tmp))
            taskfile = Path(tmp) / "tasks.json"
            taskfile.write_text(json.dumps({
                "corpus": str(repo),
                "tasks": [{"id": "noop", "kind": "navigate",
                           "prompt": "count files",
                           "accept_cmd": "true", "expect_reuse": []}],
            }))
            results = Path(tmp) / "results"
            code = bench.main(["run", str(taskfile),
                               "--results", str(results),
                               "--conditions", "A", "--reps", "1",
                               "--dry-run"])
            self.assertEqual(code, 0)
            rows = [json.loads(l) for l in
                    (results / "runs.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["condition"], "A")

            code = bench.main(["report", "--results", str(results)])
            self.assertEqual(code, 0)
            self.assertTrue((results / "report.md").exists())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_bench.CliTest -v`
Expected: ERROR — no attribute `main`

- [ ] **Step 3: Implement the CLI (replace the `__main__` stub at the bottom)**

```python
# replace the existing `if __name__ == "__main__":` block at the end of bench.py

def _dry_runner(prompt: str, ws: Path, model: str, max_turns: int) -> str:
    """Zero-cost runner for harness testing: touches nothing, returns a
    minimal valid transcript."""
    return json.dumps({"type": "result", "num_turns": 0,
                       "usage": {"input_tokens": 0, "output_tokens": 0}})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("taskfile", type=Path)
    p_run.add_argument("--results", type=Path,
                       default=Path(__file__).parent / "results")
    p_run.add_argument("--conditions", nargs="+", default=["A", "B"])
    p_run.add_argument("--reps", type=int, default=1)
    p_run.add_argument("--only", nargs="*", default=None,
                       help="task ids to run (default: all)")
    p_run.add_argument("--dry-run", action="store_true",
                       help="exercise the harness without calling claude")
    p_rep = sub.add_parser("report")
    p_rep.add_argument("--results", type=Path,
                       default=Path(__file__).parent / "results")
    args = parser.parse_args(argv)

    if args.cmd == "report":
        runs = args.results / "runs.jsonl"
        rows = [json.loads(l) for l in runs.read_text().splitlines()] \
            if runs.exists() else []
        out = args.results / "report.md"
        out.write_text(report(rows))
        print(out.read_text())
        return 0

    cfg = load_tasks(args.taskfile)
    runner = _dry_runner if args.dry_run else claude_runner
    runs_path = args.results / "runs.jsonl"
    args.results.mkdir(parents=True, exist_ok=True)
    tasks = [t for t in cfg.tasks if args.only is None or t.id in args.only]
    total = len(tasks) * len(args.conditions) * args.reps
    done = 0
    for task in tasks:
        for cond in args.conditions:
            for rep in range(args.reps):
                done += 1
                print(f"[{done}/{total}] {task.id} {cond} rep{rep}", flush=True)
                row = run_one(cfg.corpus, task, cond, rep, args.results,
                              cfg.model, cfg.max_turns, runner=runner)
                with runs_path.open("a") as fh:
                    fh.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the full bench test module**

Run: `.venv/bin/python -m unittest tests.test_bench -v`
Expected: all tests PASS (13 across Tasks 1–7)

- [ ] **Step 5: Commit**

```bash
git add benchmark/bench.py tests/test_bench.py
git commit -m "feat(bench): CLI with dry-run mode"
```

---

### Task 8: Author the private-corpus task file

Ten tasks: five reuse-bait (a helper exists; the bait is reimplementing it), five navigation. The symbol names below come from the private-corpus digest as of 2026-08-08; **step 1 verifies each against the current digest before the file is committed** — replace any symbol that no longer exists with a neighbor from the same package.

**Files:**
- Create: `benchmark/tasks/private-corpus.json`
- Delete: `benchmark/tasks/.gitkeep`

- [ ] **Step 1: Verify the referenced symbols exist**

Run:

```bash
python3 ~/workspace/hologram/hologram.py build --root ~/workspace/private-corpus --quiet
for s in MathOps normalize Rational ChangeSeq then Claim Warrant \
         Adjudication TopoGraph canonicalHash VarBindings Substitution; do
  grep -q "$s" ~/workspace/private-corpus/PROJECT_DIGEST.md && echo "ok  $s" || echo "MISS $s"
done
```

Expected: `ok` for every symbol. For any `MISS`, open the digest, pick an
equivalent symbol from the same package, and substitute it consistently in step 2.

- [ ] **Step 2: Write the task file**

```json
{
  "corpus": "~/workspace/private-corpus",
  "model": "sonnet",
  "max_turns": 40,
  "tasks": [
    {"id": "weighted-avg", "kind": "reuse",
     "prompt": "Add a method to compute the weighted average of a list of Rational values with Rational weights, in the arithmetic package. Follow existing conventions.",
     "accept_cmd": "git -C {ws} diff --name-only | grep -qi arithmetic",
     "expect_reuse": ["MathOps.add", "MathOps.multiply", "normalize"]},

    {"id": "delta-compose", "kind": "reuse",
     "prompt": "Add a way to combine a list of deltas into a single delta applied in order.",
     "accept_cmd": "git -C {ws} diff --stat | grep -q .",
     "expect_reuse": ["ChangeSeq.then", "ChangeSeq.empty"]},

    {"id": "rational-compare", "kind": "reuse",
     "prompt": "Add a helper that returns the largest Rational in a non-empty list.",
     "accept_cmd": "git -C {ws} diff --stat | grep -q .",
     "expect_reuse": ["MathOps.compare"]},

    {"id": "claim-supersede", "kind": "reuse",
     "prompt": "Add a query that returns only the active claims from a collection of claims (excluding superseded and retracted ones).",
     "accept_cmd": "git -C {ws} diff --stat | grep -q .",
     "expect_reuse": ["EntryLifecycle"]},

    {"id": "binding-merge", "kind": "reuse",
     "prompt": "Add an operation that checks whether two binding sets are compatible and merges them if so.",
     "accept_cmd": "git -C {ws} diff --stat | grep -q .",
     "expect_reuse": ["Substitution.check", "VarBindings.of"]},

    {"id": "find-lifecycle", "kind": "navigate",
     "prompt": "In one paragraph: where is a claim's lifecycle state stored and which types are involved when a claim is superseded? Do not modify any files.",
     "accept_cmd": "true",
     "expect_reuse": []},

    {"id": "find-entry", "kind": "navigate",
     "prompt": "In one paragraph: trace what happens from the application entry point to the first domain type that gets constructed. Do not modify any files.",
     "accept_cmd": "true",
     "expect_reuse": []},

    {"id": "find-digest-flow", "kind": "navigate",
     "prompt": "In one paragraph: how is a structural graph's canonical digest computed and who calls it? Do not modify any files.",
     "accept_cmd": "true",
     "expect_reuse": []},

    {"id": "find-arith-users", "kind": "navigate",
     "prompt": "List the packages that depend on the arithmetic kernel and say what each uses it for, briefly. Do not modify any files.",
     "accept_cmd": "true",
     "expect_reuse": []},

    {"id": "find-warrant-shape", "kind": "navigate",
     "prompt": "Describe the Warrant type: its components and how it relates to claims and evidence. Do not modify any files.",
     "accept_cmd": "true",
     "expect_reuse": []}
  ]
}
```

- [ ] **Step 3: Validate the file loads**

Run: `.venv/bin/python -c "import sys; sys.path.insert(0,'benchmark'); import bench; from pathlib import Path; c = bench.load_tasks(Path('benchmark/tasks/private-corpus.json')); print(len(c.tasks), 'tasks, corpus', c.corpus)"`
Expected: `10 tasks, corpus /Users/sebastian/workspace/private-corpus`

- [ ] **Step 4: Commit**

```bash
git rm -q benchmark/tasks/.gitkeep
git add benchmark/tasks/private-corpus.json
git commit -m "feat(bench): private-corpus task set — 5 reuse-bait, 5 navigation"
```

---

### Task 9: Runbook + smoke run

**Files:**
- Create: `benchmark/README.md`

- [ ] **Step 1: Write the runbook**

```markdown
# hologram benchmark

Measures agents with a digest (condition A) against agents without (condition B)
on the same tasks in the same corpus. Every run is a headless `claude -p` session
in a throwaway git worktree; metrics come from the transcript, an acceptance
command, and a digest-diff duplication check.

## Smoke run (do this first — ~4 sessions)

    .venv/bin/python benchmark/bench.py run benchmark/tasks/private-corpus.json \
        --only weighted-avg find-lifecycle --reps 1
    .venv/bin/python benchmark/bench.py report

Read the two transcripts in `benchmark/results/` end to end once. Check that the
condition-A agent actually opened PROJECT_DIGEST.md (a Read tool call naming it)
and that the acceptance commands measured what you meant.

## Full matrix (~60 sessions — this costs real money)

    .venv/bin/python benchmark/bench.py run benchmark/tasks/private-corpus.json --reps 3
    .venv/bin/python benchmark/bench.py report

## Reading the report

- **duplication** is the headline: % of reuse-task runs where the agent wrote a
  name-similar function instead of calling the existing one. Directional claim:
  A < B.
- **reads / searches / turns / tokens** are the navigation story: A should read
  and search less on navigate tasks.
- With reps=3 the numbers are directional, not significant. Don't publish a
  percentage without saying n. The per-task list at the bottom of the report is
  for eyeballing which tasks discriminate — drop tasks that saturate (everyone
  succeeds or everyone fails) and replace them.

## Honest limitations

- The duplication detector is a heuristic (call-chain + name similarity). Review
  its verdicts manually before quoting them; the per-task list makes that fast.
- One corpus, one model, ten tasks. This answers "does the digest help *here*",
  not "does it help everywhere".
- Navigation tasks are judged by acceptance `true` — their signal is in
  reads/searches/tokens, not correctness. A human should spot-check the answers.
```

- [ ] **Step 2: Dry-run the whole matrix to prove the harness end-to-end (zero tokens)**

Run: `.venv/bin/python benchmark/bench.py run benchmark/tasks/private-corpus.json --reps 1 --dry-run && .venv/bin/python benchmark/bench.py report`
Expected: 20 `[n/20] …` progress lines, then a report table with conditions A and B, all zeros. Delete `benchmark/results/` afterwards so dry-run rows never mix with real ones: `rm -rf benchmark/results`.

- [ ] **Step 3: Commit**

```bash
git add benchmark/README.md
git commit -m "docs(bench): runbook with smoke run and honest limitations"
```

- [ ] **Step 4 (human decision point — not automated):** run the smoke run from the runbook. It spends real tokens and needs the user's account; get explicit go-ahead first. After the smoke run is reviewed, the full matrix is the user's call.

---

### Task 10: Wire results into the README (only after real runs exist)

**Files:**
- Modify: `README.md` (the "Does it actually help?" section)

- [ ] **Step 1:** After the full matrix has run and the report reviewed, replace the closing sentence of the README's honest-take section ("has not been rigorously benchmarked yet…") with a summary of the measured result — including n, the corpus, and the direction of every metric, whether or not it favors the tool. If the result is null or mixed, say exactly that; the honest-take section's credibility is worth more than the claim.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: replace unbenchmarked caveat with measured results"
```

---

## Self-review

- **Spec coverage:** metrics (duplication, reads/searches, tokens, turns, acceptance) → Tasks 2/3/5; conditions A/B parity → Task 4; matrix orchestration + zero-cost testing → Tasks 5/7; corpus tasks → Task 8; run protocol + interpretation discipline → Task 9; README closure → Task 10. Gap check: variance/reps handled via `--reps` + runbook caveat (means only, n stated) — deliberate YAGNI on bootstrap CIs at n=3.
- **Placeholder scan:** every code step contains full code; Task 8 symbols carry an explicit verification step instead of assumed correctness; Task 10 is intentionally conditional on human-run results (a decision, not a placeholder).
- **Type consistency:** `Task`/`Config` fields match between loader (T1), `run_one` (T5), CLI (T7), and task file (T8: `id/kind/prompt/accept_cmd/expect_reuse`). `parse_transcript` keys (`reads/searches/edits/turns/tokens_in/tokens_out`) match `report` consumers and `run_one`'s row assembly. `judge_reuse` returns `new_lines/reused/duplicated`; `run_one` stores `len(new_lines)` as `new_lines` count — consistent with `ReportTest` fixtures.
