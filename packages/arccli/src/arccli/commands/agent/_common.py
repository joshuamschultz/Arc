"""Shared helpers for the `arc agent` subcommand subpackage.

Sibling helpers used across multiple subcommand modules. Constants
(scaffolding templates, env-search path, global capabilities dir,
the bundled calculator capability source) live here so any
subcommand can import them without crossing files.

Re-exported through ``arccli.commands.agent`` so existing internal
imports
(``from arccli.commands.agent import _resolve_agent_dir``) keep
working unchanged.
"""

from __future__ import annotations

import asyncio
import datetime
import importlib
import importlib.util
import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import arcagent
from arcokf import OKFValidationError, validate
from arctrust.paths import dotenv_file, env_file

from arccli.commands._arcllm_surface import (
    BUDGET_BLOCK,
    EVAL_BLOCK,
    commented_module_surface,
)
from arccli.commands._shared import print_kv as _print_kv
from arccli.commands._shared import print_table as _print_table

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_DEFAULT_IDENTITY = """\
# Agent Identity

You are a helpful assistant with access to tools and a structured workspace.

## About Me

**My Name:** (Update when you learn your name)

**My Role:** (Update when you learn your purpose or how you should behave)

## About the User

**User's Name:** (Update when you learn the user's name)

## Behavior

**CRITICAL: You MUST use tools - never just say you did something.**

1. **ALWAYS use tools** when saving, reading, or searching
2. **Be direct and concise** - No filler, no hedging
3. **Show your work** - Report what tools you used and what they returned
"""

_SEED_POLICY_BULLETS = (
    "Be helpful and direct",
    "Use tools when appropriate",
    "Report errors clearly",
)


def _default_policy() -> str:
    """Seed ``policy.md`` with structured ACE bullets.

    Each line carries the ``{score, uses, reviewed, created, source}`` trailer
    the policy engine expects, so the curator can score/update them and the UI
    can parse them. A bullet without that metadata is invisible to both.
    """
    today = datetime.date.today().isoformat()
    lines = ["# Policy", ""]
    for i, text in enumerate(_SEED_POLICY_BULLETS, start=1):
        lines.append(
            f"- [P{i:02d}] {text} "
            f"{{score:5, uses:0, reviewed:{today}, created:{today}, source:init}}"
        )
    return "\n".join(lines) + "\n"


_DEFAULT_CONTEXT = """\
# Context

Working memory for the agent. Updated during conversations.
"""

