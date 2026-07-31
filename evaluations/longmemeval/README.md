# LongMemEval — the memory recall harness

Drives the **unmodified** Arc stack as a black box and measures what its memory
can recall across many prior sessions. Every requirement is met by
configuration, by public API already in a package's `__all__`, or by
harness-side code — the framework is never patched to make a number look better.

---

## Layout

```
ingest/       source-agnostic — how a corpus becomes agent input
              types adapter chunker fidelity agent_factory driver
              consolidation lifecycle limits models
config/       *.toml.example templates for the generated per-question agent
tests/        this harness's suite (541 tests)
data/         gitignored — the corpus JSON (manual download)
runs/         gitignored — throwaway per-question workspaces. NO source, ever.
results/      gitignored — the JSONL ledger + run_manifest.json

adapter dataset query judge scoring reference_prompts agreement
runner ledger preflight budget manifest hygiene scrub paths cli
```

`ingest/` must not import from the harness modules beside it — that keeps the
pathway reusable for a second corpus.
`tests/architecture/test_no_evaluations_layering_violations.py` enforces it.

`runs/`, `results/` and `data/` hold no source because **git cannot re-include a
file beneath an ignored directory**. A `!` negation cannot rescue it.

---

## Before the first run

Two things must happen by hand:

1. **Download the corpus** — `longmemeval_oracle.json` and
   `longmemeval_s_cleaned.json` from `xiaowu0162/longmemeval-cleaned` into
   `evaluations/longmemeval/data/`. The SHA-256 is verified at preflight: the
   Sept-2025 "cleaned" revision is **not** numerically comparable to the
   original, which is why both the hash and the revision are pinned into the
   run manifest.
2. **Export the judge's API key.** It is read from the environment only, never
   from a file.

---

## Running it

Always from the repository root, always as a module.

```bash
# what the flags do
uv run python -m evaluations.longmemeval.cli --help

# estimate spend without making a single model call
uv run python -m evaluations.longmemeval.cli --phase oracle --dry-run

# 3-5 questions end to end; passing this is what opens --phase full-s
uv run python -m evaluations.longmemeval.cli --smoke 3 \
    --dataset-sha256 <sha256> --dataset-revision <hf-revision>

# a scored phase
uv run python -m evaluations.longmemeval.cli --phase oracle \
    --dataset-sha256 <sha256> --dataset-revision <hf-revision>

# one question, printed with what it answered
uv run python -m evaluations.longmemeval.cli --question-id <id> \
    --dataset-sha256 <sha256> --dataset-revision <hf-revision>
```

`python evaluations/longmemeval/cli.py` does **not** work: Python puts the
script's own directory on `sys.path` instead of the repository root, so the
first import fails with `ModuleNotFoundError: No module named 'evaluations'`.

### Phases

`--phase` is never defaulted, because the three differ by roughly two orders of
magnitude in spend.

| Phase | Scope |
|---|---|
| `oracle` | The oracle corpus — smallest, fastest |
| `sample-s` | ~50 questions drawn from S; requires `--strategy {even,proportional}` |
| `full-s` | The full S corpus; gated behind a passing `--smoke` run |

### Exit codes

| Code | Meaning | Code | Meaning |
|---|---|---|---|
| 0 | clean phase | 4 | repo hygiene assertion failed |
| 1 | everything else | 5 | spend ceiling reached |
| 2 | usage error | 6 | smoke gate not passed |
| 3 | preflight assertion failed | | |

---

## Measurement passes worth running first

```bash
# is "never split mid-turn" actually achievable on this corpus?
uv run python -m evaluations.longmemeval.measure_turn_lengths

# how often do the sanitizing filters eat the gold evidence?
uv run python -m evaluations.longmemeval.damage_report
```

**Run the damage report first.** It decides whether any score is trustworthy at
all: if the filters are removing the evidence a question depends on, the
accuracy that follows is measuring the filter, not the memory.

---

## What the number means

Read `run_manifest.json`, not just the accuracy.

- `measurement_scope` records that `workpad` and `policy` were left **enabled**,
  so the reported accuracy covers arcmemory *plus two additional system-prompt
  summarizers*. It is not an arcmemory-only figure.
- Every figure is `personal`-tier specific.
- Three accuracies are reported against three different denominators and are
  **not** interchangeable: task-averaged (macro over the six question types),
  overall (micro over all scored questions), and abstention (separate).
- Any per-type stratum under n=30 is flagged `directional`. At n=13 and 77%
  observed, the Wilson interval spans roughly 50% to 92% — that is a range, not
  a result.

---

## Tests

```bash
uv run pytest evaluations/longmemeval/tests -q -rs
```

The `-rs` matters. One test needs the real corpus and a live judge key; it skips
with a reason naming exactly what is missing, so a skip is never mistaken for a
pass.
