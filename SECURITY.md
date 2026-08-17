# Security Policy

Arc is a security-first agent stack. This document describes what is
actually enforced today, how to report a vulnerability, and what is out
of scope for a report.

## Supported Versions

Arc is pre-1.0 (root package version `0.2.0`; individual packages under
`packages/*` version independently, e.g. `arcllm` 0.7.0, `arctrust` 0.9.0,
`arcagent` 0.16.0). There is one git tag (`0.2.0`) and no formal
support-window policy yet. Until a 1.0 release and a documented support
matrix exist, treat the tip of `main` as the only supported version and
assume no back-port of fixes to older tags.

## Reporting a Vulnerability

**Do not open a public GitHub issue for a security report.** This repository
is currently private; if and when it is made public, a vulnerability must
still be reported privately, never in a public issue, discussion, or PR.

TODO(security-contact): the maintainer must fill in a reporting address
(a monitored email, or a GitHub Security Advisory contact) before this
repository is made public. No such contact currently exists in this
codebase or its docs.

Until that contact is set, use GitHub's private vulnerability reporting
(Security → Report a vulnerability) on this repository if you have access,
or contact the maintainer directly through an existing private channel.

What to include in a report:

- Affected package(s) and file/function.
- Steps to reproduce, or a minimal proof of concept.
- Impact (what an attacker gains: data read, privilege escalation, code
  execution, audit-trail tampering, etc.).
- Whether the issue requires a specific tier (personal / enterprise /
  federal) to reproduce.

Expected handling: there is no published SLA yet. Reports should expect
acknowledgment before any public disclosure, and should not be disclosed
publicly until a fix is available or the maintainer agrees to a disclosure
date.

## Security Model

Arc enforces four pillars on every tool call, at every deployment tier.
Tier changes stringency, not whether a pillar runs (see "Tier Model" below).

### 1. Identity

Every agent has a DID derived from an Ed25519 keypair (`did:arc:{org}:{type}/{hash}`,
where `hash` is the first 8 hex chars of `sha256(public_key)`). `ArcAgent.__init__`
requires an identity — there is no code path that runs without one. Key
files must be `0600`; loading a group- or world-readable key file is a
hard error. Primitives live in `packages/arctrust/src/arctrust/identity.py`
and `keypair.py`.

Child (spawned sub-agent) identities are derived via HKDF-SHA256 over the
parent's seed and a per-spawn nonce, and clearance narrows monotonically —
a child can never out-clear its parent.

### 2. Sign

Every loaded artifact (skill, extension, sandbox backend, agent-authored
tool) is verified at the moment it is loaded, not only at install time.
A signature is a detached `.arcsig` sidecar containing a SHA-256 content
digest, the signer's DID, public key, and signature. `verify_artifact`
re-checks digest, signature, and (when pinned) key identity on every load.

A valid signature proves integrity and attribution (these bytes are
unmodified and came from this key). It does **not** prove authorization —
an agent can sign its own malicious code with its own key. Authorization
is a separate step: an operator-approved pin (see Trust-On-First-Use
below).

Signing defaults to Ed25519. At the federal tier, signing is forced to
ECDSA-P256 (see FIPS, below) because Ed25519 (PyNaCl/libsodium) has no
CMVP validation path.

### 3. Authorize

Every tool call passes through `arctrust.policy.PolicyPipeline`, a
first-DENY-wins pipeline. If any layer raises an exception, the call is
denied — there is no fail-open path. Layer count is tier-dependent (see
Tier Model). The layers, when present: Identity, Global (tenant denylist
+ forbidden-composition check), Classification (Bell-LaPadula no-read-up),
Provider (LLM token/cost budget), Agent (per-agent tool allowlist), Team
(federal-only delegation scope), Sandbox (isolation-level floor).

**Lethal Trifecta gate.** Private-data read + external communication +
untrusted input, accumulated within a session, is treated as an
exfiltration primitive and requires human approval before the call that
would complete the set is allowed. This is enforced today, but only on
tools that are actually tagged with these capability legs — a new
network-facing tool must be tagged before the gate covers it.

### 4. Audit

Every security-relevant action is emitted through one function:
`arctrust.audit.emit(event, sink)`. The durable sink (`WormSink`) appends
`{seq, event, prev_hash, event_hash, algorithm, signature}` records to an
append-only, `0600`, single-writer (exclusive `flock`) file. `verify_chain()`
walks the file and checks hash links, signatures, and sequence
contiguity, so tampering with an entry is detectable. An external
`WitnessAnchor` can submit the operator-signed checkpoint head to a
second, separately-custodied medium, so a forger holding the operator key
alone cannot retroactively erase a checkpoint from a log they don't own.

### Sandboxed code execution

The built-in `execute_python` tool routes through a tier-selected
isolation backend: Firecracker microVM at federal (refuses to run, never
downgrades, if `/dev/kvm` is unavailable), Docker container at
enterprise and personal-default, and a stripped local subprocess only at
personal tier with an explicit opt-in (`relax_isolation`). Every backend
selection is audited before the first line of agent code runs.

