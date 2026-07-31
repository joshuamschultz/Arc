# ADR-025: Provider Cache Directives Confined to the Anthropic Adapter

**Status**: Accepted
**Date**: 2026-07-02
**Builds on**: ADR-023 (arcrun is the single runtime path to arcllm), the "Don't Mix Concerns" invariant
**Relates to**: SPEC-029 (Prompt Caching & Context Control)

## Context

Anthropic prompt caching requires explicit `cache_control` breakpoints placed on
the request in the order `tools → system → messages`. The naive way to make this
configurable is to add a `cache: bool` field to the shared arcllm message/tool
types so callers (arcrun, arcagent) can mark breakpoints. That leaks an
Anthropic-specific wire concept into the cross-provider contract that every layer
builds messages with, and every OpenAI-wire adapter would have to ignore it.

## Decision

`cache_control` lives **only** in `arcllm/adapters/anthropic.py`. The adapter
auto-places at most four breakpoints (last tool, one per system segment, rolling
tail message) when caching is enabled via `ProviderSettings.enable_prompt_caching`
(default on) with `cache_ttl` of `5m` (default) or `1h`. No shared arcllm type
carries a cache field; arcrun and arcagent never see the concept.

A caller *does* express which parts of its prompt change at different rates, but
in provider-neutral terms: it passes an ordered list of **system segments**,
most-stable first (`arcrun`'s `system_prompt: str | Sequence[str]`, one
`Message(role="system")` per segment). "Order these by stability" is a fact about
the prompt, not an Anthropic wire directive — the OpenAI-wire adapters simply
concatenate them, and their automatic prefix caching benefits from the same
stability without knowing the concept exists. Anthropic caps a request at four
breakpoints, so the adapter rejects more than two system segments rather than let
the provider return an opaque 400.

The normalized `Usage.cache_read_tokens`/`cache_write_tokens` fields are *telemetry*,
not directives — every adapter may populate them (OpenAI/Gemini read
`prompt_tokens_details.cached_tokens`), but only Anthropic emits a directive.

## Consequences

- Enabling/tuning caching is a one-file change; no ripple across packages.
- The cross-provider message/tool contract stays provider-agnostic.
- A caller cannot request a manual breakpoint at an arbitrary position — only
  declare system segments. Accepted: the adapter places the rest automatically
  (cascade covers the whole prefix).
- The two-segment cap is a hard error, not a silent degradation. Accepted: the
  alternative is a provider 400 that is far harder to trace back to its cause.

## Alternatives considered

- **`cache: bool` on shared types** — rejected: leaks Anthropic wire semantics
  into arcrun/arcagent and burdens every other adapter with a dead field.
- **A caching module wrapping adapters** — rejected as premature abstraction for
  a single-provider concern; a config flag + adapter helper is simpler.
