
## Read-path confinement is a library concern (SEC-04, editable-system-prompts)
A resolver/loader that joins caller-supplied identifiers into a filesystem path MUST validate them at the library chokepoint — reject path separators, NUL, and bare `.`/`..` components — never trust the caller to confine. Write paths had `_confine`; the read path (`resolver.resolve`, `catalog.load_stock_document`) did not, giving `arc prompt show <pkg> ../../../etc/x` an arbitrary `.md` read. Guard where the path is built, once, so every caller (route, CLI argv, agent) is covered.

## One signed-write envelope, many editors (editable-system-prompts)
When adding a new editor over signed artifacts (prose prompt, structured rubric, …), produce a body string and route it through the single write helper (`_author_signed_overlay`) that owns secret-scan → policy → confine → sign → audit. Never re-derive that sequence per surface — new surface = new body producer only.

## Decoupled consumer takes a handed-in resolver, never imports the manager (arcskill↔arcprompt)
A package that must work standalone must not import the optional manager. It takes an optional `resolve` callable it is *handed* by the integrator (arcagent), using it when present and falling back to its own stock read when None. Keeps "no manager → still works; manager present → it manages" true by construction.