_DEFAULT_CONFIG = """\
# ArcAgent config — everything EXCEPT LLM-wire (arcllm.toml) and the agentic
# loop controls (arcrun.toml). Grouped by concern; every operator-settable knob
# is present at its default with a one-line note. Sibling files load from the
# SAME directory and compose into one effective config.

# =========================================================================
# 1. IDENTITY & CORE — who this agent is
# =========================================================================
[agent]
name = "{name}"          # logical/display name
org = "local"            # owning org/namespace
type = "executor"        # agent role label
workspace = "./workspace"  # workspace dir (relative to this file)

[identity]
did = "{did}"                     # agent DID (minted by `arc agent create`)
key_dir = "~/.arcagent/keys"  # keypair dir (env-override blocked)
vault_path = ""              # resolve identity key via vault instead of disk

[ui]
# ArcUI Fleet / Team-Chat display metadata (read by arcui; core ignores it).
display_name = ""  # falls back to agent.name when empty
role_label = ""    # role chip shown in the Fleet panel
color = ""         # display color (hex or name)
hidden = false     # hide this agent from the Fleet panel

# =========================================================================
# 2. CONTEXT & SESSION — the agent's working-memory lifecycle
# =========================================================================
[context]
max_tokens = 128000        # context-window token budget
prune_threshold = 0.70     # fraction of budget → prune
compact_threshold = 0.85   # fraction → compact
emergency_threshold = 0.95  # fraction → emergency compaction
estimate_multiplier = 1.1  # token-estimate safety multiplier

[session]
retention_count = 50               # keep last N sessions
retention_days = 30                # or sessions from last N days
compaction_summary_max_chars = 2000  # cap on a compaction summary

# =========================================================================
# 3. SECURITY & EXECUTION — the federal-first envelope (review as a unit)
# =========================================================================
[security]
tier = "{tier}"                # federal | enterprise | personal (canonical tier)
clearance = "UNCLASSIFIED"       # agent max clearance (operator-authored)
classification_enforced = false  # fail closed on missing clearance (federal posture)
# Loop circuit breakers (tier-floored security controls; None disables at
# personal). runaway = identical tool-call signatures; cascade = consecutive
# failures; parallel = concurrent in-flight tool calls.
# runaway_max_repeat = 8         # (federal floor; unset = disabled at personal)
# error_cascade_max = 5          # (federal floor; unset = disabled at personal)
loop_max_parallel = 10
policy_audit_log = ""            # WORM policy-decision chain (empty = <workspace>/audit/)
operator_key_dir = ""            # empty = the deployment operator key dir (audit authority)
operator_vault_path = ""         # resolve operator key via vault instead of disk
signing_algorithm = "{signing_algorithm}"    # ed25519 | ecdsa-p256 (federal forces ecdsa-p256)
custody = "{custody}"           # in_process | vault_transit (enterprise default vault_transit)
notary_keystore = ""             # vault_transit keystore (empty = <operator_key_dir>/notary)
require_fips = {require_fips}             # federal floor: fail closed unless FIPS-validated crypto
witness_medium_path = ""         # federal witness (empty = the deployment witness dir)
witness_mode = "offline"         # offline | transparency_log
witness_log_url = ""             # transparency-log endpoint when witness_mode = transparency_log

[security.validators]
# Personal-only: auto-run agent-authored Python after the AST gate.
# Enterprise/federal must TOFU-approve via `arc trust approve`.
auto_run_agent_code = false
# [[security.validators.approved]] entries are appended only by `arc trust approve`.

[capabilities]
# Relax the AST import gate for agent-authored tools under
# workspace/capabilities/ WITHOUT moving them out of the protected root.
# Tier-gated: personal = all imports allowed (this block is ignored);
# enterprise = deny by default, set allow_all_imports = true or list
# allow_imports; federal = deny by default, ONLY allow_imports is honored
# (allow_all_imports is ignored). eval/exec/frame-traversal stay blocked always.
allow_all_imports = false
allow_imports = []

[execution]
# Code-execution isolation floor follows [security] tier:
#   federal -> VM (Firecracker/KVM), enterprise -> container, personal -> container.
# A PERSONAL-tier operator MAY relax down to a bare host subprocess
# ("sandbox off" — full host filesystem access, no container) by uncommenting
# exactly one of the lines below. Rejected at enterprise/federal (cannot go
# below the tier floor). Unset = the tier default (container for personal).
#   relax_isolation = "off"        # sandbox OFF: run on the host, no isolation
#   relax_isolation = "container"  # explicit container (same as the default)

[vault]
backend = ""            # vault backend selector (ArcLLM VaultResolver; env-override blocked)
cache_ttl_seconds = 300  # credential cache TTL (seconds)

# =========================================================================
# 4. TOOLS — the agent's hands and their guardrails
# =========================================================================
[tools]
allowed_module_prefixes = ["arcagent."]  # import prefixes permitted for module-transport tools
preamble = ""                            # tool-system preamble text (env-override blocked)

[tools.policy]
allow = []              # tool allowlist (empty = allow all)
deny = []               # tool denylist (deny wins)
timeout_seconds = 30    # per-tool call timeout
allowed_paths = []      # filesystem paths tools may access
protected_paths = []    # read-only-to-agent paths (unioned with goal-file defaults)
egress_allowlist = []   # scheme://host[:port] origins external-comms tools may reach
classifications = {{}}    # per-tool resource classification label (no-read-up)
egress_clearances = {{}}  # per-origin destination clearance (no-exfil)

[tools.human_gate]
# Lethal-trifecta human-approval gate. auto_approve lists named low-risk leg
# compositions personal/enterprise may approve without a human (federal ignores).
timeout_seconds = 300.0
auto_approve = []

# Tool transports (add entries as needed):
# [tools.mcp_servers.<name>]  command = "..."  args = []  env = {{}}  timeout_seconds = 30
# [tools.http.<name>]         url = "..."  method = "POST"  headers = {{}}  timeout_seconds = 30
# [tools.process.<name>]      command = "..."  args = []  timeout_seconds = 30  (env blocked)

# =========================================================================
# 5. MEMORY — durable-memory concerns (Brain, ACL, cockpit, profiles)
# =========================================================================
[modules.memory]
enabled = true
priority = 100

[modules.memory.config]
# Dual-speed analogical memory (arcmemory). Fresh agents get memory ON:
# zero-LLM capture writes the raw episodic stream (workspace/memory/index.db)
# and the entity graph every turn. The slow consolidation path (distiller)
# turns that stream into durable CURATED memory — entity/person/place cards,
# insights, and the human-readable daily-notes (workspace/memory/daily-log/
# YYYY-MM-DD.md, a summary — not a transcript). Without distill_provider,
# consolidation is a no-op and none of those curated files are ever written.
# brain = "none" turns memory off entirely.
brain = "arcmemory"          # none | arcmemory | auto | module:Class
tier = "{tier}"            # memory-dynamics stringency tier
brain_allowlist = []         # operator-vetted BYO brain class-paths (above personal)
embed_backend = "local"      # local (arcllm offline model) | none (BM25 + graph only)
embed_model = ""             # empty → arcllm default (all-MiniLM-L6-v2)
distill_provider = "anthropic"  # consolidation distiller provider (empty = no-op)
distill_model = "claude-sonnet-4-5-20250929"  # distiller model
top_k = 5                    # recall count at assemble_prompt
budget = 1024                # recall token budget
consolidate_event_threshold = 20      # fire consolidation after N events
consolidate_idle_seconds = 900.0      # fire after idle seconds
consolidate_interval_seconds = 3600.0  # time-based cadence floor

[modules.memory.config.dynamics]
# Overrides for the memory backend's own dynamics (arcmemory MemoryConfig), applied
# OVER the tier defaults. Otherwise these are tier-locked. Uncomment to tune. Examples:
#   entity_merge_candidate_threshold = 0.80  # name-embedding cosine to become a merge
#                                            # CANDIDATE (lower = wider net; the LLM still
#                                            # confirms each, so a wider net is safe).
#   gamma = 0.5                              # confidence growth per corroboration
#   forget_floor = 0.05                      # edge weight below which links decay away
#   struct_trigger_min = 0.7                 # structural-recall trigger match floor

[modules.workpad]
enabled = true
priority = 100

[modules.workpad.config]
# Sole writer of context.md: every ``every_n_runs`` real runs it rewrites the
# file as a curated cockpit of open loops. ``flush_idle_seconds`` is the idle
# backstop. Counters persist to workspace/.workpad-state.json.
every_n_runs = 20
max_transcript_chars = 24000  # recent-activity transcript cap fed to the maintainer
max_context_chars = 8000      # hard cap on rewritten context.md
flush_idle_seconds = 900      # idle-flush backstop seconds

[modules.progress]
enabled = true
priority = 100

[modules.progress.config]
# Live step-by-step narration of a long fan-out run, sent back to the channel
# the request came in on and nowhere else. A run with no channel (a schedule, a
# dispatched task, a CLI run) is narrated to nobody. Defaults are deliberately
# quiet: a chatty agent is worse than a silent one.
coalesce_seconds = 1.5    # wait this long so a burst of agents is one message
min_gap_seconds = 10      # floor between ordinary lines; results ignore it
max_lines_per_run = 12    # hard ceiling on messages about one run
max_step_chars = 90       # model-written stage names are cut to this

[modules.user_profile]
enabled = false
priority = 100

[modules.user_profile.config]
profile_dir = "user_profile"        # workspace sub-dir for profiles
body_cap_bytes = 2048               # markdown body cap
tombstone_dir = "tombstone_events"  # GDPR tombstone dir
schema_version = 1                  # profile schema version

# =========================================================================
# 6. BEHAVIOR MODULES — self-learning + autonomy loops
# =========================================================================
[modules.policy]
enabled = true
priority = 100

[modules.policy.config]
eval_interval_turns = 50        # Policy Reflector cadence (turns)
daily_notes_every_turns = 20    # grounded daily-notes reflection cadence (turns)
max_bullets = 200               # max policy bullets
max_bullet_text_length = 500    # max chars per bullet
flush_idle_seconds = 900        # idle-flush backstop seconds
tier = "{tier}"               # federal stages to policy.pending; else auto-applies

[modules.skills]
enabled = true
priority = 100

[modules.skills.config]
# arcskill is the workspace-declared default skills adapter (see root
# pyproject.toml) — without this block SkillsConfig defaults to adapter = "none"
# and the agent's scaffolded skills/improver never run.
adapter = "arcskill"        # none | arcskill | module:Class
tier = "{tier}"           # adapter tier
classify_outcomes = false   # consult eval-LLM OutcomeClassifier at post_plan
sweep_poll_seconds = 3600.0  # curator lifecycle-sweep poll cadence
adapter_allowlist = []      # operator-vetted BYO adapter class-paths
# [modules.skills.improver] block is forwarded verbatim to arcskill ImproverConfig.

[modules.pulse]
enabled = false
priority = 100

[modules.pulse.config]
interval_seconds = 600      # pulse tick interval (>= 10)
pulse_file = "pulse.md"     # checks-definition file
state_file = "pulse-state.json"  # state file
timeout_seconds = 300.0     # per-check timeout

[modules.proactive]
enabled = false
priority = 100

[modules.proactive.config]
leader = "noop"             # noop | redis | k8s (leader election backend)
identity = ""               # empty = agent name / hostname
redis_url = ""
redis_key = "arcagent:proactive:leader"
k8s_namespace = ""
k8s_lease_name = ""

[modules.scheduler]
enabled = true
priority = 100

[modules.scheduler.config]
min_interval_seconds = 60      # schedule interval floor
max_schedules = 50            # max schedules
max_prompt_length = 500       # max scheduled-prompt chars
default_timeout_seconds = 300  # default run timeout
max_timeout_seconds = 3600    # timeout ceiling
circuit_breaker_threshold = 3  # failures → trip
check_interval_seconds = 30   # scheduler poll cadence
store_path = "schedules.json"  # schedule store file
timezone = ""                 # IANA tz for cron/once (e.g. "America/Chicago"); empty = UTC

# =========================================================================
# 7. COMMS & TASKS — inter-agent bus, task board, external gateways, I/O
# =========================================================================
[team]
root = ""  # shared team root dir; all team modules read it from here

[spawn]
# Multi-agent fan-out — a run spawning child runs via the spawn_task tool.
enabled = true            # register spawn_task as an LLM-callable tool
max_depth = 3             # max nesting depth for spawned children
max_concurrent = 5        # max concurrent child runs
timeout_seconds = 300     # wall-clock timeout per child run
# max_total_tokens =      # shared token pool across all children (LLM10; unset = uncapped)

[modules.messaging]
enabled = true
priority = 100

[modules.messaging.config]
# Join the shared team bus. ensure_live_backend degrades to an in-memory
# backend (with a warning) if no server is reachable, so a solo agent still runs.
entity_id = ""                # registry entity id (empty = agent name)
entity_name = ""              # registry entity name
nats_url = "nats://127.0.0.1:4222"  # NATS JetStream url; empty → in-memory backend
auto_ack = true               # auto-ack on tool read
max_messages_per_poll = 20    # per-poll message cap
roster_ttl_seconds = 60.0     # team roster cache TTL

[modules.tasks]
enabled = true
priority = 100

[modules.tasks.config]
# Mission Control (SPEC-056): a per-agent task list plus a shared team board.
# nats_url mirrors messaging so assign_task can resolve @handles.
dispatch = true              # execute assigned tasks so workflow nodes progress by default
nats_url = "nats://127.0.0.1:4222"  # shared arcteam registry url (@handle resolution)
default_max_attempts = 3     # retry ceiling (1 disables retry)
retry_backoff_seconds = 30.0  # base exponential backoff
task_timeout_seconds = 0.0   # per-run wall-clock cap (0 = unbounded)
stuck_reclaim_seconds = 300.0  # orphaned in_progress reclaim threshold
routing = true               # auto-route ownerless tasks to least-loaded agent
notify = true                # operator/assignee notifications on transitions

[modules.workflows]
enabled = true
priority = 100

[modules.workflows.config]
# ArcFlow (SPEC-061): named, signed workflow definitions this agent can author
# from conversation and run. Declared here because an undeclared module sits
# dead fleet-wide — the builder tools never register and nothing says why.
workflows_dir = "workflows"  # bundle root, relative to the deployment config dir
nats_url = "nats://127.0.0.1:4222"  # team bus; gives authoring a real agent roster
max_workflows = 50           # quota, checked before any validation work (LLM10)
max_nodes = 200              # per-definition node ceiling
max_inline_text_length = 2000  # ceiling on inline free text a builder tool accepts
max_repair_attempts = 3      # bounded self-repair before asking the human

[modules.connectors]
enabled = true
priority = 100

[modules.connectors.config]
# Connected accounts (SPEC-062, SPEC-064): every connection this agent has been
# GRANTED is attached at startup — the bundle is verified, its approval mode
# bound to the human gate, and its verbs registered as individually named tools.
# A newly created agent holds no grant, so it starts with no connections until an
# operator runs `arc connector grant`; creating an agent can never widen access.
# Declared here because an undeclared module sits dead: the grant would exist and
# the agent would serve none of it.
arc_dir = ""          # deployment root holding connections.toml + connections.env;
                      # empty = ~/.arc (honours $ARC_CONFIG_DIR)
extensions_root = ""  # ONE bundle root; empty = search <arc_dir>/extensions,
                      # $ARC_EXTENSIONS_ROOT, then ~/.arc/extensions
data_dir = ""         # empty defers to arcstore.resolve_data_dir (approved tool contracts)

[modules.runcontrol]
enabled = true
priority = 100

[modules.runcontrol.config]
# Operator kill-switch watcher (SPEC-056 follow-up). Polls the shared
# ``cancellations`` store and cooperatively cancels the matching live tracked
# run. ``arc stop`` and the arcui cancel route WRITE cancel requests regardless,
# but a fleet agent needs THIS block for the watcher that APPLIES them — so an
# agent can be stopped without SSHing the box.
data_dir = ""                # empty defers to arcstore.resolve_data_dir (shared store)
stale_ttl_seconds = 300      # age out a cancel request that never matches a live run

# Telegram/Slack are gateway ADAPTERS (packages/arcgateway-*), not in-agent
# modules — configure them in the gateway's [platforms.*] blocks. The agent
# reaches the human through the gateway channel (notify_user), never a bot it
# owns. (Legacy [modules.telegram]/[modules.slack] were removed.)

[modules.web]
enabled = false
priority = 100

[modules.web.config]
# extract_provider "browser" is KEYLESS — it reads pages through the browser
# module's Chrome DevTools Protocol backend, so site lookup works with no
# account. Parallel/Firecrawl/Tavily are paid opt-ins: name one here AND set its
# key (PARALLEL_API_KEY / FIRECRAWL_API_KEY / TAVILY_API_KEY).
extract_provider = "browser"    # browser (keyless) | parallel | firecrawl | tavily
# No keyless web search exists, so search is off until you buy one. Uncomment,
# name a provider, and set its key — otherwise web_search is not registered.
# search_provider = "tavily"    # parallel | firecrawl | tavily — all require a key
browser_cdp_url = ""            # empty launches local Chrome; federal needs a remote one
tier = "{tier}"               # drives allowlist / PII enforcement
url_allowlist = []              # glob allowlist (federal requires non-empty)
max_content_bytes = 1000000     # extracted-content truncation cap
pii_redaction_enabled = false   # off at personal/enterprise; federal forces it on
request_timeout_s = 30.0        # provider HTTP timeout

[modules.voice]
enabled = false
priority = 100

[modules.voice.config]
tier = "{tier}"                # drives air_gap / redact_pii defaults
stt_provider = "whisper_cpp"     # whisper_cpp | whisper_api
tts_provider = "piper"           # piper | elevenlabs
air_gap = false                  # local-only providers (federal always true)
redact_pii = false               # federal/enterprise always true
elevenlabs_api_key_env = "ELEVENLABS_API_KEY"
elevenlabs_base_url = "https://api.elevenlabs.io/v1"
elevenlabs_default_voice_id = "21m00Tcm4TlvDq8ikWAM"
openai_api_key_env = "OPENAI_API_KEY"
openai_whisper_model = "whisper-1"
whisper_cpp_binary = "whisper-cpp"
whisper_cpp_model = "base.en"
whisper_cpp_threads = 4
piper_binary = "piper"
piper_model = "en_US-lessac-medium"
piper_data_dir = ""
transcribe_timeout_s = 60
synthesize_timeout_s = 30

[modules.browser]
enabled = false
priority = 100

[modules.browser.config]
provider = "cdp"                 # cdp (local launch / remote attach) | browserbase
tier = "{tier}"                # federal forbids local Chrome (remote CDP only)
accessibility_tree_depth = 10
chrome_memory_limit_mb = 512

[modules.browser.config.security]
url_mode = "denylist"
url_patterns = []
blocked_schemes = ["file", "chrome", "chrome-extension", "javascript", "data", "blob", "ftp"]
allow_js_execution = true
allow_downloads = true
download_path = "/tmp/arcagent-downloads"
redact_inputs = false
max_page_text_length = 50000
max_screenshot_width = 1920
max_screenshot_height = 1080

[modules.browser.config.connection]
cdp_url = ""                     # empty = launch local Chrome; set = attach remote CDP
endpoint_kind = "page"           # page = direct page target | browser = managed endpoint
chrome_path = ""
headless = true
remote_debugging_port = 0
chrome_flags = []
startup_timeout_seconds = 10

# provider = "browserbase": managed remote browser. The API key resolves via
# the tier-aware secret resolver (browserbase_api_key / BROWSERBASE_API_KEY).
[modules.browser.config.browserbase]
project_id = ""
api_key_env = "BROWSERBASE_API_KEY"
api_base = "https://api.browserbase.com/v1"
region = ""
proxies = false
keep_alive = false
request_timeout_s = 30.0

# Agentic browser_task tool. Needs the 'browser-use' package (installed into the
# deployment venv separately). Off by default; FORBIDDEN at the federal tier.
[modules.browser.config.browser_use]
enabled = false
llm_provider = "anthropic"
llm_model = ""
max_steps = 25
max_actions_per_step = 5
use_vision = true
step_timeout_s = 180
cdp_url = ""

[modules.browser.config.cookies]
persist = false
encryption_key_env = "ARCAGENT_BROWSER_COOKIE_KEY"
storage_path = ""

# =========================================================================
# 8. TELEMETRY & STORE — observability + the durable operational store
# =========================================================================
[telemetry]
enabled = true              # enable OTel + logging
service_name = "{name}"      # OTel service name
log_level = "INFO"          # log level
export_traces = false       # export OTel traces
exporter_endpoint = ""      # OTLP endpoint
# Persist raw tool args + results to the spool (feeds arcstore store_raw_bodies).
# Federal/enterprise set false to keep only digests.
capture_tool_io = true

[arcstore]
enabled = true              # single on/off gate for spool/ingest recording
data_dir = ""               # empty → resolve_data_dir (env > this > ~/.arc/store)
database_credential_ref = "" # vault coordinate; runtime DSN comes from the vault/env
pool_min_size = 1
pool_max_size = 10
command_timeout = 30.0
connect_timeout = 10.0
store_raw_bodies = false    # persist raw request/response bodies
rotation = "daily"          # store file rotation
retention = ""              # retention window (empty = keep all)
sample_rate = 1.0           # recording sample rate (0.0-1.0)
"""

