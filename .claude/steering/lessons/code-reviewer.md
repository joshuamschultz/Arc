
## Check read paths for confinement, not just writes (SEC-04)
When a diff adds signed/confined WRITE paths, verify the matching READ paths confine identically. A resolver that joins caller-supplied `package`/`name` into `context/<name>.md` without rejecting `..`/separators is an arbitrary-file-read primitive — especially from CLI argv (unrestricted) vs URL segments (partly constrained). Flag the library chokepoint, not each call site.
## Orchestration entry points need a real integration test (2026-07-26)

New orchestration entry points need at least one integration test that calls the REAL top-level function — not just its decomposed helpers with injected fakes. In the arctui/blueprints review, `ensure_gateway`'s probe/spawn/sleep callables and `_build_transport`'s sub-pieces were well unit-tested, but the real subprocess-spawn + network-probe wiring stayed unexercised — the producers-unwired pattern ([[feedback_producers_unwired_pattern]]). Flag a diff that adds a top-level async orchestrator whose only tests inject doubles for every side-effecting dependency.

## Verify the trust-ESTABLISHMENT path before rating a privilege finding blocking
*(SPEC-066 /review, 2026-08-13 — downgraded finding)*

A security lens correctly observed that VERIFIED module capabilities run uncontained, and
rated it blocking on the reasoning that a third-party issuer gains full in-process
privilege. The observation was right; the severity was not. Checking HOW an issuer becomes
trusted showed `_trusted_issuers()` reads only the on-box operator key plus a manual entry
in `~/.arc/trust/issuers.toml`, and `grep save_issuer|add_issuer|write_issuer` across every
package returned nothing — no code path writes that file, and absence is refusal. The grant
is a deliberate out-of-band act, the same model as apt/npm/pip.

**How to apply:** a privilege finding has two halves — what the privilege permits, and how
it is obtained. Trace the acquisition path (who writes the trust store, is it automatic, is
absence fail-open or fail-closed) before assigning severity. A real privilege reached only
through an explicit manual grant is a documentation gap; the same privilege reached
silently is a vulnerability.
