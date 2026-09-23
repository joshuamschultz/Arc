# ArcUI field-help inventory

This inventory describes controls present in ArcUI today. The stable help IDs live in [`screen-help.json`](../../packages/arcui/web/src/content/screen-help.json); the UI attaches them to fields and page headers. A help entry is copy, not a promise that a control exists. Conditional controls appear only when their selected tab, workflow node, connector schema, or returned data provides them.

## Coverage and exceptions

There are now 622 authored field IDs across 18 screens. The existing literal control attachments and dynamic agent-tab/section attachments remain. The Settings catalog has 528 entries: path-specific descriptions for declared scalar fields, dynamic pattern descriptions for operator-named entries, section/control help, and honest fallbacks. Settings resolves help against the full serialized file/path: exact authored keys win, then `*` patterns match exactly one dot-separated path segment, preferring patterns with fewer wildcards. Unknown extension modules and undeclared provider additions use the unavailable-description fallback; other fields without authored help use neutral configuration guidance. One old approval ID, `approvals.reason`, described a nonexistent input and has been removed. The approval card shows the reason for the requested action and offers Approve/Deny; it does not ask the operator to enter a decision reason.

| Surface | Actual control and help behavior |
|---|---|
| Home, Fleet | Read-only dashboard and agent roster; no form controls. |
| Agent detail | Identity content editor, tab-specific controls, inbox search/reply/handoff recipient, Telegram setup, and voice settings. The 17 `agent.tab.*` entries are selected by the current tab; unavailable tabs have no control. Read-only identity/status facts are not editable fields. |
| Chat | Conversation selector, message composer, and attachment picker. |
| Tasks | Owner/tag filters; create/edit title, description, priority, owner, review requirement; task status and message-to-owner actions. Board results are paged. |
| Approvals | Expanded approval request cards display requester, tool, arguments, gate reason, and decision controls. There is no selection input or decision-reason field. |
| Pending capabilities | Show-all toggle and capability review/action controls. |
| Rules, Audit, Activity | Search/sort/retired filter, event selection, and run search/selection. Event and run detail content is read-only. |
| Workflows | Workflow fields depend on the selected workflow/node/trigger: name, node configuration, schedule, and response target. Only controls present for the chosen type show field help. |
| Knowledge | Agent/source selectors, document/chunk search, datastore lookup controls, provenance lookup, mapping filter, and configure/sync actions. Resource checkboxes and mapping facts come from the selected source. |
| Shared knowledge, Model usage | Search and time-window selector respectively. Results and chart values are read-only. |
| Tools & Skills | Agent filter and signed capability import controls for target agent, archive, staged file, and staged content. |
| Connections | Bundle/account selection, grants, authorization code, connector-defined credential and endpoint controls, and Doctor/Probe actions. Schema-defined fields use connector descriptions where supplied; secrets never have sample values. |
| Settings | Scope and provider-key controls; each configuration section has guided scalar fields plus Advanced JSON. `settings.<file>.<path>` entries describe known schema fields; `*` marks one operator-defined path segment. The four `settings.section.*` IDs attach to section headings. Provider key display reports presence without revealing saved values. |

## Configuration guidance

The Settings editor follows the returned configuration tree rather than presenting a separately enumerated form schema. Guided scalar inputs have type inferred from the current value and numeric validation additionally uses the current template. Arrays and objects use Advanced JSON. Top-level scalar and array sections, including ArcRun controls, show their authored help in the section heading and Advanced JSON editor. Nested list and null notices resolve help using their actual serialized paths. Nested objects are edited in Advanced JSON; individual JSON tokens do not have separate help popovers. The catalog covers declared scalar fields from ArcAgent core (`AgentConfig`, `LLMConfig`, `BudgetConfig`, `UIConfig`, `IdentityConfig`, `VaultConfig`, `ToolsConfig` and its typed tool-entry models, telemetry/context/eval/session/team/spawn/security/capabilities, `ArcRunConfig`, and the shared `ArcStoreConfig`); it also covers declared fields in the shipped ArcAgent module configuration classes and `GatewayConfig` plus its shipped platform adapters. System ArcLLM guidance covers `DefaultsConfig`, `VaultConfig`, provider connection/model metadata, and the scalar settings in the packaged modules. See source list and exceptions below.