AGENT_TIERS = ("personal", "enterprise", "federal")
"""Canonical deployment tiers, least to most stringent."""


def render_agent_config(*, name: str, tier: str = "personal", did: str = "") -> str:
    """Render the full arcagent.toml surface for one agent at one tier.

    ``tier`` sets every subsystem's tier at once — [security], memory, policy,
    skills, web, voice, browser. They are one decision: a config that is federal
    in [security] but personal in [modules.web] is a hole, not a preference.

    ``did`` is substituted rather than blanked so a regeneration keeps the
    identity the agent signs its capabilities with and is registered to
    arcteam under.
    """
    if tier not in AGENT_TIERS:
        raise ValueError(f"unknown tier {tier!r} — choose one of {', '.join(AGENT_TIERS)}")
    return _DEFAULT_CONFIG.format(name=name, tier=tier, did=did, **_crypto_posture(tier))


def _crypto_posture(tier: str) -> dict[str, str]:
    """Tier-correct values for the crypto knobs the template states explicitly.

    ``SecurityConfig`` auto-resolves federal floors only for knobs the operator
    left unset; an explicitly *weaker* value is refused fail-closed. Because
    this template states every knob outright — that is the point of it — a
    federal render must state the federal floor, or the config it produces will
    not load at all.

    Federal values come from ``SECURITY_CONFIG_KNOBS`` rather than being
    duplicated here, so moving a floor moves the template with it.
    """
    if tier == "federal":
        floors = {k.name: k.federal_floor for k in arcagent.SECURITY_CONFIG_KNOBS}
        return {
            "signing_algorithm": str(floors["signing_algorithm"]),
            "custody": str(floors["custody"]),
            "require_fips": "true" if floors["require_fips"] else "false",
        }
    return {
        "signing_algorithm": "ed25519",
        # REQ-007: enterprise custody defaults to vault_transit; it may relax to
        # in_process, which is why this is a starting point and not a floor.
        "custody": "vault_transit" if tier == "enterprise" else "in_process",
        "require_fips": "false",
    }


