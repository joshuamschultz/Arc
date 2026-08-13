
## Read-path confinement is a library concern (SEC-04, editable-system-prompts)
A resolver/loader that joins caller-supplied identifiers into a filesystem path MUST validate them at the library chokepoint — reject path separators, NUL, and bare `.`/`..` components — never trust the caller to confine. Write paths had `_confine`; the read path (`resolver.resolve`, `catalog.load_stock_document`) did not, giving `arc prompt show <pkg> ../../../etc/x` an arbitrary `.md` read. Guard where the path is built, once, so every caller (route, CLI argv, agent) is covered.

## One signed-write envelope, many editors (editable-system-prompts)
When adding a new editor over signed artifacts (prose prompt, structured rubric, …), produce a body string and route it through the single write helper (`_author_signed_overlay`) that owns secret-scan → policy → confine → sign → audit. Never re-derive that sequence per surface — new surface = new body producer only.

## Decoupled consumer takes a handed-in resolver, never imports the manager (arcskill↔arcprompt)
A package that must work standalone must not import the optional manager. It takes an optional `resolve` callable it is *handed* by the integrator (arcagent), using it when present and falling back to its own stock read when None. Keeps "no manager → still works; manager present → it manages" true by construction.

## A requirement is not implemented until something CONSUMES it
*(SPEC-066 /review, 2026-08-13 — blocking finding)*

`arcbundle.capability_copy` was written, documented, and covered to 92%. `arc module
install` called it. Nothing ever loaded what it wrote — `grep` for
`capability_dir|MODULE_COPIES_DIR|copy_capabilities` over `packages/arcagent/src/` returned
ZERO hits. REQ-337 (Must) was unmet while every test passed, because the tests exercised
the producer and no test asked whether a consumer existed. Worse, the module's docstring
asserted an isolation property that therefore did not hold, which an operator could have
relied on.

**How to apply:** before marking a task done, grep for a CALLER of the thing you built,
outside its own tests. If the only callers are tests and the producer itself, the feature is
dead code no matter how green the suite is. This repo has now shipped the "correct producer,
dead activating wiring" pattern more than once.

## Do not claim a control works in a commit message unless you watched a test execute it
*(SPEC-066 /review, 2026-08-13)*

A commit message described the verifier as refusing "payload files the manifest never
declared" as a security property. No test in the repository referenced that refusal — the
control had never run. The claim was true of the code and false of the evidence.

**How to apply:** a security property named in a commit message, README, or docstring needs
a test whose failure would disprove it. If you cannot point at that test, describe what the
code intends, not what the system guarantees.
