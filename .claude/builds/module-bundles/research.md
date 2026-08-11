# module-bundles — build & deepen notes

The `/build` output for this feature: the reasoning behind each decision, the current-state
survey, the work scope, and the open questions.

**Decisions from this build:** D-632–D-641 (10 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

**Date:** 2026-08-11
**Status:** Decisions made, not specified, not implemented
**Next:** `/specify module-bundles`

---

## The problem

Federal deployments cannot install some modules at all. `browser` is the clear case: it needs
playwright and a chromium binary, and its capability is network egress from an agent. A config
flag that disables it is not enough for an enclave that must show the capability is not present.

The existing mechanism cannot express this. `pip install arc-agent` writes every file in the
wheel, and no flag installs a subset of a package's files. So the code is on disk regardless of
tier, and the only controls are extras (which gate dependencies, not source) and config.

## Three locations, three jobs

Code appears in exactly one of them.

| Location | Scope | Holds | Agent can write? |
|----------|-------|-------|------------------|
| `${ARC_CONFIG_DIR:-~/.arc}/modules/<name>/` | deployment, one per box | module **runtime**: `_runtime.py`, hook handlers, background loops | **no** — outside the tool fence |
| `<agent_dir>/capabilities/<name>/` | per agent | the module's **tools and skills**, copied at install | **yes, on purpose** — untrusted root |
| `<workspace>/` | per agent | agent state and working data | yes — and only ever data |

The workspace holds no module code and no symlink to module code. A module's tools read and write
the workspace at runtime, which is the point; the code that implements them does not live there.

The split between the first two rows is by kind, not by convenience. A tool is a leaf the agent
invokes and is meant to improve. `_runtime.py` owns background loops, hook registration, and shared
state for every agent on the box. Editing the first changes what one agent can do; editing the
second changes the harness.

Copying rather than loading in place is deliberate (D-648), on two grounds:

- **Drift is the product.** An agent's skills are supposed to improve for that agent. A shared
  read-only original cannot improve for one agent without changing every agent.
- **Bounded scan roots.** Loading in place means every installed module and every extension adds a
  scan root, so the root list grows without bound and its shape is set by artifacts that do not
  exist yet. One known copy destination keeps the loader's roots fixed and enumerable forever.

Which modules an agent loads is still decided by its `[modules.NAME]` config entries.

## The shape

Two artifacts instead of one.

| Artifact | Varies by deployment | Contains |
|----------|---------------------|----------|
| `arc-agent` wheel | no | core + always-on modules |
| `*.arcbundle` | yes | the optional modules this deployment is approved for |

The core is approved once and is byte-identical everywhere. The bundle is the auditable delta,
and it reads as a list of module names rather than a diff of two binaries.

## Why not the alternatives

**One package per optional module.** Real absence, correct layering, and it matches what
`arcgateway` already does for platform adapters. Rejected on cost: eighteen distributions to
version, sign, release, and keep in dependency lockstep with a core that changes weekly.

**Post-install prune of `site-packages`.** No new packages and genuine absence. Rejected because
it mutates an installed wheel: RECORD hashes stop matching, `pip check` and any integrity scanner
flags it, and the next `uv sync` silently restores every pruned module. An integrity control that
an upgrade undoes is not a control.

**Custom wheel per deployment.** Rejected as a violation of the stringency-dial rule (CLAUDE.md
line 129). Federal would run code that was never built or tested anywhere else, every module
combination becomes its own hash and its own approval, and the wheel under test stops being the
wheel that ships.

**Extras only, files always present.** This is today. It is the smallest change and it does gate
the dangerous dependencies. Rejected as insufficient for the stated requirement: the module source
is on disk inside the boundary, and an auditor scanning for capability finds it.

## Current state — what already exists

The pieces are mostly built. This is assembly, not invention.

| Piece | Where | State |
|-------|-------|-------|
| Folder-presence discovery | `core/module_discovery.py` | Done. `_is_module()` requires `capabilities.py` + `_runtime.py`; a missing folder is simply not discovered. |
| Config activation, default off | `core/module_discovery.py:module_statuses` | Done. Discovered-but-disabled is already a valid listable state. |
| Path-based module loading | `capabilities/capability_loader.py` | Done for capabilities (`spec_from_file_location`). Modules do not use it yet. |
| Signed materialize of file trees | blueprint v2 | Done. Same operation, different source. |
| Signed-manifest gate on load | `arcrun/backends/loader.py` | Done for executor backends. The pattern to copy. |
| Trusted vs untrusted scan roots | `capability_loader.py:_UNTRUSTED_ROOTS` | Done. Modules are appended as trusted roots at `agent_lifecycle.py`. |
| Overlay dir outside tool sandbox | `arcprompt` (D-466) | Done. The write-barrier precedent. |
| Extras for heavy deps | `arcagent/pyproject.toml` | Partial. `memory` and `azure` exist; `browser-use` is a prose comment, not a gate. |

The gap is the bundle format, the CLI, the second scan root, and the write barrier.

## Module split — there isn't one

All eighteen are optional (D-632). The earlier draft proposed an always-bundled set; that was
wrong, and dropping it removes work rather than adding it.

Checked against the code before adopting: nothing in `core/` imports a module or requires one to
exist. The one hardcoded module name in core, `core/tool_policy_bridge.py`, is a tool-name prefix
list (`memory`, `session`, `user_profile`) used for caller-DID binding. It never matches when the
module is absent, which is correct behavior.

| Module | LOC | Module | LOC |
|--------|-----|--------|-----|
| browser | 2399 | scheduler | 1382 |
| voice | 2021 | policy | 1310 |
| tasks | 1968 | messaging | 1049 |
| workflows | 1861 | skills | 811 |
| planning | 1754 | memory | 638 |
| proactive | 1538 | pulse | 619 |
| user_profile | 1456 | workpad | 520 |
| web | 1424 | runcontrol | 354 |
| connectors | 1407 | session | 1153 |

About 22,000 LOC, all of it currently in every wheel including federal ones.

Migration order is by risk, not by size: `memory` first (it already has an extra and a `NullBrain`
default, so absent-safe is already true and the step proves the machinery), then `browser` (the
case that motivated the build), then the rest.

An agent with zero modules installed is a working nucleus with builtin tools. `arc agent build`
and the personal installer run `arc module install --all` so nothing about the out-of-box
experience changes.

## The signing gap — a live defect, not new work

Writing D-649 turned up something worse than a missing feature. `arc trust approve` cannot sign,
and above personal tier nothing else can either.

`capability_loader.py:_passes_trust_gate` runs two checks, in this order:

1. **Signature floor.** Above personal, a missing or invalid detached signature denies outright,
   before TOFU is consulted at all. Requires a pinned trusted key.
2. **TOFU layer.** Governs first sighting and drift.

`arc trust approve` only touches the second one — it pins a source hash. It even prints a note
conceding the pin changes nothing at personal tier. Nothing in the product writes a detached
signature for a capability or a skill.

So today:

- An agent-improved skill can never load above personal tier.
- No command exists that would make it load.
- `README.md` line 114 already advertises "signed agent-authored capabilities (SPEC-033)".

The machinery is all there. `arc prompt edit` already signs overlays with the operator key
(`commands/prompt.py:93`), `commands/operator.py` resolves the signer, and the arcui capability
inventory already renders every gated row with its status and source. What is missing is the
action, in both surfaces, and the procedure that tells an operator how to use it.

D-651 (CLI), D-652 (arcui), D-653 (docs) close it. All three ship together with the code.

## Work scope

1. **Bundle format.** Manifest with name, version, per-file sha256, issuer; Ed25519 detached
   signature over canonical JSON. Reuse `arcrun/backends/_verifier.py` shapes rather than
   inventing a second envelope.
2. **`arc module` command set.** `list`, `install <names...>`, `install --all`,
   `install --from <bundle>`, `remove <name>`, `bundle <names...> -o <file>`. Install writes the
   config entry as well as the files.
3. **Second scan root.** `module_discovery` scans the workspace module dir alongside the in-wheel
   dir; `ModuleStatus` gains `origin`.
4. **Path loader.** `configure_module_runtimes` resolves a folder and loads via
   `spec_from_file_location`, one path for both origins.
5. **Write barrier.** Materialized trees at `0444`/`0555`, excluded from the tool sandbox's
   writable set.
6. **Move the optional modules** out of the wheel into the catalog, one at a time, `memory` first.
7. **Absent-safe suite.** Parametrized over `discover_modules()`: remove one, run a real turn,
   expect green.
8. **Per-agent capability copy.** Install copies the module's tools and skills into
   `<agent_dir>/capabilities/<name>/`; remove deletes them. Runtime stays at the deployment root.
9. **Signing, CLI.** `arc trust approve` writes a detached signature with the operator key, pins
   the key, records the TOFU pin. Replaces the hash-only pin. `disapprove` is the exact inverse.
10. **Signing, arcui.** Approve action on gated rows in the capability inventory, same code path,
    operator-authenticated, off `/ws/chat`, on no tool registry.
11. **Docs.** `docs/runbooks/signing-capabilities.md` (new), `docs/walkthrough/10-security-model.md`
    (where the two gates sit), `README.md` (correct the supply-chain row). `mkdocs --strict` gates
    the links.

Order matters twice. Items 3, 4, and 7 land before 6, so each removal is verified as it happens.
Items 9 through 11 land before 8, because copying capabilities into an untrusted root without a
way to re-sign them would ship the drift feature with no way to use it above personal tier.

## Risks

**Executable code in the workspace is new.** ADR-029 put agent *state* there. Code is a different
class of thing, and a writable module directory is a self-modification path (ASI05, ASI06). The
write barrier is the control, not the signature — a signature checked at install says nothing
about a file edited afterwards. This is the single highest-risk item in the build.

**Trusted scan roots.** Materialized modules must be trusted roots (they are first-party, signed),
but they arrive by the same motion as connector bundles, which D-565 deliberately routes through an
*untrusted* root. The distinction is issuer, not delivery mechanism, and the code must make that
explicit or the next person will collapse them.

**LOC budget.** Core is capped under 3,500 (ADR-004). The bundle verifier and installer belong in
`arccli` and a non-core module home, not in `core/`.

## Open questions — all four closed 2026-08-11

1. **Which modules are optional?** All of them (D-632). Verified against the code: core imports no
   module and requires none.
2. **What is the catalog?** A build output, not a new place in the source tree. Modules stay at
   `packages/arcagent/src/arcagent/modules/` in git. Release CI packages each folder into a signed
   bundle and publishes the bundles as release assets; the wheel excludes the directory (D-633).
   Nothing moves, nothing is duplicated, and a source checkout is still the one place a module is
   edited.
3. **Who signs?** The Arc release key, in release CI, at build time. First-party modules are never
   self-signed and never signed on the installing machine, at any tier. Third-party modules are
   extensions, and their signing story is a separate build.
4. **What does remove do?** Delete the materialized tree, drop the config entry, non-zero exit and
   a loud error if either half does not complete (D-639). The "removing an in-wheel module" edge
   case disappeared with question 1.

### Follow-on decision

Making every module optional created a new problem: with no modules in the wheel, a source checkout
has nothing to load, and a rebuild-sign-install cycle per edit is friction severe enough that
people route around the boundary.

The first answer was a symlink into the workspace. That was wrong twice over — the workspace is not
a home for code, and skipping verification creates a second load path that exists only for
convenience. D-641 is now `arc module install --from-source <name>`: bundle one module, sign it
with a local development key, install it through the identical verify-then-materialize path. One
code path, no exceptions, no unverified load anywhere. The dev key is trusted at personal tier
only, so a dev-signed module will not load on an enterprise or federal box.

### Closed — tools and skills copy (D-648, D-649)

Copy, not load in place. I recommended the opposite and was wrong on both counts.

Drift is a product goal, not a defect — the skill improver exists to make an agent's skills better
for that agent, and a shared read-only original cannot do that for one agent without changing all of
them. And the scan-root argument is decisive on security: loading in place means every module and
every extension adds a root, so the list grows without bound in a shape dictated by artifacts that
have not been written yet. A fixed copy destination means no future artifact gets to add a load
path.

The untrusted classification I raised as an objection turns out to be the correct reading. A
directory whose contents are *meant* to change should be adjudicated on every load. Copies arrive
Arc-signed and load clean on first install at every tier; once an agent edits one, TOFU adjudicates
at personal, and enterprise and federal refuse it until an operator re-signs through the SPEC-035
path (D-649).

That tier split is worth stating plainly because it is a real product boundary, not an
implementation detail: **an agent may improve its own skills on a laptop; a federal box improves
nothing without a human signing for it.**