_ARCLLM_HEADER = """\
# ArcLLM config — everything LLM-wire for this agent. arcagent composes the
# [llm]/[eval]/[budget] tables below into the effective config. The full arcllm
# module surface is listed at the bottom under [llm.modules.*], commented at its
# packaged default — uncomment a line to override that module for THIS agent
# only. arcllm's OWN global module + provider defaults live in the user-wide
# ~/.arc/arcllm.toml ([defaults]/[modules]/[vault]), read by arcllm itself.

[llm]
model = "anthropic/claude-sonnet-4-5-20250929"  # ArcLLM model id (provider/model)
max_tokens = 8192   # max output tokens per LLM call
temperature = 0.7   # sampling temperature

# Additional models this agent may be routed to. The model above stays the
# default; a route is chosen per call by explicit pin, then tool continuity,
# then phrase match. This list is also the boundary — the router can reach a
# provider only if the agent declared it here.
#
# [llm.routes.local]
# model = "litellm/qwen3-coder"
# phrases = ["run this locally", "keep this on my machine"]
"""


def _build_default_arcllm_config() -> str:
    """Compose the per-agent arcllm.toml: [llm]/[eval]/[budget] + the full,
    commented [llm.modules.*] override surface (derived from arcllm's config)."""
    parts = [
        _ARCLLM_HEADER,
        "\n".join(EVAL_BLOCK),
        "",
        "\n".join(BUDGET_BLOCK),
        "",
        "# --- Per-agent arcllm module overrides. Every module below is shown",
        "# commented at its packaged default; uncomment a line to override that",
        "# module for THIS agent only (unknown module names are rejected). ---",
        commented_module_surface(prefix="llm."),
    ]
    return "\n".join(parts).rstrip() + "\n"


