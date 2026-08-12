# SPEC-065 — Gateway Messaging + Media

| | |
|---|---|
| **Status** | **Complete** — 26/26 tasks, suite `10959 passed, 43 skipped, 0 failed` |
| **Branch** | `feat/gateway-messaging-media` |
| **Type** | integration |
| **Workflow** | standard (phase approvals) |
| **Decisions** | D-668 – D-681 in [`.claude/decisions-log.md`](../../decisions-log.md) |
| **Requirements** | REQ-296 – REQ-318 (23, all Must) |
| **Components** | COMP-001 – COMP-014 |
| **Tasks** | T-923 – T-948 (26) across 4 phases |

## Documents

| File | State |
|---|---|
| [PRD.md](PRD.md) | Validated |
| [SDD.md](SDD.md) | Validated — every REQ traced to a component |
| [PLAN.md](PLAN.md) | Validated — every COMP has a task |

## Why this exists

Three defects, each verified against the tree rather than reported:

1. **A photo produces no run at all.** The Telegram adapter registers `MessageHandler(filters.TEXT, …)`, so a photo never reaches a handler — and `InboundEvent.message` is a bare `str`, so there is nowhere for media to travel even once it does.
2. **Every message starts a fresh run.** `deliver_message` is published only to the module bus for teammates; every human surface calls `agent.run()` instead, which always begins a new turn. The session was never rotating — it only looked that way.
3. **The arcui inbox is silently empty.** `ensure_nats_server` exists and works but has exactly one caller, `arccli/commands/_serve.py`. Any other launch path leaves the broker unstarted and `_connect_backend` returning `None`.

Plus one structural complaint: each platform is a separate package reimplementing the same security properties, so the fourth adapter is the fourth chance to forget a size cap.

## Phases

| Phase | Tasks | Ships |
|---|---|---|
| 1 · Foundation | T-923 – T-928 | The parts envelope and the media store everything writes through |
| 2 · Core | T-929 – T-936 | Delivery joins the live turn; history keeps refs; replay names files |
| 3 · Integration | T-937 – T-946 | Adapters in-tree, broker guaranteed, teammate parity |
| 4 · Polish | T-947 – T-948 | Trust boundary and the pairing collapse |

Phase 1 opens with **T-923 (red)** — the contract suite fails its inbound-image case against today's text-only adapters. That failure *is* defect 1, reproduced before anything is fixed.

## Operator constraints (REQ-315 – REQ-318)

Only add, simplify, condense, re-route. Specifically:

- Session history keeps being written to the workspace and keeps being listable and replayable. The gateway FIFO being deleted is transient inbound buffering and never held a line of it.
- A module left with no caller is deleted in the same change, not abandoned.
- Session replay names a media file; it does not serve its bytes to the browser.

## Open questions

- The 100-message flood cap disappears with T-932; reinstate it beside the agent's per-session lock only if flooding is observed.
- ~~T-940 deletes the three adapter distributions~~ — confirmed by the operator: they become in-package modules.

## Learnings

### Phase 1 — Foundation (T-923 – T-928) · complete

Suite went `1 failed, 10761 passed` → `1 failed, 10834 passed`, the one failure being
T-923's intentional RED. The +73 reconciles exactly (74 new tests, 1 deliberate deletion),
which is the check that proves nothing was quietly skipped.

**Two real defects the RED wave found, neither of them in the spec's list of three.**

1. **`dispatch_and_await` skipped the rotation generation** — it called
   `build_session_key(agent_did, resolved_did)` (generation 0) while `handle()` used
   `current_session_key()`. After `/new`, a turn on that path landed in the session the
   operator had just cleared. Fixed by extracting `SessionRouter._canonicalise`, which
   *both* entry points now call — the duplication was the root cause, so removing the
   duplication is the fix rather than patching the second copy.
