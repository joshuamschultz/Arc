# H-040 — Multi-Agent-Type Fleet

> **Status:** DESIGN DRAFT (for Planner review — no production code yet)
> **Author:** design agent · 2026-08-29
> **Scope:** let an arcteam fleet hold multiple agent *types* — native `arcagent`
> plus foreign harnesses (hermes, openclaw, LangGraph, CrewAI, …) — as
> first-class members that share memory, the arcui dashboard, and fleet
> coordination, without weakening the four pillars.

---

## 1. Problem + goal

Today an arcteam fleet is, in practice, a fleet of one type. The **roster** is
abstract — an `Entity` is a plain Pydantic record keyed by DID
(`packages/arcteam/src/arcteam/types.py:172-203`) — but **everything that runs or
renders a member assumes it is a native `ArcAgent`**:

- The always-on fleet holds `ArcAgent` instances and nothing else
  (`packages/arcgateway/src/arcgateway/fleet.py:31-51`).
- The subprocess worker instantiates `ArcAgent` from an `arcagent.toml`
  (`packages/arccli/src/arccli/agent_worker.py` docstring, "Instantiates ArcAgent
  with the config").
- The dashboard reads each member from `team/<dir>/arcagent.toml` and hard-wires
  every detail tab (llm, tools, prompts, skills, policy…) to arcagent's file
  layout (Explore finding: `arcui/web/src/pages/agent-detail.tsx:113-131`,
  `policy-views.tsx:110` references `arcagent.toml`).

**Goal:** introduce **one seam** — a `HarnessAdapter` Protocol plus an `AgentType`
descriptor — so a foreign harness can be enrolled, addressed, coordinated,
rendered, and audited exactly like a native agent, with native `arcagent`
becoming *one implementation of the seam rather than the hard-coded case*. The
seam mediates identity, capability inventory, run/dispatch, memory, roster/status,
and message send/receive.

### Non-goals

- **Not** re-homing agent internals. A foreign harness keeps its own loop, LLM
  calls, and tools — arc does not run its ReAct loop.
- **Not** a translation layer for foreign *tools*. We coordinate members; we do
  not proxy every internal tool call of a foreign runtime into arc's registry.
- **Not** federation across deployments. One `arc ui start` process still serves
  one fleet (`arcgateway/fleet.py:21-23`).
- **Not** changing the native-agent trust story. `arcagent.toml` + REQ-030 stays.
- **Not** giving a foreign harness raw `Brain` / `build_brain` access (see §5 —
  that bypasses the isolation guard).

---

## 2. The seam

### 2.1 One contract: `HarnessAdapter`

A fleet member is anything that can answer these questions and accept these
verbs. This mirrors arc's existing modular pattern — one unifying surface,
backends split out via extensions/modules, same as gateway platforms
(`AdapterSpec`, `arcgateway/adapters/base.py`) and the memory `Brain` Protocol
(`arcagent/brain/protocol.py`).

```python
# arcteam/harness/protocol.py  (NEW — illustrative signatures, not final)

@runtime_checkable
class HarnessAdapter(Protocol):
    """What a fleet needs FROM a member to treat it as first-class.

    Inverse of arcagent.fleet.FleetProvider (which is what a member needs FROM
    the fleet). Native arcagent implements BOTH: it consumes FleetProvider and
    it is wrapped by an ArcAgentHarness that satisfies this."""

    # ---- Pillar 1: Identity -------------------------------------------------
    @property
    def did(self) -> str: ...                 # did:arc:<org>:<harness_type>/<hash>
    @property
    def public_key(self) -> bytes: ...        # Ed25519 verify key (hex on the wire)
    @property
    def enrollment(self) -> EnrollmentGrant: ...  # operator-signed proof (see §3)

    # ---- Roster / status ----------------------------------------------------
    @property
    def handle(self) -> str: ...              # @mention name; agent://<handle>
    @property
    def harness(self) -> str: ...             # "arcagent" | "hermes" | "openclaw"
    async def capabilities(self) -> Sequence[str]: ...  # for roster + policy tags
    async def status(self) -> MemberStatus: ...         # online | idle | offline | health

    # ---- Run / dispatch -----------------------------------------------------
    async def dispatch(self, event: InboundEnvelope) -> AsyncIterator[MemberOutput]: ...
        # The single run entrypoint. Mirrors arc-agent-worker's
        # `collect(agent.run(message, session=...))`. A foreign harness maps its
        # own turn onto InboundEnvelope -> stream of MemberOutput.
        # MemberOutput is an ARCTEAM-owned envelope (see §2.4), NOT arcllm.Delta —
        # arcteam may not import arcllm.

    # ---- Message receive ----------------------------------------------------
    async def deliver(self, message: Message) -> None: ...
        # A DM / @mention / channel post the fleet routed to this member.
        # (Message SEND is not here — a member sends via FleetMessenger, §6.)

    # ---- Memory (mediated, never raw Brain) --------------------------------
    def memory_port(self) -> MemoryPort | None: ...     # DID-scoped; see §5
```

Plus a thin **registration descriptor**, modeled directly on SPEC-065's
"a platform is a folder": a gateway platform is a `PLATFORM = AdapterSpec(...)`
exported from `adapters/<name>/`, discovered by a directory scan
(`arcgateway/adapters/registry.py:58` `_DESCRIPTOR = "PLATFORM"`,
`:165` `discover_adapters()`, `:192` `_load_descriptor` which rejects anything not
an `AdapterSpec` at `:221-223`). Deleting the folder deletes the platform and the
registry learns neither name (`arcgateway/CLAUDE.md`, "A platform is a folder").
An `AgentType` follows the same rule — a folder exporting the descriptor, scanned,
deletable, with the core learning nothing about hermes or openclaw:

```python
class AgentType(BaseModel):
    """A discovered harness kind. Folder-scanned exactly like adapters/ (SPEC-065)."""
    name: str                 # "hermes"
    build: Callable[[Entity, ...], HarnessAdapter]   # how to construct a live member
```

**Trust is NOT a descriptor field — it is pinned in core fleet code.** A
folder-discovered descriptor must never be able to declare its own `trusted: bool`
or `sandbox`: a foreign extension folder could ship `trusted=True` and self-bless,
the exact trap avoided everywhere else in this design (`arcteam/agent_fleet.py:120-129`,
the operator-key-not-sidecar-key argument). This mirrors how `arcbundle` pins its
dev-issuer in the verifier's own code rather than trusting a bundle to name its
issuer. The rule, in the fleet's code and nowhere else:

- **Trusted iff `harness == "arcagent"`** — the single in-tree implementation, known
  to the fleet by identity, not by a flag it carries. Every folder-discovered type
  is untrusted, unconditionally (ASI04). There is no descriptor field that can say
  otherwise.
- **Sandbox placement is the FLEET's decision from trust tier**, never the
  descriptor's: native (`arcagent`) may run in-process; every foreign type runs
  out-of-process, mandatory, all tiers (§9), microVM at federal. The descriptor
  cannot request in-process.

**Typed DEGRADED results for absent capabilities (fail-closed, never a crash).**
A foreign harness will not support every verb — it may have no skills surface, no
workflow authoring, no private memory. The Protocol handles this the way arc
already handles a missing seam: `arcagent.fleet` returns "unavailable" rather than
raising when a fleet feature is absent (`arcagent/fleet.py:10-12`, "every fleet
feature reports itself unavailable while the agent runs on normally"). Each
`HarnessAdapter` method that a type cannot serve returns a typed
`Degraded(capability, reason)` result, **not** an exception and **not** a silent
empty — so the fleet can render "capability X not supported by this harness"
instead of a broken read, and any authorization check for an unsupported
capability is a fail-closed DENY (a type that does not implement memory sharing
cannot be coaxed into a memory write).

### 2.2 Native arcagent becomes one implementation

`ArcAgentHarness(HarnessAdapter)` wraps an `ArcAgent`:

- `did` / `public_key` → `agent._identity` (`arcagent/core/agent.py:124-136,474`).
- `dispatch` → `collect(agent.run(...))`, exactly what the subprocess worker
  already does (`arccli/agent_worker.py`).
- `deliver` → the existing messaging inbox loop (`arcgateway/fleet.py:5-8`).
- `memory_port` → a DID-scoped wrapper over the existing `Brain`
  (`arcagent/brain/protocol.py:23`) that re-imposes the `state()` guard (§5).
- `harness = "arcagent"` — which is *why* the fleet trusts it and *why* the fleet
  may place it in-process (both decided in fleet code by that identity, §2.1, never
  by a descriptor flag).

Nothing about native agents changes on disk or in behavior; they are simply now
*addressed through the same Protocol* as everyone else. This satisfies pillar
Composability rule 4 ("isolate and completely rewrite a module … same input/output
format, it will still work") — the fleet talks only to `HarnessAdapter`.

### 2.3 Where it lives (dependency direction)

`arcteam/harness/` is the natural home: arcteam already owns roster, messaging,
and coordination and legally imports `arcstore` + `arctrust` but never `arcagent`
(`packages/arcteam/CLAUDE.md`, "Layer"). The `HarnessAdapter` **Protocol** is
defined in arcteam (it speaks primitives + `Entity` + `Message`, all arcteam/
arctrust types — no arcagent import). `ArcAgentHarness` (the native impl) lives
in `arcgateway` (which legally imports arcagent), alongside the existing
`FleetRegistry`. Foreign harnesses ship as **extensions** — a folder exporting an
`AgentType`, scanned the way adapters and connectors are, deletable with no core
change (build-principles "Module vs Extension").

### 2.4 `MemberOutput` — an arcteam-owned envelope, never an arcllm type

arcteam's legal imports are `arcstore` + `arctrust` only (`arcteam/CLAUDE.md`,
"Layer"). `Delta` is an **arcllm** wire type; putting it in the `HarnessAdapter`
Protocol would drag arcllm into arcteam and break the concern split (root
`core.md` §1). So arcteam defines its own minimal **`MemberOutput`** envelope,
mirroring the JSON-lines shape the subprocess worker protocol already speaks
(`arccli/agent_worker.py`: "One Delta JSON object per line … ending with a
Delta(kind='done', is_final=True)") — a `kind` (`text` / `done` / `error`), a text
chunk, and an `is_final` flag, as a pydantic model in arcteam. `ArcAgentHarness`
(in arcgateway, which legally imports both arcagent and arcllm) maps
`arcllm.Delta → MemberOutput` **at its own edge**; a foreign adapter emits
`MemberOutput` directly. The wire boundary already exists — this only gives it an
arcteam-owned name.

### 2.5 Layering: Protocol in arcteam, native impl in arcgateway (satisfies both readings)

Two build docs read differently at a glance: root `core.md` says "the alpha fleet
direction is **arcteam → arcagent**", while `arcteam/CLAUDE.md` says arcteam
depends on arcstore + arctrust and is **"Not arcagent"** with "No upward imports of
arcagent". H-040's split resolves both, and the builder must not cite `core.md` to
justify importing arcagent into arcteam:

- The **`HarnessAdapter` / `AgentType` / `MemberOutput` Protocol and types live in
  arcteam** and reference only arcteam + arctrust types (`Entity`, `Message`,
  `EnrollmentGrant`, primitives). **arcteam imports no arcagent** — the existing
  layering test stays green.
- The **native `ArcAgentHarness` implementation lives in arcgateway**, which
  already legally imports arcagent (`arcgateway/CLAUDE.md`, "Use `import arcagent`")
  and holds the `FleetRegistry` today (`arcgateway/fleet.py:31`). The
  "arcteam → arcagent" *conceptual* direction (orchestration knows about agents) is
  realized through the Protocol seam, not a Python import edge.

This is the same Protocol-here / impl-there pattern arcagent already uses for the
`Brain` seam (Protocol in `arcagent/brain`, impl in `arcmemory`) and `FleetProvider`
(Protocol in `arcagent/fleet`, impl in `arcteam/agent_fleet.py`).

---

## 3. Identity & enrollment (LEAD)

This is the crux: a native agent is trusted because its DID matches the key in
its own `arcagent.toml` that it signs with (REQ-030,
`arccli/commands/team.py:360-386`). A **foreign harness is untrusted code
(ASI04)** — we cannot infer trust from "it holds a key". Trust must be an
**operator-signed enrollment**, verified at every load and every route.

### 3.1 Identity — DID is already parameterized by type

No new primitive needed to *mint*. A DID is
`did:arc:<org>:<agent_type>/<key_hash>` (`arctrust/identity.py:62-70`), and
`AgentIdentity.generate(org, agent_type)` works for any type
(`identity.py:304-321`). A hermes member gets `did:arc:acme:hermes/<hash>`. The
`agent_type` segment becomes the harness discriminator, surfaced as the roster
badge (§4). The foreign harness generates and holds its **own** Ed25519 keypair
(non-exportable, `0600`, like every arc key — `identity.py:243-267`). Arc never
holds a foreign harness's private key.

### 3.2 Enrollment — SIGNED, verified, fail-closed

**Gap today:** `arc team register` just writes the `Entity` and emits an
`entity.registered` **audit** event (`arcteam/registry.py:88-107`). The
registration is logged but **not operator-signed as a grant** — for a native
agent that is acceptable (the DID is self-consistent with a locally-created
`arcagent.toml`), but for foreign untrusted code it is not: anyone who can write
a registry record could enroll a rogue member (ASI03/ASI10).

**Proposal — `EnrollmentGrant` (new arctrust primitive).** Model it on the two
operator-signing primitives that already exist:

- `sign_approval_for_hash(call_hash, operator)` → `ApprovalGrant`
  (`arctrust/policy.py:549`), verified by `verify_approval`
  (`policy.py:666`); operator authority is `OperatorApprovalAuthority`
  (`policy.py`, used by `arc approve` at `arccli/commands/approve.py:131-132`).
- `ScenarioGrant` (`policy.py:123-155`) — a standing operator-signed record
  binding a small tuple of facts, `frozen=True`, carrying `approver_did`,
  `public_key`, `signature`.

`EnrollmentGrant` binds the operator signature over the canonical bytes of the
enrollment facts:

```python
class EnrollmentGrant(BaseModel):        # arctrust/policy.py or identity.py
    model_config = ConfigDict(frozen=True)
    did: str                  # the member DID being admitted
    handle: str               # its @mention handle
    harness: str              # "arcagent" | "hermes" | ...
    member_public_key: bytes  # the member's OWN verify key — pinned here
    capabilities: frozenset[str]
    clearance: str            # max classification admitted (never exceeds operator grant)
    audit_mode: str           # "boundary" | "full" — tier dial, see §10 (federal requires "full")
    not_before: str           # ISO ts
    nonce: str                # replay protection
    # NOTE: no expiry field — by design. Revocation is the mechanism (§3.5); a
    # time-bomb on the grant is the mapping-approval-expiry incident waiting to
    # recur (a valid grant silently lapsing on a timer, reverting a working
    # member). See §13-ruling-4 note.
    approver_did: str
    approver_public_key: bytes
    algorithm: str = "ed25519"
    signature: bytes          # operator sig over canonical_json(the above, minus sig)
```

Signed with the deployment **operator** authority (the same key that signs the
audit chain and approvals — never an agent key; `arctrust/CLAUDE.md`, "Operator
key ≠ agent identity"). Canonicalization goes through `canonical_json`
(`arctrust/canonical.py`), like every other signed artifact.

### 3.3 Who signs, and the flow

Reuse the **mechanical approval subsystem** already shipped (memory pointer
`project_mechanical_approval_subsystem`): a pending row → `arc approve` signs a
grant. Concretely:

1. `arc team register <handle> --type agent --harness hermes --pubkey <hex> --caps ...`
   — instead of writing the `Entity` directly, writes a **pending enrollment
   row** to the approval store (`arcstore/src/arcstore/approvals.py`,
   `PendingApproval`/`ApprovalStore`). For `--harness arcagent` (the default) the
   existing self-consistent path may still auto-enroll (native trust unchanged);
   for any **foreign** harness, enrollment is **pending until operator-signed**.
2. `arc approve <id>` — operator reviews (handle, harness, pubkey fingerprint,
   caps, clearance) and signs an `EnrollmentGrant` via
   `OperatorApprovalAuthority` (mirrors `approve.py:131-132`).
3. The signed grant is stored **on the Entity** (new field
   `Entity.enrollment: EnrollmentGrant | None`) and the Entity is written to the
   registry.

### 3.4 How the fleet refuses an un-enrolled / forged type

Fail-closed at three chokepoints:

- **Registry admission** (`arcteam/registry.py:88` `register`): reject any
  `Entity` whose `harness != "arcagent"` and whose `enrollment` is absent or
  fails `verify_enrollment(entity, operator_public_key)`. The operator pubkey
  comes from the trust store (`arctrust/trust_store.py`), never from the grant
  itself (the self-blessing trap called out in `arcteam/agent_fleet.py:120-129`).
- **Roster eligibility** (`arcteam/agent_fleet.py:43-55` `list_agents`): a member
  is eligible for work only if `status == active` **and** its enrollment
  verifies. An unverifiable member never reaches a routing decision.
- **Dispatch / start** (`AgentType.build` in `arcgateway/fleet.py`): the fleet
  refuses to construct/start a `HarnessAdapter` for an Entity that fails
  verification — so forged code never runs, even if a registry row was tampered
  in.

Because `member_public_key` is pinned inside the signed grant, a later swap of
the Entity's `public_key` (TOCTOU, `threat-surface.md`) breaks verification. The
member's message/ToolCall signatures must validate against that pinned key.

### 3.5 Replay / nonce / revocation

- **Replay:** `EnrollmentGrant.nonce` + `not_before`; registration is one-shot —
  the registry already rejects a duplicate DID or handle
  (`registry.py:91-95`). A replayed enrollment for an existing DID is refused.
- **Message replay** is already covered: `Message` carries `sig/nonce/signer_did`
  and the consumer verifies before delivery (`arcteam/types.py:120-152`, ASI07).
  A foreign member's outbound messages ride the same signed path (§6).
- **Revocation:** today `EntityStatus` has only `active`
  (`arcteam/types.py:21-25`). Add `revoked` / `suspended`. `arc team revoke
  <handle>` sets status and appends to a revocation set; the roster filter and
  the messaging consumer must check status before routing or delivering. For a
  hard cut, the operator also removes the member's pubkey from the trust store,
  so its message signatures stop verifying (identity-service revocation, ASI10).
  Follows the shared-knowledge revocation pattern already in the tree
  (`arcteam/shared_knowledge/backend.py:250-259`).

### 3.6 KEY CUSTODY — the fork the whole design turns on

There are two, and only two, ways a foreign member's signature can exist. This
decision shapes identity, authorization, and audit, so it is stated explicitly
rather than left implicit.

**Option A — native key, foreign member signs itself (RECOMMENDED).** The foreign
harness generates and holds its own Ed25519 keypair; its DID is derived from that
key (`did:arc:<org>:<harness>/<hash>`, `arctrust/identity.py:62-70`); it signs its
own messages and ToolCalls. Arc holds only the **public** key (pinned in the
`EnrollmentGrant`, §3.2). This is exactly how a native agent works today — the
`IdentityLayer` requires the presented pubkey's fingerprint to match `agent_did`
(`arctrust/policy.py:425-437`, `did_matches_pubkey`), so a member's call
authenticates on the identical mechanism whether it is arcagent or hermes. Arc
never custodies a foreign private key, so there is no proxy to compromise.

**Option B — adapter custodies a key and signs AS the member (proxy — REJECTED as
default).** The `HarnessAdapter` holds a signing key and stamps the member's
signature on its behalf. This makes the adapter a **confused-deputy surface**: the
adapter process now holds credentials that speak *as* the agent, and a compromised
adapter can sign anything in that agent's name — precisely the ASI03
identity/privilege-abuse the pinned-key model exists to prevent. If a deployment
*must* use Option B (a harness that genuinely cannot sign — e.g. a closed SaaS
runtime), then:

- the adapter's signing key is a **distinct, derived identity**, not the agent's.
  Use `derive_child_identity(parent_sk, spawn_id, ...)`
  (`arctrust/identity.py:443-477`, HKDF-SHA256, clearance never exceeds parent) so
  the proxy key is bound to — and demonstrably subordinate to — an operator-held
  parent, and the child DID is distinguishable in every audit record;
- the `EnrollmentGrant` records `custody = "proxy"` and names the proxy DID, so
  the fleet and the UI can see this member is adapter-signed, not self-signed;
- the proxy adapter runs out-of-process under Arc supervision (§9, deployment
  posture A) so its blast radius is a single sandboxed member, never the fleet.

**What binds adapter identity to agent identity (Option B):** the child-DID
derivation (`identity.py:443`) means the proxy key cannot be minted without the
parent secret, and its clearance is `min(requested, parent)` — a compromised
adapter can never out-clear or impersonate a *different* agent, because its DID is
cryptographically tied to one parent and one `spawn_id`. **Recommendation to the
Planner: default to Option A everywhere; permit Option B only for a harness that
cannot sign, and only via child-DID derivation + proxy custody flag + sandbox.**

**Enrollment/identity model in three sentences:** A foreign harness holds its own
non-exportable Ed25519 key and DID (`did:arc:<org>:<harness>/<hash>`); to become a
fleet member it needs an **operator-signed `EnrollmentGrant`** that pins its
handle, harness, public key, capabilities, and clearance, produced by the same
`arc approve` mechanical-approval path that signs tool approvals today. The fleet
**verifies that grant against the operator trust-store key — never a key the
member supplies — at registry admission, at roster eligibility, and at
dispatch/start**, so an un-enrolled, forged, or tampered member never routes,
runs, or renders. Revocation flips `EntityStatus` and removes the pubkey from the
trust store, after which the member's signed messages and calls stop verifying.

---

## 4. Shared UI / fleet (arcui)

**What arcui assumes today** (Explore findings): the roster comes from
`GET /api/team/roster` → `team_roster.list_team()` reading each
`team/<dir>/arcagent.toml` (`arcgateway/team_roster.py:50,116-132`); every
agent-detail tab is hard-wired to arcagent's config layout
(`agent-detail.tsx:113-131,267-271`; `policy-views.tsx:110`). There is a free-form
`type` string (rendered as an "Agent Type" KV row and in the DID meta line) but
**no harness/runtime badge and no branching by member kind**.

**What H-040 needs:**

- **Roster source becomes harness-aware — the `EntityRegistry` is the single
  source.** The roster must include foreign members, which have no
  `arcagent.toml`. Serve the roster from the `EntityRegistry` (which already
  carries `did/handle/name/type/capabilities/status/clearance`, `types.py:192-203`)
  joined with live status, rather than the on-disk `arcagent.toml` scan. arcui
  reads it **via arcteam** (the legal arcui → arcteam edge), and the registry path
  **replaces** the filesystem scan — it must **not** add a new direct `team/` read
  in arcui: SPEC-022 stays intact (arcui must not touch `team/` directly; go
  through `fs_reader` — `arcgateway/CLAUDE.md`). The `RosterEntry` gains a
  `harness` field (`team_roster.py:32-47`).
- **Type badge.** Render `harness` as a first-class badge on the fleet card
  (next to the existing online/activity dot, `fleet/agent-card.tsx:56-74`) and in
  agent-detail. This is the "type badge" the task calls for and is a pure
  additive UI change.
- **Capability-gated detail tabs.** Tabs that assume `arcagent.toml`
  (llm, tools, prompts, skills, policy) must render only when the member declares
  the capability, driven by `HarnessAdapter.capabilities()`. A foreign member
  shows the tabs it actually supports (identity, runs, inbox, status) and hides
  the rest — no broken reads of a nonexistent config. A **native `arcagent`
  declares all those capabilities, so it keeps every rich `arcagent.toml`-driven
  tab exactly as today** — the capability route is additive, it does not demote
  native agents.
- **Runs / activity by DID.** Already DID-keyed: agent-detail joins runs, traces,
  stats, and audit from the arcstore Observe mirror by the member's **DID**
  (Explore: `observe.runs/traces/stats/audit(agent=<DID>)`). A foreign member
  that emits arc-shaped run/audit records (via the mediated dispatch/audit path,
  §7) renders here **with no UI change** — this is the biggest reuse win.

**Fleet coordination (arcteam):** needs the member reachable as
`agent://<handle>` (already works — resolution is DID-keyed and abstract,
`registry.py:40-65`) and able to receive a wake/notice (`HarnessAdapter.deliver`,
mapping to the existing `FleetMessengerAdapter.send_notice` path,
`arcteam/agent_fleet.py:58-73`). Relevance-triage (agents publish digests, a
ranker picks the responder — `arcteam/CLAUDE.md`) works for a foreign member as
long as it publishes an `AgentDigest`; a member that publishes none is simply
never auto-selected (graceful, not broken).

---

## 5. Shared memory (no cross-agent bleed)

**The guard that matters** (Explore findings): the fail-closed DID gate
`state()` at `arcagent/modules/memory/_runtime.py:179-215` resolves per-turn
memory by the DID bound in a `ContextVar` (`_current_did`), keyed into a
per-agent `_registry` dict; a missing or mismatched DID calls `_fail_closed()`
(`_runtime.py:279-300`), which emits `memory.isolation_fault` and raises
`MemoryIsolationError`. This is what stops one agent reading another's memory
when 32 `ArcAgent` instances share one process.

**The danger for a foreign type:** that guard lives in the *arcagent wrapper*,
not in arcmemory. `arcmemory.build_brain(context)` takes a plain
`{workspace, agent_did}` dict (`arcmemory/provider.py:43-83`) and **any caller
supplying those keys gets a Brain**; on-disk isolation is only the workspace path
(`<workspace>/memory/index.db`, `arcmemory/db.py:1-11`). **A foreign harness that
called `build_brain` directly with a chosen `agent_did`/`workspace` would bypass
the ContextVar guard entirely** and could name another agent's workspace.

**Design rule:** a foreign harness **never** gets raw `Brain` / `build_brain`.
It gets a `MemoryPort` returned by `HarnessAdapter.memory_port()`, constructed by
the fleet and **bound to the member's enrolled DID and workspace**. The port:

- re-imposes the same DID scope check as `state()` before every call — the port
  is constructed with the member's DID from its `EnrollmentGrant`, and refuses
  any call whose target DID/workspace differs (fail-closed, same
  `MemoryIsolationError` shape);
- passes every write/read through the existing sign→authorize→audit wrapper
  (`arcmemory/tools.py:140-184`) and the no-read-up classification gate
  (`arcmemory/security.py:228-258`), so a foreign member is authorized and
  audited identically to a native one;
- for **private** memory, points at the member's own workspace SQLite (hard
  shared-nothing, `db.py`);
- for **shared** memory, routes to arcteam's `TeamMemoryService`
  (`arcteam/memory/service.py:38-214`), which is *already* designed for foreign
  callers ("usable by arcagent, langchain, crewai, or direct", `service.py:41`),
  with promotion-gated writes and classification-filtered reads.

So shared memory is *already* harness-agnostic; the work is the **mediated
private-memory port that re-imposes the DID gate arcmemory itself does not
enforce**. The foreign harness must not receive a workspace path it can point
anywhere — the port owns the path from enrollment.

### 5.1 A foreign agent's memory contribution IS untrusted input (ASI06)

A foreign harness is untrusted code (ASI04), so **everything it writes into shared
memory is untrusted input** and must never be able to launder itself into a
higher-trust or higher-classification record. Three rules, all enforceable on
existing seams:

- **Arc stamps classification, not the foreign agent.** A shared-memory write goes
  through `TeamMemoryService.promote()`
  (`arcteam/memory/service.py:107-123`), which routes to the `PromotionGate` — arc
  code that validates, classifies, and audits before writing ("Write entry point.
  Validates, classifies, audits, writes.", `service.py:114`). The classification
  on the stored entry is set **arc-side at the boundary** from the member's
  enrolled clearance (§3.2), never from a label the foreign runtime supplies. A
  foreign member cannot self-declare `UNCLASSIFIED` on sensitive content.
- **No-write-down / no-read-up already holds.** Reads are classification-filtered
  on the way out (`service.py:76-93,139-146`, `ClassificationChecker.check_access`
  / `filter_results`), reusing arctrust's `dominates` comparator; private-memory
  reads pass the same no-read-up gate (`arcmemory/security.py:228-258`). A foreign
  member cleared to `UNCLASSIFIED` can neither read above its clearance nor write a
  record labeled below the source's — the gate is arc's, not the harness's.
- **Provenance is permanent and quarantinable.** Every promoted entry records its
  writer (`promote(..., agent_id)`, `service.py:112`); model the foreign-source
  marking on the shared-knowledge backend, which already pins the signer and
  stamps provenance/revocation on each document
  (`arcteam/shared_knowledge/backend.py:139-142,250-259`,
  `_pin_signer` + `knowledge.revoked`). H-040 adds a `source_harness` /
  `foreign: true` marker at promotion so a foreign-sourced memory **stays
  distinguishable forever** and can be quarantined (excluded from a native agent's
  retrieval, or revoked wholesale) if the contributing harness is later found
  compromised — the same way a revoked shared-knowledge doc is skipped from search
  (`backend.py:166,222`). Retrieval into a native agent can then treat foreign-
  sourced memory as a lower-trust tier rather than blending it indistinguishably
  with the agent's own captures (LLM06/ASI06 memory poisoning).

The contributor DID is **already** stamped today (`promote(..., agent_id)`,
`service.py:112`) — H-040 adds only the `harness` marker; no new provenance
infrastructure is required, just an added field.

### 5.2 Independent hardening item — the unguarded `build_brain` factory

Flagged by the Planner as its **own** hardening item, tracked independently of
H-040: `arcmemory.build_brain(context)` (`arcmemory/provider.py:43-83`) trusts any
in-process caller to pass an honest `{workspace, agent_did}` — the DID scope gate
lives only in the arcagent wrapper (`_runtime.py:179`), not in arcmemory itself.
**Today, any in-process Python caller can already bypass the DID gate**, H-040 or
not. The durable fix is defense-in-depth inside arcmemory: bind `workspace ↔
agent_did` internally so the factory cannot be handed a mismatched pair. This does
**not** block H-040 (the mandatory out-of-process posture, §9, means foreign code
never reaches `build_brain` in-process), but it should be filed as a standalone
arcmemory hardening task so the guarantee does not rest solely on every caller
being polite.

---

## 6. Policy & audit (it may not run the arcrun loop)

**Native path today:** a tool call is signed (`sign_call` binds the ToolCall to
the agent's key, `arctrust/policy.py:415-419`), the `IdentityLayer` denies
unsigned/forged calls fail-closed (`policy.py:425-437`,
`did_matches_pubkey`), and the pipeline runs inside arcagent's dispatch
(`arcagent/core/agent_dispatch.py:152` passes `caller_did`; origin set at
`:420`). A foreign harness runs its **own** loop and its internal tool calls do
**not** flow through this pipeline.

**Design stance — mediate at the fleet boundary, not inside the foreign loop:**

- We do **not** try to authorize every internal tool call of a foreign runtime
  (that would mean owning its loop — a non-goal, and impossible for opaque
  harnesses). We authorize the actions a foreign member takes **through arc
  seams**: sending fleet messages, reading/writing shared or private memory,
  invoking arc-registered tools/connectors, and starting workflow runs.
- Each of those seams already carries a `PolicyPipeline` evaluation and an
  `AuditEvent` emission point. The `MemoryPort` (§5) evaluates policy per call
  (`arcmemory/tools.py:159-176`). Fleet message send is signed and audited
  (`arcteam/agent_fleet.py:194-212`, "Outbound notices are signed with the
  agent's own identity"). Any arc tool a foreign member calls goes through the
  same `sign_call` → pipeline → audit path a native agent uses.
- **The foreign member signs with its own enrolled key.** `sign_call` /
  `verify_call` already require the presented pubkey's fingerprint to match
  `agent_did` (`policy.py:425-437`) — the enrolled key from §3 is exactly that
  key, so a foreign member's calls authenticate on the identical mechanism. A
  call it cannot sign (because it doesn't hold the enrolled key) is denied
  fail-closed.
- **Origin tagging.** A foreign member acting on its own is interactive
  (`origin=None`); when driven by an arc workflow/schedule the driver stamps
  `workflow:<id>` / `schedule:<id>` (`policy.py:136-139`, `ScenarioGrant`), so a
  standing waiver earned by automation never covers the member's free-form work.

**Audit:** every seam above emits through the single arctrust `emit` point
(`arctrust/audit.py`), signed by the operator authority
(`arcteam/agent_fleet.py:198-205`). A foreign member's fleet-visible actions are
therefore in the tamper-evident chain identically to a native agent's. Actions a
foreign harness takes *entirely inside itself* (never touching an arc seam) are
outside arc's audit boundary by construction — this is a **stated trust boundary**
the Planner must accept: arc audits what crosses its seams, not a black-box
runtime's internals.

---

## 7. Four pillars + OWASP / ASI

| Pillar | How H-040 holds it |
|--------|--------------------|
| **Identity** | Every member has a DID; foreign members carry an operator-signed `EnrollmentGrant` pinning their key. `HarnessAdapter.did` is mandatory. |
| **Sign** | Enrollment is operator-signed and verified against the trust-store key at admission/eligibility/dispatch. Messages and tool calls are member-signed and verified. |
| **Authorize** | `PolicyPipeline` on every arc seam a member touches (memory, messaging, tools, workflow); member signs with its enrolled key; fail-closed on unsigned/forged. |
| **Audit** | Single arctrust `emit` point, operator-signed chain, on enrollment, message, memory op, tool call, and status change. |

| Threat | Mitigation |
|--------|------------|
| **ASI03 Identity & Privilege Abuse** | Per-member DID; operator-signed enrollment pins key + capabilities + clearance; no privilege inheritance; roster eligibility re-verifies. The memory `MemoryPort` re-imposes the DID gate arcmemory doesn't (`_runtime.py:179` semantics at the port). |
| **ASI04 Agentic Supply Chain** | A foreign harness is **untrusted code** — untrusted unconditionally, because the fleet trusts only `harness == "arcagent"` in its own code and no descriptor field can say otherwise (§2.1). It ships as a deletable folder-scanned extension, enrolled only by explicit operator signature, and runs out-of-process (below). Enrollment verification uses the operator key, never a key the harness supplies (the self-blessing trap, `agent_fleet.py:120-129`). |
| **ASI07 Insecure Inter-Agent Comms** | Foreign members ride the existing signed message path (`Message.sig/nonce/signer_did`, verify-before-deliver, `types.py:120-152`); classification travels on the envelope (`arcagent/fleet.py:40-56`). mTLS on NATS unchanged. |
| **ASI10 Rogue Agents** | Revocation via `EntityStatus.revoked` + trust-store pubkey removal; audit/telemetry monitoring already keyed by DID; anomalous member behavior is visible in the Observe mirror. |
| **LLM03/LLM06** | Untrusted-harness supply chain and memory poisoning: enrollment signing + `MemoryPort` sign→authorize→audit + no-read-up gate. |

**Sandbox (ASI05 unexpected code execution):** the **fleet** decides placement from
trust tier (§2.1), never the descriptor. Native arcagent (trusted by fleet-pinned
identity) may run in-process. A **foreign harness runs out-of-process — mandatory,
all tiers** — the `arc-agent-worker` subprocess model already
exists for federal isolation (`arccli/agent_worker.py`: own event loop, own
connection pool, own audit chain; resource limits applied by `SubprocessExecutor`
via `preexec_fn`). Foreign members reuse that isolation boundary; federal tier
can escalate to a microVM (`threat-surface.md` ASI05). The fleet talks to the
subprocess over the same JSON-lines `InboundEvent → Delta` protocol, which is
exactly what `HarnessAdapter.dispatch` returns.

---

## 8. Phasing

**Slice 1 — minimal shippable (one foreign, read-only, visible + messageable):**
1. `HarnessAdapter` Protocol + `AgentType` descriptor in `arcteam/harness/`.
2. `Entity.harness: str = "arcagent"` field + `EntityStatus.revoked/suspended`
   (`arcteam/types.py`).
3. `EnrollmentGrant` primitive + `verify_enrollment` in arctrust; `arc team
   register --harness` writes a pending row; `arc approve` signs the grant;
   registry admission verifies (§3.4 chokepoint 1).
4. `ArcAgentHarness` wrapping the existing `ArcAgent` (native becomes an impl).
5. arcui: `harness` badge on the roster card; roster served from the registry so
   a foreign member appears; runs/traces already render by DID.
6. One reference foreign adapter (a read-only "hermes" that can be `@mentioned`,
   `deliver`s a reply, and shows in the roster) — proves the seam end-to-end.
   Memory: shared-read only via `TeamMemoryService`; no private-memory port yet.
   **Runs out-of-process** (mandatory, §9) even in this first slice — the subprocess
   posture is not deferred, because the memory-isolation argument (§5) depends on it.
7. **Enrollment abuse battery (moved up from Slice 2 — the admission chokepoint
   ships here, so per the project rule it is incomplete without its abuse cases).**
   Add to `scripts/run_adversarial_tests.py`: forged grant signature, self-supplied
   approver key (grant verified against a key the member supplies rather than the
   trust-store operator key), replayed enrollment (nonce/duplicate-DID), tampered
   `Entity.public_key` after signing (TOCTOU), and an un-enrolled-member dispatch
   attempt. All must fail closed.

Slice 1 gives: a foreign type **enrolled (signed) + visible (badged) +
messageable + audited**, with the two other chokepoints (eligibility, dispatch)
wired and fail-closed.

**Slice 2 — full member:** private `MemoryPort` with the DID gate; capability-gated
detail tabs; workflow participation (a foreign member as a workflow node owner);
revocation CLI + trust-store removal; `source_harness` provenance + quarantine on
shared memory (§5.1); contract tests (start-with-component-absent). The
out-of-process sandbox and the enrollment abuse battery are **not** here — they
ship in Slice 1.

**Slice 3 — vision:** multiple foreign harnesses (openclaw, LangGraph, CrewAI) as
folder-scanned extensions; foreign members as full workflow/DAG participants and
relevance-triage responders; federal-tier microVM sandbox.

---

## 9. Deployment posture — two threat models (pick per member)

"Spawned under Arc supervision" and "attach as remote peer" are **different threat
models** and get **separate stories**. The **deployment** (operator config) chooses
the posture per member — it is a transport/lifecycle fact, not a trust claim the
extension folder makes about itself (trust stays pinned in fleet code, §2.1).

**Posture A — spawned under Arc supervision (RECOMMENDED default).** Arc starts and
owns the member process, exactly as the always-on fleet starts native agents
(`arcgateway/fleet.py:1-19`) and the subprocess worker runs one isolated session
(`arccli/agent_worker.py`: own event loop, own connection pool, own audit chain;
resource limits via `SubprocessExecutor.preexec_fn`). A foreign harness runs in
that subprocess (microVM at federal, ASI05). Threat model: the code is untrusted
but the **lifecycle, filesystem fence, and resource limits are Arc's** — blast
radius is one sandboxed member. This is the posture for a bundled hermes/openclaw
adapter shipped as an extension.

**Posture B — attach as remote peer.** An already-running foreign agent, elsewhere
on the network, connects to the fleet (the way arctui attaches over `/ws/chat`,
memory pointer `project_arctui_spec058`). Different threat model: Arc controls
**neither its lifecycle nor its host**, so it must be treated as a fully external,
mutually-authenticated peer — mTLS on the NATS/WS boundary (`threat-surface.md`
ASI07), enrollment signature verified on connect, replay/nonce on every message
(`arcteam/types.py:120-152`), and **no in-process trust ever** (a remote peer can
never be `sandbox = in_process`). It cannot hold a native workspace path; its
memory access is shared-only via `TeamMemoryService` unless it presents a signed
private-memory port token bound to its DID. Revocation must also **hard-drop the
connection** (close the stream + remove the trust-store key), not merely flip a
status flag, because Arc cannot kill a process it does not own.

The two postures share the enrollment, signing, policy, and audit stories (§3, §6,
§10); they differ only in lifecycle ownership and transport trust. Slice 1 ships
Posture A; Posture B is a Slice 3 item and must not be assumed by Slice-1 code.

## 10. Enforcement & audit run ARC-SIDE (never trust the foreign runtime)

Restating §6 as a hard invariant for the builder: **a foreign runtime never
self-enforces and never self-audits.** Every authorization decision and every
audit record for a cross-type interaction is produced by arc code at the arcteam
boundary:

- **Authorize:** the `PolicyPipeline` runs arc-side on every seam a member crosses
  (memory `arcmemory/tools.py:159-176`; message send is signed + audited in
  `arcteam/agent_fleet.py:194-212`; any arc tool via `sign_call` → pipeline). An
  unsupported capability is a fail-closed DENY (§2.1 degraded results). A foreign
  member's *self-report* that it "checked policy" is never accepted.
- **Audit:** every cross-type interaction is emitted arc-side into the WORM chain
  through the single arctrust `emit` point (`arctrust/audit.py:518` `emit`,
  `:174` `WormSink`), signed by the operator authority
  (`arcteam/agent_fleet.py:198-205`). The chain is externally verifiable
  (`arctrust/audit.py:432` durable-chain validation). Foreign runtimes can neither
  forge nor omit these records because they never hold the emission point or the
  operator key.

### 10.1 `audit_mode` — a tier dial on the grant (ADR-019 stringency)

How much of a foreign harness's **internal** activity Arc must see is a per-member
stringency dial carried on the `EnrollmentGrant` (`audit_mode`, §3.2), not a
one-size rule:

- **`boundary` (personal / enterprise, accepted default).** Arc audits every
  interaction that crosses an arc seam (§10); a foreign harness's purely-internal
  tool calls are outside arc's audit boundary — a **stated, accepted trust
  boundary** at these tiers.
- **`full` (REQUIRED at federal).** Admission at federal tier requires
  `audit_mode="full"`: the harness must stream its internal tool-call audit into
  arc's WORM chain, or attest WORM-equivalent emission. An **opaque harness that
  cannot comply is not admissible at federal** — declared explicitly, the same
  principle as the non-indexable-source rule (a source that cannot meet the bar
  must say so, never silently opt out). This is tier-as-stringency-dial per
  ADR-019: the pillars are universal, the *stringency* is the dial.

The registry admission check (§3.4) enforces this: at federal tier, a grant whose
`audit_mode != "full"` fails admission fail-closed.

## 11. What does NOT change (guardrails for the builder)

- **arcagent internals are untouched.** `arcagent.toml`, the DID-required
  `ArcAgent.__init__` (`arcagent/core/agent.py:124`), the memory `state()` guard
  (`arcagent/modules/memory/_runtime.py:179`), and REQ-030 all stay. Native agents
  gain a thin `ArcAgentHarness` wrapper (§2.2) and nothing else.
- **Dependency direction is preserved.** The fleet direction stays
  `arcteam → {arcagent, arcmemory}` (memory pointer `project_arcteam_comms_direction`;
  `arcteam/CLAUDE.md` "Not arcagent"). The `HarnessAdapter` Protocol lives in
  arcteam and speaks only arcteam/arctrust types — **no `arcagent` import in
  arcteam**, enforced by the existing layering tests. `ArcAgentHarness` (the native
  impl) lives in arcgateway, which already legally imports arcagent.
- **No upward imports; no new reverse edges.** arcmemory still must never import
  arcagent (`arcmemory` architecture test `test_no_arcagent_import.py`); arctrust
  stays a leaf. `EnrollmentGrant` is an arctrust primitive (leaf-safe — pure crypto
  + pydantic), not an arcteam or arcagent type.
- **The seam is deletable.** Removing every foreign `AgentType` folder must leave
  the native fleet, roster, messaging, and UI fully working — the same
  start-with-component-absent guarantee the adapter registry already provides.

## 12. Planner review-criteria crosswalk

| # | Criterion | Answered in |
|---|-----------|-------------|
| 1 | Enrollment ceremony (DID+keypair, operator signs, record location, revocation) | §3.1–§3.5 |
| 2 | **Key custody fork** (native-sign vs proxy; confused-deputy; child-DID binding) | §3.6 |
| 3 | Enforcement arc-side, fail-closed for unsupported capability | §2.1 (degraded), §6, §10 |
| 4 | Memory = untrusted input (classification stamp, no-write-down, provenance/quarantine) | §5.1 |
| 5 | Type model (folder-is-a-platform SPEC-065, degraded results, UI degraded-detail) | §2.1, §2.3, §4 |
| 6 | Audit at the boundary (arc-side WORM) | §10 |
| 7 | Deployment posture (spawned vs remote peer — separate stories) | §9 |
| 8 | What does not change (arcagent internals, direction, no upward imports) | §11 |

## 13. Resolved questions (Planner rulings)

All five original open questions were ruled on in the Planner's design pass;
recorded here so the builder inherits the decisions, not the debate.

1. **Enrollment signing key — RULED: same operator key.** One operator authority
   (`OperatorApprovalAuthority`). A second "fleet key" would be a second root of
   trust with its own custody/revocation story — pure added attack surface.
   Federal/HSM is **configuration, not design**: enrollment signing goes through
   the existing arctrust `Signer` seam (in-process / vault-transit,
   `arctrust/signer.py`), so HSM custody is a config choice on the same seam.
2. **Foreign in-process vs out-of-process — RULED: out-of-process, MANDATORY, all
   tiers, no exceptions.** Not defense-in-depth — it is what makes §5 sound: a
   foreign harness in-process could just call `build_brain` itself (the exact
   bypass §5.2 documents). "Trusted first-party foreign harness" is a contradiction
   under ASI04 — anything genuinely first-party and trusted ships as a **native
   module through arcbundle signing**, not as a foreign type. MicroVM at federal.
   (Folded into §2.1, §9, §8-Slice-1.)
3. **Memory access — RULED: `MemoryPort` only, ABSOLUTE.** Same coupling as (2). The
   unguarded `build_brain` factory is filed as its **own** hardening item,
   independent of H-040 (§5.2): arcmemory should eventually bind `workspace ↔
   agent_did` internally as defense-in-depth, not rely on caller honesty.
4. **Federal internal visibility — RULED: a tier dial on the grant.**
   `EnrollmentGrant.audit_mode: "boundary" | "full"` (§3.2, §10.1). Personal/
   enterprise accept `boundary`; federal admission **requires** `full` (stream
   internal audit or attest WORM-equivalent), and an opaque harness that cannot
   comply is inadmissible at federal — declared explicitly. Tier-as-stringency per
   ADR-019.
5. **Roster source — RULED: `EntityRegistry` is the single source.** arcui reads it
   via arcteam (legal edge). Two constraints, both folded into §4: native agents
   keep their rich `arcagent.toml`-driven tabs via the capability route, and the
   registry path **replaces** the filesystem scan — it must **not** add a new direct
   `team/` read in arcui (SPEC-022 stays intact).

### Note — EnrollmentGrant deliberately has NO expiry

Recorded so a future reviewer does not "add a TTL for safety". Revocation (§3.5) is
the sole invalidation mechanism. A time-bomb on the grant reproduces the
mapping-approval-expiry incident, where a valid approval silently lapsed on a 24h
timer and every connector reverted to awaiting-approval daily. A grant is valid
until explicitly revoked — never on a clock.

### Remaining for Josh (not blocking Slice 1)

- **Posture B (attach-as-remote-peer) transport specifics** — the mTLS/enrollment
  handshake for an external peer (§9) is a Slice 3 concern; the exact connect-time
  ceremony is unspecified and should be its own design pass when Slice 3 is scoped.