The source of truth is the owning package schema and its consumers: `packages/arcllm/src/arcllm/config.py` and `config.toml`, `packages/arcagent/src/arcagent/core/config.py`, `packages/arcagent/src/arcagent/modules/*/config.py`, `packages/arcrun/src/arcrun` (ArcAgent's `ArcRunConfig`), `packages/arcstore/src/arcstore/config.py`, `packages/arcgateway/src/arcgateway/config.py`, and `packages/arcgateway/src/arcgateway/adapters/{telegram,slack,mattermost,voice}/config.py`. Descriptions explain the field's actual purpose; only explicit Pydantic constraints are stated as bounds. Provider API keys are separate masked key controls and must not be entered in ordinary configuration JSON.

The field-to-content mapping is the field `key` in each Settings entry in `screen-help.json`; its path prefix maps to these schemas and serialized files:

| Config file and schema | Mapped Settings path prefixes |
|---|---|
| Agent `arcagent.toml`; `AgentConfig`, `UIConfig`, `IdentityConfig`, `VaultConfig`, `ToolsConfig`, `TelemetryConfig`, `ContextConfig`, `SessionConfig`, `TeamSection`, `SpawnConfig`, `SecurityConfig`, `CapabilitiesConfig`, `ArcStoreConfig` | `settings.arcagent.agent.*`, `.ui.*`, `.identity.*`, `.vault.*`, `.tools.*`, `.telemetry.*`, `.context.*`, `.session.*`, `.team.*`, `.spawn.*`, `.security.*`, `.capabilities.*`, `.arcstore.*` |
| Agent `arcllm.toml`; `LLMConfig`, `AgentRoute`, `EvalConfig`, `BudgetConfig` | `settings.arcllm.llm.*`, `.llm.routes.*.*`, `.eval.*`, `.budget.*`; operator-named route is a wildcard segment |
| Agent `arcrun.toml`; `ArcRunConfig`, `SandboxSettings` | `settings.arcrun.max_turns`, `.tool_timeout`, `.allowed_strategies`, `.sandbox.allowed_tools`, `.approval_opt_in` |
| Agent module sections; `ModuleEntry` plus shipped `arcagent.modules.<name>.config` schema | `settings.arcagent.modules.*.enabled`, `.*.priority`, and module-specific `settings.arcagent.modules.<name>.config.*` entries. Browser submodels use `.security.*`, `.connection.*`, `.browserbase.*`, `.browser_use.*`, and `.cookies.*`; connected-data limits use `.limits.*`; skill improver fields use `.improver.*` and its `change_bound`, `lifecycle`, and `suite` submodels. |
| System `arcllm.toml`; `DefaultsConfig`, `VaultConfig`, `ProviderSettings`, `EndpointConfig`, `ModelMetadata`, and packaged ArcLLM module config | `settings.arcllm.defaults.*`, `.vault.*`, `.providers.*.provider.*`, `.providers.*.models.*`, `.providers.*.endpoints.*`, and `.modules.<name>.*` |
| System `gateway.toml`; `GatewayConfig`, `WebPlatformConfig`, shipped Telegram/Slack/Mattermost/voice platform schemas | `settings.gateway.gateway.*`, `.security.*`, `.platforms.web.*`, `.platforms.<name>.*`, `.pairing.*` |

The schema class names and package paths above are the auditable coverage map; the JSON entries provide the individual field names and operator-facing descriptions. Array and object field values, including task/tool allowlists, route phrase arrays, module allowlists, and provider endpoint arrays, are edited in Advanced JSON and receive section-level help rather than per-token popovers. Their authored nested keys are used by guided scalar rows or nested list/null notices when those paths are rendered.

### Settings help matching

`configHelpKey(file, path)` builds the full serialized key `settings.<file>.<path>`. An exact authored key wins. If there is no exact match, authored wildcard keys are compared by dot-separated segment; each `*` consumes exactly one segment, and when several patterns match, the pattern with fewer wildcards wins. This supports operator-named routes, tool definitions, provider/model names, policy map entries, module names, and gateway platform names without letting a wildcard consume an arbitrary subtree. The matcher does not strip path prefixes, since that could attach an unrelated description.

When no exact or wildcard entry matches, unknown extension modules and undeclared provider additions receive `settings.unknown_extension_field`; other fields receive `settings.config_value`. This describes the editor's actual fallback behavior. It does not claim that all 528 catalog entries are individually visible in every live configuration: a field must exist in the returned configuration tree and render as a supported control to show its help.

### Schema coverage and exceptions

Core declared scalar coverage includes agent identity/display metadata; LLM/evaluation/budget; tool policy and typed MCP/HTTP/process timeout/endpoint fields; telemetry, context, session, team, spawn, security, capability-import policy and ArcRun controls; all ArcStore settings; gateway/web/Telegram/Slack/Mattermost/voice endpoint and enablement scalars; system ArcLLM defaults and vault; declared scalar settings in the packaged ArcLLM modules; and declared scalar properties in shipped ArcAgent module config schemas. Fields whose values are lists/objects are not guided scalar controls and remain available in Advanced JSON with section-level help. Dynamic map values with a known, common semantic have wildcard help.

Named exceptions that cannot receive a truthful per-field description from these core schemas: arbitrary `ModuleEntry.config` values for third-party modules; connector/provider adapter fields owned by separately installed extensions; arbitrary `ProviderSettings`/`ModelMetadata` additions not declared in the shipped schema; free-form dictionaries such as environment/header maps, backend-specific parameter objects, routing phrase lists and arbitrary route/provider-specific metadata; and values introduced by future schema versions. These use generic schema guidance or the explicit extension-description-unavailable fallback. Secret values must never appear as examples.

## Route coverage

All 18 current entries correspond to routes in `packages/arcui/web/src/app/router.tsx`: Home, Fleet, Agent detail, Chat, Tasks, Approvals, Pending capabilities, Rules, Audit, Activity, Workflows, Knowledge, Shared knowledge, Model usage, Tools & Skills, Connections, and Settings. Route variants use the same screen entry. The approvals card control uses `approvals.request`; no control references `approvals.reason`.