2. **Three adapters hand-built platform-scoped session keys.** Real violations of the
   stated invariant, but **inert** — `SessionRouter.handle` already overwrote them. Dead
   misleading code, not a routing bug. Worth recording that the first reading looked like
   a live outage and was wrong; reading the router is what settled it.

**Deleting a pinned test can carry an obligation.** `test_session_keys_intentionally_diverge_across_platforms`
existed to fail at exactly this moment and its docstring instructed whoever aligned the
key formats to update ADR-002 first. The test was correctly deleted; the ADR update was
missed until caught in review. SPEC-025 ADR-002 and TD-2 are now marked superseded.
**A test that pins a limitation should be read as a message to whoever removes it.**

**Side effect worth naming: cross-platform session unification landed early.** With
adapters no longer composing keys, web and Slack now converge on one session for the same
(agent, user) pair. That was SPEC-025 TD-2 and was scoped to SPEC-026. It is a
consequence of REQ-304, not a scope grab, but it is a behaviour change operators will see.

### Phase 2 — Core (T-929 – T-935) · complete, T-936 deferred

Suite `1 failed, 10761 passed` → `2 failed, 10861 passed`. The two failures are T-923's
designed RED and T-936, which is deferred to Phase 3 for the reason below.

**THE FINDING THAT MATTERS: everything built in Phases 1–2 is inert.**
`MediaStore`, `InboundMessage`/`MediaPart`/`TextPart`, and `arcagent.PartTranslator` have
**zero production callers** between them. Five correct, well-tested modules that nothing
invokes; a photo sent to Telegram still produces no run. This is the PLAN's sequencing —
the activating wiring is T-937/939/940 — but **do not describe Phases 1–2 as delivering
media support.** They deliver components. T-923's still-red contract test is the canary
and is behaving exactly as designed.

Caught only by checking for callers before "fixing" the T-936 fixture. The fixture writes a
raw `MediaPart` dict; `PartTranslator` writes `{"type":"text","text":"<readable line>",
"media":{…}}`. Neither is "the real shape" until Phase 3 wires the path, so **T-936 must be
settled against what the wired path actually produces** — rewriting it now would convert an
honest red into a false green.

**T-936 needs no vitest and no TSX change.** The translator stores media as a text block
whose text already names file and kind, and `type: "text"` means the SPA renders it via
`block.text` rather than the JSON fallback. The "add a frontend test runner" question was
never a real decision.

**Two corrections to earlier claims in this spec's own notes:**
- arcrun's injection queues are **bounded at `maxsize=16`** (`arcrun/state.py:66-67`), not
  unbounded. Capacity went 100-with-silent-drop → **16-with-loud-refusal** (`put_nowait`
  raises `QueueFull`, surfaced to the sender). The flood cap did not disappear; it tightened
  and stopped being silent.
- `start_tracked_run` registers under `session.session_id`, and `open_or_resume` sets
  `session_id = key`, so it matches what `deliver_message` looks up. No mismatch — a
  suspected fourth defect that was not real.

**Deleting the Hermes race guard was the live risk, and it was handled correctly.** The
suites were rewritten, not removed: assertions moved from `router.agent_tasks_spawned` to
`runs_opened(session_key)` — same invariant, new location — keeping the `[5,10,20,50]`
parametrisation and preserving genuine interleaving by parking the model. An
instantly-returning agent makes `asyncio.gather` run sequentially, and the test would pass
with the guard removed.

**Orchestration mistake to avoid repeating:** the RED test for T-929 and the FIFO deletion in
T-932 were placed in the same wave, and the deletion transitively broke the test's import via
`arcgateway.session`. Telling the builder "do not edit that test" was right in spirit but
created a deadlock. **Put a deletion and the tests that depend on it in different waves.**

### Phase 3 — Integration (T-936 – T-946) · complete

Suite went fully green for the first time: `1 failed, 10761 passed, 42 skipped` →
**`0 failed, 10944 passed, 43 skipped`**. +183 tests. All three PRD defects fixed AND
wired into production.