_DEFAULT_ARCLLM_CONFIG = _build_default_arcllm_config()

_DEFAULT_ARCRUN_CONFIG = """\
# ArcRun config — the agentic-loop controls arcagent hands to the run loop.
# (Per-run token/cost/request ceilings live in arcllm.toml [budget]; the
# tier-floored circuit breakers live in arcagent.toml [security].)

max_turns = 120         # hard cap on the agent's own agentic loop turns
# tool_timeout = 30.0   # per-tool-call wall-clock timeout (seconds); unset = none
# Unset = every strategy is available and the model picks the one that fits each
# task. Narrow it only to take capability away. The federal tier ignores this and
# permits "react" alone, because the other strategies let a model author its own
# control flow.
# allowed_strategies = ["react"]
approval_opt_in = []    # tool names always requiring human approval at personal/enterprise

[sandbox]
# allowed_tools = ["calculate"]  # restrict the loop to these tools; unset = all allowed
"""

_CALCULATOR_TOOL = '''\
"""Capability: calculate — safe arithmetic via AST parsing."""

from __future__ import annotations

import ast
import operator

import arcagent

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp):
        op_fn = _OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        return op_fn(_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op_fn = _OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        return op_fn(_safe_eval(node.operand))
    raise ValueError(f"Unsupported expression: {ast.dump(node)}")


@arcagent.tool(
    description="Evaluate a math expression. Supports +, -, *, /, %, **.",
    classification="read_only",
    capability_tags=["computation"],
    when_to_use="When you need to evaluate an arithmetic expression deterministically.",
    version="1.0.0",
)
async def calculate(expression: str) -> str:
    """Evaluate ``expression`` safely via AST parsing."""
    try:
        tree = ast.parse(expression, mode="eval")
        return str(_safe_eval(tree))
    except Exception as exc:  # reason: fail-open — continue
        return f"Error: {exc}"
'''


