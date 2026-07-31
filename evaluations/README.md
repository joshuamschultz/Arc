# evaluations/ — the LongMemEval memory harness

SPEC-060. A standalone tree that drives the **unmodified** Arc stack as a black box and
measures what its memory can recall. Zero framework changes: every requirement is met by
configuration, by public API already in a package's `__all__`, or by harness-side code.

`evaluations/` is a peer of `packages/`, deliberately **not** a uv workspace member.
Nothing under `packages/` may import it — `tests/architecture/test_no_evaluations_layering_violations.py`
enforces that, and the same guard keeps `ingest/` from importing `longmemeval/`.

## Layout

```
ingest/        source-agnostic — the reusable pathway
               types adapter chunker fidelity agent_factory driver consolidation lifecycle
longmemeval/   the first and only consumer
               adapter dataset query judge scoring reference_prompts agreement
               runner ledger preflight budget manifest hygiene scrub cli
longmemeval/data/   gitignored — the dataset JSON (manual download)
runs/          gitignored — throwaway per-question workspaces. NO harness code, ever.
results/       gitignored — the JSONL ledger + run_manifest.json
```

`runs/` holds no source because **git cannot re-include a file beneath an ignored
directory**. A `!` negation cannot rescue it.

## Running it

Always as a module, from the repo root. A bare `python evaluations/longmemeval/cli.py`
fails with `ModuleNotFoundError: No module named 'evaluations'`, because the script's own
directory goes on `sys.path` instead of the repo root.

```bash
uv run python -m evaluations.longmemeval.cli --help
uv run python -m evaluations.longmemeval.cli --dry-run --phase oracle   # estimate, no spend
uv run python -m evaluations.longmemeval.cli --smoke 3                  # gates entry to full-s
uv run python -m evaluations.longmemeval.cli --phase oracle
```

Before the first run, two things must happen by hand:

1. Download `longmemeval_oracle.json` / `longmemeval_s_cleaned.json` from
   `xiaowu0162/longmemeval-cleaned` into `evaluations/longmemeval/data/`. The SHA-256 is verified at
   preflight — the Sept-2025 "cleaned" revision is **not** numerically comparable to the
   original, which is why the hash and revision are pinned in the manifest.
2. Export the judge's own API key. It is read from the environment only, never a file.

Two measurement passes are worth running before any scored phase:

```bash
uv run python -m evaluations.longmemeval.measure_turn_lengths  # is "never split mid-turn" achievable?
uv run python -m evaluations.longmemeval.damage_report         # how often do the filters eat gold evidence?
```

The damage report decides whether any score is trustworthy at all. Run it first.

## What the number means

Read `run_manifest.json`, not just the accuracy. `measurement_scope` records that
`workpad` and `policy` were left **enabled**, so the reported accuracy covers arcmemory
*plus two additional system-prompt summarizers*. Every figure is `personal`-tier specific.

Three accuracies are reported with three different denominators, and they are not
interchangeable: task-averaged (macro over the six question types), overall (micro over
all scored questions), and abstention (separate). Any per-type stratum under n=30 is
flagged `directional` — at n=13 and 77% observed, the Wilson interval spans roughly
50% to 92%.

## Tests

```bash
uv run pytest evaluations/tests/ -q -rs
```

The `-rs` matters. One test needs the real dataset and a live judge key; it skips with a
reason naming exactly what is missing, so a skip is never mistaken for a pass.
