# ADR-035: Model-Routing Policy Plugs into ArcLLM

**Status**: Accepted
**Date**: 2026-08-22
**Builds on**: ADR-002 (ArcRun Bridge), ADR-024 (Unified Streaming Entry)

## Context

Arc needs policy-driven model selection without weakening the one-way package
stack. NVIDIA NeMo Switchyard is a candidate policy engine, but placing it as a
new runtime layer between ArcRun and ArcLLM would make the loop depend on a
provider-routing implementation and duplicate ArcLLM's existing routing seam.

## Decision

Routing remains an ArcLLM concern. `RoutingModule` is the sole dispatcher and
accepts a typed `RoutingPolicy`. A NeMo Switchyard integration, if selected,
is an optional ArcLLM extension that implements that policy contract. It may
select only from the already authorized `Route` values supplied by ArcLLM; it
does not invoke providers, own retries, inspect ArcRun, or bypass
classification, residency, capability, and budget eligibility checks.

Policy results carry a non-sensitive request hash and policy version through
response metadata and OpenTelemetry attributes. Prompts and credentials never
cross the policy seam unless a separately reviewed policy contract explicitly
requires them.

## Consequences

- ArcRun continues to depend only on ArcLLM's public invocation contract.
- Native routing remains the zero-configuration default.
- Switchyard can be installed or removed without changing ArcRun or ArcAgent.
- All policy-selected routes still pass ArcLLM's fail-closed eligibility gate.
- The external engine adds operational complexity only for deployments that
  opt into it; it is not an alpha runtime dependency.

## Alternatives considered

- **Insert Switchyard between ArcRun and ArcLLM.** Rejected because it creates
  a second routing seam and couples loop execution to an optional engine.
- **Let ArcRun choose provider/model names.** Rejected because provider logic
  belongs exclusively to ArcLLM.
- **Make Switchyard mandatory.** Rejected because standalone and air-gapped
  ArcLLM must remain complete with zero external services.