**The wiring gap from Phase 2 was real and had to be closed by hand.** T-937–941 landed with
a green adapter suite while `MediaStore` still had **zero production callers** — the tests
constructed the store themselves, so a deployed gateway still stored nothing. Fixed in
`bootstrap.py`: `_media_store_for(agent_did)` resolves a store **per agent**.
**Per-agent is a security property, not tidiness.** The router is fleet-wide but a workspace
belongs to one agent; a single shared store would file every agent's inbound attachments in
one agent's home — an ADR-029 breach that also puts one correspondent's files inside another
agent's readable workspace. The builder proposed a single `media_store=` on `SessionRouter`
and flagged it as "a real design question". It was right to ask.

**Tests that stop being able to fail were the recurring theme of this whole spec.** Four
separate instances, all found by checking rather than by a failure:
- `test_contract.py` — the photo-defect canary — went **dark** the moment T-940 deleted the
  distribution its gate probed (`find_spec("arcgateway_telegram")`). It read `2 skipped`,
  which looks like "no failure". Re-gated on `discover_adapters()`.
- **Six** `pytest.importorskip("arcgateway_telegram")` calls skipped forever for the same
  reason (skips rose 42→49). Both SDKs are installed, so those tests should always have run;
  re-pointed at `telegram` / `slack_sdk`, the genuinely optional part. 48 passed.
- `test_gateway_core_ships_no_platform_adapter_modules` asserted "remote platforms live in
  packages" — **the opposite of REQ-308** — and survived T-940 only because it looked for
  `telegram.py` while the platform had become the directory `telegram/`. Passing on a
  technicality while asserting a false principle is worse than failing. Replaced with
  `test_the_registry_names_no_platform` (scoped to the discovery functions by AST) and
  `test_every_in_tree_platform_exports_the_descriptor`.
- The T-936 fixture hand-rolled a raw `MediaPart` that **no producer emits**; it would have
  gone green against a completely unwired pipeline. Rebuilt to compose its turn through the
  real `PartTranslator`.

**Scope a boundary guard to the axis it actually guards.** The first cut of
`test_the_registry_names_no_platform` flagged `OFFICIAL_ADAPTERS` — which names platforms
*legitimately*, as a federal trust allowlist (federal tier blocks unofficial platforms; every
load is audited official/unofficial). Vetting is supposed to be an explicit list; discovery
is not. Narrowed to the discovery functions rather than deleted or loosened.

**A leaf must not learn a platform's name.** Moving `build_session_key` into
`arctrust/session_identity.py` (correct — arcagent may not import arcgateway, so the formula
had to sit on a leaf both reach) carried a docstring mentioning Telegram and broke arctrust's
own "never name a chat platform" guard.

**Verify the fix as hard as the finding.** A builder correctly reported that a parity test was
unsatisfiable, and proposed pinning `IdentityConfig(did=_AGENT_DID)`. That one-line fix was
wrong — a pinned DID makes arctrust hunt for a key file the tmp dir has no. The real fix was
to derive from `ArcAgent.did`, which makes the assertion bite instead of comparing against a
constant that could never equal a `sha256(public_key)[:8]` suffix.

**Missing dependency edges, again.** Four arcgateway modules import `arctrust` and
`pyproject.toml` declared none of it. Works locally only because the uv workspace resolves
everything; a standalone install fails at import. This repo has already been burned by this
exact class once and papered over it with a runbook rule.

### Phase 4 — Polish (T-947 – T-948) · complete

Final suite: **`0 failed, 10959 passed, 43 skipped`** (baseline `1 failed, 10761 passed, 42
skipped`) — +194 tests, zero regressions across every phase. ruff clean repo-wide; mypy
--strict clean on arcgateway (54 files), `arcagent/parts.py` and `arctrust/session_identity.py`.