def _env_paths() -> list[Path]:
    """The ``.env`` files an agent command loads, in precedence order.

    ``arc.env`` comes from :func:`arctrust.paths.env_file` — the same resolver
    :func:`arcagent.keys.default_env_file` writes through — so a key set by any
    surface is a key this loader reads. Resolved per call, never frozen at
    import: the Arc home is routinely relocated after this module loads, and the
    cwd can change within one process.
    """
    return [
        Path.cwd() / ".env",
        env_file(),
        Path.home() / ".env",
    ]


# ---------------------------------------------------------------------------
# Env / agent-dir / config / tool helpers
# ---------------------------------------------------------------------------


def _load_env(agent_dir: Path | None = None) -> None:
    """Load .env files without importing click."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return  # dotenv optional for status/read-only commands
    # The deployment's own .env wins over the ambient ones, so a self-contained
    # folder is "start up, config, and go" without exporting keys by hand.
    paths = [dotenv_file(), *_env_paths()]
    if agent_dir is not None:
        paths.insert(0, agent_dir / ".env")
    for env_path in paths:
        if env_path.exists():
            load_dotenv(env_path)


def _resolve_agent_dir(path: str) -> Path:
    """Resolve and validate an agent directory path."""
    agent_dir = Path(path).expanduser().resolve()
    if not agent_dir.exists():
        sys.stderr.write(f"arc agent: directory not found: {agent_dir}\n")
        sys.exit(1)
    return agent_dir


def _load_agent_config(agent_dir: Path) -> dict[str, Any]:
    """Load arcagent.toml; exit 1 on failure."""
    config_path = agent_dir / "arcagent.toml"
    if not config_path.exists():
        sys.stderr.write(f"arc agent: no arcagent.toml in {agent_dir}\n")
        sys.exit(1)
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def _load_agent_llm_config(agent_dir: Path) -> dict[str, Any]:
    """Load the agent's arcllm.toml — the home of `[llm]`/`[eval]`/`[budget]`.

    Returns an empty mapping when the file is absent so callers report their
    own "not configured" verdict rather than dying on a missing file.
    """
    config_path = agent_dir / "arcllm.toml"
    if not config_path.exists():
        return {}
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def _import_capability_file(path: Path) -> Any:
    """Import a capability `.py` by file path (no package required)."""
    module_name = f"arccli_cap_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not create import spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _discover_tools(agent_dir: Path) -> list[Any]:
    """Discover @tool-decorated capabilities in the agent's capabilities/ dir."""
    caps_dir = agent_dir / "capabilities"
    if not caps_dir.is_dir():
        return []
    all_tools: list[Any] = []
    for cf in sorted(caps_dir.glob("*.py")):
        if cf.name.startswith("_"):
            continue
        try:
            mod = _import_capability_file(cf)
        except Exception as e:  # reason: fail-open — continue
            sys.stdout.write(f"  Warning: could not load capabilities/{cf.name}: {e}\n")
            continue
        for value in vars(mod).values():
            meta = getattr(value, "_arc_capability_meta", None)
            if meta is not None and getattr(meta, "kind", None) == "tool":
                all_tools.append(meta)
    return all_tools


