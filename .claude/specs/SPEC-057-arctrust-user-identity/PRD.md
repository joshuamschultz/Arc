# PRD — SPEC-057: arctrust User Identity, Pairing & the Context-Resolved Trifecta

## 1. Problem

The lethal-trifecta gate (SPEC-035) is correct in principle but unusable as deployed:

1. **The 300s freeze.** When a session accumulates `private_data + external_comms + untrusted_input`, `GlobalLayer` denies and `HumanGate` awaits an operator approval for up to `timeout_seconds = 300`, **synchronously blocking the agent turn**. On the fleet, live spool evidence shows `ls`/`read` calls each blocking exactly 300.00s, then the model routing around the gate via `bash` (untagged) — burning turns and wall-clock.
2. **Approvals are ungrantable.** The pending-approval store exists in code but is not wired on deployed agents (`find ~/.arc -iname "*approv*"` → nothing; `auto_approve = []` everywhere), so every gate can only time out.
3. **The owner exemption is brittle.** "Talking to the owner isn't exfiltration" is implemented as a hardcoded `OWNER_CHANNEL = "user://operator"` string match plus a fixed tool list. It has no idea that *this Telegram chat* or *this Slack user* is actually the operator.
4. **No first-class user.** arctrust models agent DIDs but not the *human* behind arcui / Telegram / Slack, and browser access is gated by a token-in-URL scheme that is both weak and clunky.

The result: agents that read local files, search the web, and message people — i.e. do their job — freeze or fail.

## 2. Goals

- **G1.** The trifecta continues to fire on genuine exfiltration risk. **No bypass to make workflows convenient.**
- **G2.** Each trifecta leg is a *context-resolved definition* evaluated per call, matching its real-world condition (see §4).
- **G3.** A first-class arctrust **user identity**, with **pairing** of external surfaces (Slack user, Telegram user, email) to a workspace.
- **G4.** Owner-scoped egress: comms with the paired owner over any surface never count as `external_comms`.
- **G5.** Per-tier URL **trust** policy drives `untrusted_input` (personal trusts open web via denylist; federal trusts only an allowlist).
- **G6.** Approvals reachable and operator-signed via **arcui, arccli, and the owner's paired channel**; retire the token-URL auth.
- **G7.** On approval timeout, the **run ends cleanly and fires a completion** so the dispatch queue advances — no wedged serial queue.

## 3. Non-Goals

- **NG1.** Weakening or disabling the trifecta for any tier. Tier changes *definitions of the legs*, never *whether the gate runs*.
- **NG2.** "Paired owner ⇒ allow anything." Pairing scopes exactly two things: owner comms are not egress, and the owner may sign approvals. It does **not** exempt owner-initiated actions from policy.
- **NG3.** Full external SSO/IdP federation (future). This spec covers local user registration + surface pairing.

## 4. The context-resolved trifecta (core)

### 4.1 `private_data`
Reading **anything on the machine** contributes the leg: workspace files, `.env`, tomls, traces, agent files, and the private stores (memory, profile, recall). "Private" = private from the outside world, not from the operator. No exemptions.

- **REQ-001** — Any tool that reads on-machine data carries the `private_data` leg. (Today: `file_read`, `memory`, `user_profile`, `recall`. Confirm no local-read path is untagged.)
- **REQ-002** — Review the `subprocess`→`untrusted_input` mapping: a `bash cat <localfile>` reads *private* local data, not untrusted content. Bash that reads local files should contribute `private_data`; bash that fetches external content contributes `untrusted_input`. Resolve the current mislabel so bash is neither a bypass of `private_data` nor a spurious `untrusted_input`.

### 4.2 `external_comms` — non-owner only
Outbound communication contributes the leg **only when the counterparty is not the paired owner.**

- **REQ-010** — `external_comms` is resolved against the recipient identity: paired-owner recipient ⇒ no leg; any non-owner (or mixed) recipient ⇒ leg. Applies across messaging, Telegram, Slack, and any egress tool that names a destination.
- **REQ-011** — Owner resolution uses the SPEC-057 identity/pairing registry, not a hardcoded `user://operator` string. A paired Telegram/Slack/email identity resolves to the workspace owner.
- **REQ-012** — A web request to a provider/site is a non-owner destination ⇒ `external_comms`. (See open question OQ-1 on `web_extract`.)

### 4.3 `untrusted_input` — unvetted only, tier-defined
Ingesting content contributes the leg **only when the source is not operator-vetted.** An allowlisted/trusted URL is not untrusted.