**T-947 was already satisfied, and that is the finding** — REQ-305 holds *by construction*.
The human gate is per-tool-call inside the agent (`arcagent/tools/human_gate.py:159`, reached
from `tool_registry.py:455` only for `global.forbidden_composition`); the gateway constructs
no gate at all. An unpaired sender's photo triggers **no download attempt** (`bot.get_file`
await count 0) because custody runs *after* the pairing gate (`session.py:419` → `:441`).
The 9 tests were mutation-checked (5/5 mutants killed) rather than trusted, because "no gate
fired" is precisely the assertion that also passes when nothing is wired.

**T-948's acceptance criterion is factually wrong and was overridden.** Recorded so nobody
re-litigates it: there are **six** pairing modules (~1865 LOC), not four; "runner untouched"
contradicts collapsing `pairing_allowlist` (`runner.py:228`); "session untouched" contradicts
collapsing `session_pairing` (`session.py:59`); and "the pairing suite passes unchanged"
contradicts any collapse, since six test files import the sub-modules by name. The honest
version shipped instead: delete the zero-importer `pairing_postgres.py` stub, and merge
`pairing_signature` + `pairing_throttle` into `pairing.py` — justified because their lazy
imports exist *only* to dodge a circular dependency, not for tidiness. `arcgateway.pairing`
was already the single name other packages import.

### Post-completion — the invisible-guard defect

Found after all 26 tasks were green, by two agents independently, and worth recording because
it is the sharpest example of this spec's recurring theme.

`InboundEvent`'s `message`/`parts` validator reconciled the two views **only when one was
missing**. Supply both, disagreeing, and two contradictory views survived validation — the
exact divergence its own docstring promised was impossible. Worse than "they differ": *each
view dropped what the other held*. A caption lived in no part, so a parts-reading consumer
never saw it; a media ref lived in no text, so a message-reading consumer never saw the photo.

**Why a green suite could not see it.** `grep "message=.*parts=" packages/arcgateway/tests`
returns nothing. Every test filled exactly one view, so both branches of the guard produced
the same result as having no guard at all. The defect lived in the case the happy path never
constructs. Not reachable in production — `session.py:124` is the only site supplying both and
it is consistent by construction — so it was a latent hole, not an outage.

**The invariant held by convention, not by enforcement**, which is what made accepting the
`parts`-on-`InboundEvent` deviation defensible in the first place. The justification was that a
validator kept the projections in sync. It did not, quite.

**Fix, and the correction to the fix.** `parts` wins whenever present (it is the richer view;
it can express an artefact and `message` cannot). But deriving alone **silently discards the
caller's text** — trading one silent failure for another. It now also logs a WARNING naming the
dropped text: loud degrade. Refusing outright would cost the whole turn instead of one field,
the worse trade on an inbound path.

Four tests cover it, verified to bite by reverting the validator and watching them fail. One
exists solely to stop the fix becoming noise: **the consistent path must not warn**, because
every media message goes through `message=flatten_text(words), parts=words` and a warning
there would flood the log until the signal was worthless.

**Verification notes for later phases**
- The adapter contract suite **skips silently** under the system interpreter and only
  reproduces the defect under `.venv/bin/python`. A skip reads as "no failure" and will
  pass a careless RED gate.
- `StoredMedia.kind` is an untyped `str` while `MediaPart.kind` is a `Literal`. The two
  modules had already diverged in their documented vocabulary (`file` vs `document`/`video`)
  and no test caught it. **Phase 2 should type the seam**, since MediaStore output feeds
  MediaPart directly and a bad `kind` would only fail at runtime.
- **T-923's acceptance is only half met.** It requires the suite to "enumerate adapters via
  the registry"; the registry is COMP-003, which does not exist until T-937. The suite uses
  a literal list with the swap marked. Close this in T-937.
- `packages/arcgateway/CLAUDE.md` currently instructs the opposite of REQ-308 ("New chat
  platform → new package, not code in this core"). **T-940 must rewrite it in the same
  change**, or the next session will follow it and undo the work.