@dataclass(frozen=True)
class _DiscoveredTool:
    """One tool as the agent's real runtime registry would report it.

    ``source`` is the scan root the tool was found under ("builtins",
    "global", "agent", "workspace", or "module:<name>") — free provenance
    from :class:`~arcagent.capabilities.capability_registry.ToolEntry`,
    which already tracks it.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    source: str
    timeout_seconds: int | None = None


def _discover_runtime_tools(agent_dir: Path) -> list[_DiscoveredTool]:
    """Answer "what tools would this agent actually have at startup?" (task #29).

    Unlike :func:`_discover_tools` (which only looks at the agent's OWN
    ``capabilities/`` directory — the right question for ``arc agent build``/
    ``arc agent status``), this builds the same standalone CapabilityRegistry
    ``arc ext inspect`` uses: builtins + global + agent + workspace + every
    ENABLED module's ``capabilities.py``. That registry is what closes the
    live bug — an agent with one scaffolded capability reported exactly one
    tool via ``arc agent tools`` while ``arc ext inspect`` correctly showed
    ~15 (the builtins were never scanned).
    """
    from arccli.commands._capability_registry import build_capability_registry

    config_path = agent_dir / "arcagent.toml"
    if not config_path.is_file():
        return []
    try:
        config = arcagent.load_config(config_path)
    except Exception:  # reason: fail-open — a listing command must degrade, not crash
        return []

    registry = build_capability_registry(config, agent_dir)
    if registry is None:
        return []

    # Snapshot read of the registry's private tool dict — the same read-only
    # pattern arcagent.extension.inspect._iter_registry already uses for this
    # exact purpose (inspection never mutates, so a private-dict read is the
    # accepted convention rather than growing CapabilityRegistry's public API
    # for a CLI-only need).
    entries = getattr(registry, "_tools", {}).values()
    tools = [
        _DiscoveredTool(
            name=entry.meta.name,
            description=entry.meta.description,
            input_schema=entry.meta.input_schema,
            source=entry.scan_root,
            timeout_seconds=getattr(entry.meta, "timeout_seconds", None),
        )
        for entry in entries
    ]
    return sorted(tools, key=lambda t: t.name)


# ---------------------------------------------------------------------------
# Workspace scaffold
# ---------------------------------------------------------------------------


def _scaffold_workspace(agent_dir: Path, name: str) -> None:
    """Create the agent + workspace directory structure (SPEC-021 layout)."""
    workspace = agent_dir / "workspace"
    workspace.mkdir(exist_ok=True)

    identity_path = workspace / "identity.md"
    if not identity_path.exists():
        identity_path.write_text(_DEFAULT_IDENTITY)

    policy_path = workspace / "policy.md"
    if not policy_path.exists():
        policy_path.write_text(_default_policy())

    context_path = workspace / "context.md"
    if not context_path.exists():
        result = validate(_DEFAULT_CONTEXT, path=context_path.name)
        if not result.valid:
            raise OKFValidationError(result.diagnostics)
        context_path.write_text(_DEFAULT_CONTEXT)

    # Per-agent capabilities live at the AGENT root (trusted scan root).
    # Agent-authored capabilities go under workspace/capabilities (untrusted).
    (agent_dir / "capabilities").mkdir(exist_ok=True)
    (workspace / "capabilities").mkdir(exist_ok=True)

    # Only scaffold directories the runtime actually reads. Session transcripts
    # land in workspace/sessions/. Memory (workspace/memory/index.db + entities)
    # is created lazily by arcmemory when a Brain is selected, so it is not
    # pre-made here.
    (workspace / "sessions").mkdir(exist_ok=True)


def _print_scaffold_summary(display_name: str, agent_dir: Path, tier: str = "personal") -> None:
    """Print directory structure and next-steps after scaffold."""
    sys.stdout.write("\n")
    sys.stdout.write(f"Tier: {tier}\n")
    sys.stdout.write("\n")
    sys.stdout.write("Structure:\n")
    sys.stdout.write(f"  {display_name}/\n")
    sys.stdout.write("    arcagent.toml             # agent/tools/security/modules/store\n")
    sys.stdout.write("    arcllm.toml               # LLM-wire: [llm] / [eval] / [budget]\n")
    sys.stdout.write("    arcrun.toml               # agentic-loop controls\n")
    sys.stdout.write("    capabilities/             # per-agent capabilities (trusted)\n")
    sys.stdout.write("      calculator.py\n")
    sys.stdout.write("    workspace/\n")
    sys.stdout.write("      identity.md, policy.md, context.md\n")
    sys.stdout.write("      capabilities/          # agent-authored (UNTRUSTED, AST-validated)\n")
    sys.stdout.write("      sessions/              # chat transcripts (JSONL)\n")
    sys.stdout.write("      memory/                # lazily created when a Brain is enabled\n")
    sys.stdout.write("\n")
    sys.stdout.write("Next steps:\n")
    # --check, not a bare `build`: the config already exists, and `build`
    # refuses to regenerate over it without --force.
    sys.stdout.write(f"  arc agent build {agent_dir} --check\n")
    sys.stdout.write(f"  arc agent chat {agent_dir}\n")


# ---------------------------------------------------------------------------
# Capability scan roots (used by status/skills/extensions and chat)
# ---------------------------------------------------------------------------


def _capability_scan_roots(agent_dir: Path) -> list[tuple[str, Path]]:
    """Return the four user-visible capability scan roots in precedence order.

    Mirrors `arcagent.core.agent_lifecycle.setup_capabilities` (SPEC-021 R-001)
    but skips the package-internal builtins root, which the user never edits.
    """
    workspace = agent_dir / "workspace"
    return [
        ("global", arcagent.global_capabilities_root()),
        ("agent", agent_dir / "capabilities"),
        ("workspace", workspace / "capabilities"),
    ]


def _iter_capability_files(agent_dir: Path) -> list[tuple[str, Path]]:
    """Yield (root_name, .py path) for every capability file across roots."""
    out: list[tuple[str, Path]] = []
    for root_name, root in _capability_scan_roots(agent_dir):
        if not root.is_dir():
            continue
        for py_file in sorted(root.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            out.append((root_name, py_file))
    return out


def _iter_skill_folders(agent_dir: Path) -> list[tuple[str, Path]]:
    """Yield (root_name, folder) for every <root>/<name>/SKILL.md skill folder."""
    out: list[tuple[str, Path]] = []
    for root_name, root in _capability_scan_roots(agent_dir):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").exists():
                out.append((root_name, entry))
    return out


# ---------------------------------------------------------------------------
# Shared ArcAgent loader (used by run/serve/chat)
# ---------------------------------------------------------------------------


def _load_arcagent(agent_dir: Path) -> tuple[Any, Any, Path]:
    """Load ArcAgent from agent directory.

    Returns (ArcAgent instance, ArcAgentConfig, config_path).
    Exits 1 with a clear message if arcagent.toml is missing or
    ArcAgent / load_config cannot be imported.
    """
    config_path = agent_dir / "arcagent.toml"
    if not config_path.exists():
        sys.stderr.write(f"arc agent: no arcagent.toml in {agent_dir}\n")
        sys.exit(1)

    config = arcagent.load_config(config_path)
    arc_agent = arcagent.ArcAgent(config, config_path=config_path)
    return arc_agent, config, config_path


def _print_result_json(result: Any) -> None:
    """Serialize a ``RunResult`` (from ``collect``) to JSON and write to stdout."""
    data = {
        "content": result.content,
        "turns": result.turns,
        "tool_calls_made": result.tool_calls_made,
        "cost_usd": result.cost_usd,
    }
    sys.stdout.write(json.dumps(data, indent=2) + "\n")


# Re-export asyncio for convenience in subcommand modules that call asyncio.run.
__all__ = [
    "AGENT_TIERS",
    "_CALCULATOR_TOOL",
    "_DEFAULT_ARCLLM_CONFIG",
    "_DEFAULT_ARCRUN_CONFIG",
    "_DEFAULT_CONFIG",
    "_DEFAULT_CONTEXT",
    "_DEFAULT_IDENTITY",
    "_capability_scan_roots",
    "_default_policy",
    "_discover_runtime_tools",
    "_discover_tools",
    "_env_paths",
    "_iter_capability_files",
    "_iter_skill_folders",
    "_load_agent_config",
    "_load_agent_llm_config",
    "_load_arcagent",
    "_load_env",
    "_print_kv",
    "_print_result_json",
    "_print_scaffold_summary",
    "_print_table",
    "_resolve_agent_dir",
    "_scaffold_workspace",
    "asyncio",
    "render_agent_config",
]
