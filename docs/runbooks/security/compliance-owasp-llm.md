# OWASP Top 10 for LLM Applications — control mapping

> **Runbooks**  ·  Operate  ·  page 18 of 19  
> **For** Operators deploying and running Arc  
> [← NIST 800-53 mapping](compliance-nist-800-53.md)  ·  [Docs home](../../README.md)  ·  [OWASP Agentic mapping →](compliance-owasp-agentic.md)

Arc's controls for the OWASP Top 10 for LLM Applications (2025), with the code
that implements each one and the command that proves it.

**How to read this.** *Control* is what Arc does. *Where it lives* is the module
that owns it — if you change security behaviour, change it there and nowhere
else. *Verify* is how an auditor confirms the control is live in a given
deployment.

```mermaid
flowchart LR
    classDef inb fill:#D6E6FF,stroke:#0073FE,color:#002550
    classDef ctl fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef out fill:#002550,stroke:#001A38,color:#FFFFFF

    IN["Untrusted input<br/>user · web · tool output"]:::inb
    INJ["Injection scan<br/>arcllm injection module"]:::ctl
    POL["PolicyPipeline<br/>first-DENY-wins, fail-closed"]:::ctl
    GATE["Human approval gate<br/>trifecta legs resolved"]:::ctl
    GUARD["Output guardrails<br/>arcllm guardrails module"]:::ctl
    AUD["Signed WORM audit chain"]:::out

    IN --> INJ --> POL --> GATE --> GUARD --> AUD
```

---

## The mapping

| ID | Risk | Control in Arc | Where it lives | Verify |
|---|---|---|---|---|
| **LLM01** | Prompt injection | Untrusted content is never promoted to instructions. The prompt is assembled in fixed, boundary-marked sections; a scanning module inspects inbound content; the trifecta gate blocks the private-data + external-comms + untrusted-input combination. | `arcllm/modules/injection.py`, `arcprompt` section boundaries, `arctrust/policy.py` | `pytest packages/arcllm/tests -k injection` |
| **LLM02** | Sensitive information disclosure | Classification-aware policy layer evaluates every call; output guardrails filter before egress; secrets never enter prompts. | `ClassificationLayer` in `arctrust/policy.py`, `arcllm/modules/guardrails.py` | `pytest packages/arctrust/tests -k classification` |
| **LLM03** | Supply chain | Every loaded artifact — skill, extension, backend, blueprint — is signature-verified before use. Installs are scanned and can be sandboxed. Dependencies are audited. | `arctrust/artifact.py`, `arcskill/hub/scanner.py`, `arcskill/hub/dry_run.py` | `pip-audit` · `arc skill validate` |
| **LLM04** | Data poisoning | Memory writes are validated and attributed; consolidation reads before it rewrites, so a passing mention cannot truncate an established card; workspace reads are containment-checked. | `arcmemory/stores/*`, `arcmemory/security.py` | `pytest packages/arcmemory/tests -k acl` |
| **LLM05** | Improper output handling | Model output is never executed. Tool arguments are schema-validated before dispatch; the tool set is frozen for the life of a run so output cannot introduce a new tool. | `arcrun/registry.py`, `arcrun/loop.py` | `pytest tests/architecture/test_no_global_tool_name_mutation.py` |
| **LLM06** | Excessive agency | Least-privilege tool allowlists per agent; policy authorizes every dispatch; destructive actions require a signed operator grant, never a chat confirmation. | `arctrust/policy.py` (`AgentLayer`, `SandboxLayer`), `arc approve` | `arc trust list` · `arc approve list` |
| **LLM07** | System prompt leakage | Prompts carry no secrets — credentials resolve from a vault at call time. Prompts are treated as exfiltrable by design. | `arcllm` `VaultResolver`, `arcprompt` | `grep -r "api[_-]key" docs/ packages/*/src` |
| **LLM08** | Vector and embedding weaknesses | Memory is scoped per agent and access-controlled; a cross-agent read is refused fail-closed rather than degraded. | `arcmemory/acl.py`, `gate_no_read_up` | `pytest packages/arcmemory/tests -k acl` |
| **LLM09** | Misinformation | Retrieved memory carries provenance and confidence; a missing embedder degrades **loudly** rather than silently returning nothing. | `arcmemory/degrade.py`, `semantic_status()` | `arc memory status` |
| **LLM10** | Unbounded consumption | Hard budgets on every run: `max_turns`, `max_tokens`, `max_cost_usd`, `max_parallel`, `max_consecutive_errors`, plus rate limiting, queueing and circuit breaking at the provider. | `arcrun.run(...)` budget arguments, `arcllm/modules/{rate_limit,queue,circuit_breaker}.py` | `pytest packages/arcrun/tests -k budget` |

---

## Standing invariants

These hold at every tier and are the reason the table above is short.

1. **`arcrun` is the only runtime path to `arcllm`.** There is no second route
 that could skip the controls.
2. **The tool set is frozen per run.** Nothing the model emits can add a tool
 mid-run.
3. **Audit has a single emission point.** `arctrust.emit()` writes the signed
 chain; sinks fan out from there. There is no parallel telemetry wire that
 could disagree with the record.
4. **Policy fails closed.** An exception inside a policy layer denies the call.

---

## See also

- [OWASP Agentic mapping](compliance-owasp-agentic.md)
- [NIST 800-53 mapping](compliance-nist-800-53.md)
- [Threat model](threat-model.md) · [Adversarial tests](adversarial-tests.md)
- [Security reference](../../reference/security.md)