A newer, separate execution path — a restricted-Python-subset script
interpreter for model-authored orchestration scripts (`arcrun`'s
`dynamic` strategy) — is under active development and not yet stable
enough to document in detail here. As a design property worth stating:
a script written by the model is parsed against a whitelisted grammar and
dry-run before it is allowed to execute, not executed directly from raw
model output.

### Dynamic tool safety (agent-authored code)

Code an agent writes itself for its own tools passes an AST validator
before it can load, independent of tier: dynamic execution calls
(`eval`, `exec`, `compile`, `__import__`), interpreter-frame traversal,
assignment to interpreter internals, and non-UTF-8 source encodings are
rejected at every tier, including personal. What varies by tier is which
*module imports* are additionally allowed (personal: all; enterprise: a
blocklist of privileged groups — filesystem, process/exec, interpreter,
network; federal: an allowlist, deny-by-default).

## Tier Model

Tier is stringency metadata, not a separate code path. The same pillars
run at every tier; the tier only tightens each one.

| Knob | Personal | Enterprise | Federal |
|---|---|---|---|
| Trusted signers | self-signed OK + audit warn | operator-chain | operator-chain (FIPS) |
| Policy layers | Identity + Global (2) | + Classification, Provider, Agent, Sandbox (6) | + Team (7, all layers) |
| Dynamic tool creation | allowed | approval gate | denied |
| Signing algorithm | Ed25519 | Ed25519 | ECDSA-P256 |
| Trust-on-first-use | signed → allow; unsigned needs an explicit opt-in | name+hash pin; unknown name needs operator approval | signature required AND operator approval |
| Import policy for agent-authored tools | allow-all | blocklist (4 privileged groups) | allowlist, deny-by-default |
| Code execution sandbox | Docker (default), local subprocess (explicit opt-in) | Docker | Firecracker microVM, refuses to downgrade |

### FIPS

At federal, `require_fips=true` arms a fail-closed startup check
(`packages/arctrust/src/arctrust/fips.py`): a protected crypto function is
refused unless the loaded OpenSSL provider is itself FIPS-140-3-validated
*and* the algorithm is one of the two FIPS-approved algorithms Arc
supports (`ecdsa-p256`, `aes-256-gcm`). This is a real, exercised code
path — but it only actually enforces FIPS mode when the deployment links
a system OpenSSL build with a CMVP-validated FIPS provider. The default
PyPI-wheel OpenSSL used in most development environments is never CMVP
validated, so the gate will correctly refuse to claim FIPS compliance in
that environment. Do not treat "the FIPS code path exists" as "this
deployment is FIPS-validated" — that depends on the OpenSSL build in use.

## Real Security Tooling (verified against current CI config)

- `pip-audit` runs as a blocking CI job (`.github/workflows/ci.yml`,
  job `security`), scoped to the exported runtime dependency closure only
  (dev/test/tutorial groups are excluded). Any new, unsuppressed
  vulnerability in that closure fails the build.
- Documented, accepted exceptions live in `sbom/security-suppressions.txt`,
  each with a compensating-control note.
- A CycloneDX SBOM (`sbom/arc-python.cdx.json`) is regenerated and
  uploaded as a CI artifact on every run.
- `ruff` runs with the `S` (flake8-bandit) rule set enabled
  (`pyproject.toml` `[tool.ruff.lint] select`), which is Arc's static
  security-lint mechanism. There is no separate bandit invocation.
- Known transitive CVEs are floored directly in `pyproject.toml`
  (`[tool.uv] constraint-dependencies`), each pinned line citing a
  `PYSEC-`/`CVE-` identifier.
- `mypy --strict` is run per-package in CI as a correctness gate, not a
  security gate in itself, but strict typing at data boundaries (Pydantic
  models) reduces a class of input-handling bugs.

What does **not** currently exist, so it is not claimed above: a
Dependabot configuration, a pre-commit hook running these checks locally,
a published FedRAMP or CMMC control-mapping document (only NIST 800-53
and OWASP LLM/Agentic mappings exist, under `docs/runbooks/security/`),
and a security-reporting contact address (see TODO above).

## Out of Scope for a Vulnerability Report

- Findings that require you to have already compromised the operator's
  private signing key, the OS, or the underlying host — Arc's trust model
  assumes host and operator-key integrity as a precondition, not a
  guarantee it provides.
- Denial of service against your own local deployment via resource
  exhaustion you deliberately trigger (e.g., an unbounded agent loop you
  wrote yourself with no budget cap configured) — configure the
  documented Provider/budget policy layer instead.
- Reports about the `personal` tier's `relax_isolation` opt-out running
  agent code with full host access — this is a documented, explicit
  opt-in, not a vulnerability.
- Missing features from the tier/compliance mapping docs that are
  explicitly marked as not yet implemented.
- Social-engineering or physical-access scenarios against a specific
  deployment, rather than the Arc codebase itself.

If you are unsure whether something is in scope, report it anyway through
the private channel above rather than guessing.
