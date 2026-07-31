
## Check read paths for confinement, not just writes (SEC-04)
When a diff adds signed/confined WRITE paths, verify the matching READ paths confine identically. A resolver that joins caller-supplied `package`/`name` into `context/<name>.md` without rejecting `..`/separators is an arbitrary-file-read primitive — especially from CLI argv (unrestricted) vs URL segments (partly constrained). Flag the library chokepoint, not each call site.
## Orchestration entry points need a real integration test (2026-07-26)

New orchestration entry points need at least one integration test that calls the REAL top-level function — not just its decomposed helpers with injected fakes. In the arctui/blueprints review, `ensure_gateway`'s probe/spawn/sleep callables and `_build_transport`'s sub-pieces were well unit-tested, but the real subprocess-spawn + network-probe wiring stayed unexercised — the producers-unwired pattern ([[feedback_producers_unwired_pattern]]). Flag a diff that adds a top-level async orchestrator whose only tests inject doubles for every side-effecting dependency.