- **REQ-020** — The `untrusted_input` leg for a fetch is resolved against a URL **trust verdict**, not a static tag.
- **REQ-021** — **Personal**: trusted-by-default. Support a **denylist** in the web/browser module; a fetched URL is untrusted only if denylisted (and denylisted URLs are also blocked from reachability). Net: personal web reads usually carry no `untrusted_input`.
- **REQ-022** — **Federal**: untrusted-by-default. Only allowlisted URLs are trusted; every other fetch carries `untrusted_input`. (Reachability stays deny-by-default — SPEC-018 startup rejects an empty federal allowlist.)
- **REQ-023** — Enterprise: allow-by-default reachability; trust default per operator policy (denylist-style, defaulting to trusted, is acceptable).

### 4.4 Resolution mechanism
- **REQ-030** — Leg resolution is centralized in a `legs_for_call`-style hook that receives the call's arguments (recipient, URL) **and** the tier trust policy, mirroring the existing owner-channel exemption. Static `TAG_TO_LEGS` remains the base; the hook subtracts/adds legs from the real call context.

## 5. User identity & pairing

- **REQ-040** — arctrust gains a **User** identity (distinct from an agent DID) with its own keypair/credential, registrable from arcui and arcgateway.
- **REQ-041** — A user **pairs** external surface identities to a workspace: Slack user id, Telegram user id, email. The pairing is signed and stored in the trust store.
- **REQ-042** — The policy engine resolves "is counterparty X the paired owner of workspace W?" from this registry (feeds REQ-011).
- **REQ-043** — Login/registration replaces the browser **token-in-URL** auth (retire it; G6). arcui/arccli authenticate the user; the authenticated user is the approval signer.

## 6. Approvals

- **REQ-050** — A blocked trifecta call writes a **pending approval** (SPEC-035 store) that surfaces on **arcui**, **arccli** (`arc approve`), **and the owner's paired channel** (approve from Telegram/Slack DM).
- **REQ-051** — Approvals are **operator/user-signed** (never the agent DID) and pinned to the deployment owner identity (SPEC-053 authority).
- **REQ-052** — The pending row and audit event carry enough context to triage: the tool, its arguments (redacted preview), the completed leg set with **provenance** (which prior calls lit each leg + when), and the session id. *(Folds in the earlier "make blocks approvable" work.)*
- **REQ-053** — Personal/enterprise may configure `auto_approve` for named low-risk compositions; federal never auto-approves.

## 7. Timeout → end-run → complete

- **REQ-060** — The gate may remain blocking, but on approval **timeout** the run **terminates and emits a completion/termination event** carrying the reason ("approval not granted: <composition>") so the SPEC-056 serial dispatch queue advances and the next task runs. A stuck approval must never wedge the queue.
- **REQ-061** — Termination is attributed and audited (which operator, or "timeout"), consistent with the cancel/kill-switch work (caller_did on terminate).

## 8. Open questions

- **OQ-1** — `web_extract` fetches an agent-chosen URL, which can encode data in the URL to a non-owner. Keep it `untrusted_input`-shaped (a read) and rely on URL trust, or also carry `external_comms` (a real outbound push)? Resolve so the shipped web-leg approximation (currently `untrusted_input` only) is corrected to the true model (`external_comms` + tier-conditional `untrusted_input`).
- **OQ-2** — Ledger lifetime: the session capability ledger never resets (`reset()` has no callers), so a long-lived agent saturates. Define the reset boundary (turn / compaction / never) consistent with the exfil window. With owner-scoped egress + tier trust the pressure drops, but this remains a real correctness item.
- **OQ-3** — Enterprise trust default (REQ-023) — trusted-by-default like personal, or untrusted like federal?

## 9. Phasing

- **P0 (shipped, temporary):** web leg reclassified to `untrusted_input`-only as a stopgap so personal research works today. Corrected by OQ-1.
- **P1:** URL trust policy (denylist mode + trust verdict) → `untrusted_input` resolution (REQ-020–023, REQ-030).
- **P2:** User identity + pairing (REQ-040–043) → owner-scoped egress resolution (REQ-010–012); retire token-URL.
- **P3:** Approvals across all three surfaces + provenance-rich pending rows (REQ-050–053).
- **P4:** Timeout → end-run → complete (REQ-060–061); ledger-reset decision (OQ-2).

## 10. Success criteria

- A personal agent can read local files, search the open web, and write output **without ever hitting the trifecta gate** (research-and-write is 2 legs, never 3).
- A personal agent that ingests **denylisted** content, holds private data, and messages a **non-owner** **does** hit the gate and gets an approval the operator can grant from arcui/arccli/paired channel within the window.
- A federal agent hits the gate on any unvetted fetch + private data + non-owner egress.
- No run wedges the dispatch queue: an un-approved gate ends the run and the next task dispatches.
- Every gate decision and approval/termination is attributed and in the tamper-evident audit chain (and visible on the arcui Security screen once its ingest path is fixed).
