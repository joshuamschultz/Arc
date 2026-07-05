"""Shared regex-scan cost guard (LLM10 unbounded consumption).

Three independent scan sites — ``GuardrailsModule`` (operator-supplied
allow/deny regex), ``RegexPiiDetector`` (PII/secret patterns), and
``InjectionModule`` (prompt-injection pattern/semantic corpora) — each cap
the text they run regexes against so a single huge message can't consume
unbounded CPU synchronously on the event loop. All three converged on the
same cap; it lives here once rather than as three independent literals.

This bounds total scan cost, not just catastrophic-backtracking blowup: a
huge input scanned by many patterns in a single synchronous pass is itself
a worker-stalling cost even when every individual pattern is linear-time.
Capping the scanned prefix means an attacker cannot grow compute merely by
growing the message past this length — it does NOT eliminate blowup from a
pathological pattern matched against content *within* the capped window.
"""

MAX_REGEX_SCAN_LENGTH = 4000
