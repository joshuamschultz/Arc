# Sales-Exec Assistant — Blueprints v2, Tool Attachment, Guided Onboarding

**Date:** 2026-07-26 · **Branch:** `feat/blueprints` · **Status:** brainstorm, pre-spec

Companion to [`rapport-agentic-crm.md`](./rapport-agentic-crm.md) (the *code* layer for
revenue memory) and [`2026-07-21-editable-system-prompts.md`](./2026-07-21-editable-system-prompts.md)
(arcprompt, which postdates the blueprint system and was never wired into it).

---

## 1. Thesis

> A non-technical sales exec clicks a link, picks **"Sales Exec Assistant"**, answers eight
> questions, and five minutes later has a running agent in the cloud that remembers his
> deals, reaches him on Telegram, and acts on his tools.
>
> And a BlackArc engineer — or a client's admin — can author that blueprint **by configuring
> one agent by hand**, not by writing code.

Two audiences, one artifact. The blueprint is the unit of packaged expertise: it is how a
domain (sales, research, ops) gets encoded once and deployed a thousand times.

## 2. The layering ruling (the constraint everything else obeys)

**A blueprint is configuration. It contains no code, and nothing in it is baked into
`arcllm` / `arcrun` / `arcmemory` / `arcagent`.** It is a file you drop in, and the agent
becomes a specialist.

| Layer | Artifact | Signed by | Lives in |
|---|---|---|---|
| Config | `arcagent.toml`, `arcllm.toml`, `arcrun.toml` | n/a (plain TOML) | agent dir |
| Prompts | arcprompt overlays (`context/<pkg>/<name>.md`) | **operator key**, `.arcsig` | agent dir |
| Persona | `workspace/identity.md` | unsigned (agent-editable) | workspace |
| Skills | arcskill bundles | operator/author key | skills root |
| Verbs (code) | `@tool` capability drop-ins | operator key, `.arcsig` | `~/.arc/capabilities/` |

The blueprint declares the first four. **The fifth is out of scope for a blueprint** — new
verbs mean a package (that is what `rapport-arc` is). If a blueprint ever needs to ship a
`.py`, that is the signal it should have been a plugin.

This is what makes the sales blueprint shippable *now*: tuning what the agent **remembers**
is a prompt-override problem, not a code problem.

## 3. Current state (verified in-tree)

**Blueprints exist — SPEC-047.** `packages/arcagent/src/arcagent/blueprints/`:
`personal-assistant`, `enterprise-ops`, `federal-analyst`. CLI: `arc blueprint list/show/apply/sign`.

Two properties worth preserving exactly as-is:

- **Tier is stringency-max** (`loader.py:147`) — a blueprint can only *raise* a floor.
  A client blueprint can never downgrade a federal deployment.
- **Denylist blocks trust-root hijack** (`loader.py:60`) — a blueprint may not set
  `vault.backend`, `identity.key_dir`, `security.operator_key_dir`, `notary_keystore`,
  or `witness_medium_path`.

**arcprompt exists** — 31 stock prompts across `arcagent` (9), `arcrun` (9), `arcmemory` (7),
`arcskill` (6). Overlays live at `<agent_root>/context/<package>/<name>.md` + `.arcsig`,
pinned to the operator key, re-verified at run start. The reference write envelope is
`arcui.routes.agent_detail.prompts._author_signed_overlay`:
**secret-scan → policy-gate → confine → operator-sign → write → audit.**

**Config is three files** — `arcagent.toml` (everything except LLM-wire), `arcllm.toml`
(`[llm]`/`[eval]`/`[budget]`), `arcrun.toml` (loop controls). `render_agent_config(name, tier, did)`
renders the full surface at a tier.

**Gateway adapters are entry-point packages** — `packages/arcgateway-{telegram,slack,mattermost}`.
Core ships only `web`. Adding a platform is a new package, never a core edit.

**Deployment exists** — one Docker image, compose, Caddy TLS, cloud-init.

## 4. Gaps

| # | Gap | Severity |
|---|---|---|
| G1 | `apply` writes **only `arcagent.toml`** — can't set model, budget, or `max_turns` | blocks |
| G2 | **No prompt overlays in blueprints** — arcprompt postdates SPEC-047 | blocks |
| G3 | No `identity.md` / persona in a blueprint | blocks |
| G4 | No skills declared by a blueprint | blocks |
| G5 | **No authoring workflow** — blueprints are hand-written TOML | blocks #2 |
| G6 | No connector/tool attachment model | blocks #3 |
| G7 | **MCP is an enum and nothing else** (`tools/_transport.py:31`) — zero dispatch | blocks #3 |
| G8 | Container entrypoint hardcodes `ANTHROPIC_API_KEY` | blocks provider choice |
| G9 | No machine surface for "onboarding answers → tomls" | blocks #7 |

## 5. Blueprint v2 format

Additive. The three shipped blueprints keep working unchanged (top level = `arcagent.toml`).

```toml
[blueprint]
name = "sales-exec-assistant"
version = "1.0.0"
tier = "personal"
description = "Relationship, pipeline, and commitment memory for a sales executive."
author = "blackarc"

# ── top level → arcagent.toml (unchanged semantics) ──
[modules.memory]
enabled = true
[modules.memory.config]
brain = "arcmemory"
[modules.tasks]          # commitments become durable tasks (SPEC-056)
enabled = true
[modules.scheduler]      # daily follow-up sweep
enabled = true

# ── NEW: sibling config files ──
[arcllm.llm]
model = "anthropic/claude-sonnet-5"
[arcllm.budget]
max_cost_usd = 2.00        # runaway guardrail — protects the customer's own key
[arcrun]
max_turns = 40

# ── NEW: persona (unsigned, customer- and agent-editable) ──
[identity]
body = """
You are a sales executive's chief of staff...
"""

# ── NEW: signed prompt overlays ──
[[prompts]]
package = "arcmemory"
name    = "distill_fact"
body    = """..."""

# ── NEW: skills to preload ──
[[skills]]
name   = "pre-call-brief"
source = "packaged"        # packaged | hub | path

# ── NEW: what onboarding must ask (see §10) ──
[[questions]]
id       = "llm_provider"
prompt   = "Which model provider?"
type     = "choice"
choices  = ["anthropic", "openai"]
sets     = "arcllm.llm.model"
```

**Validation rules — all fail loud at apply:**

- Unknown `(package, name)` in `[[prompts]]` → **hard error.** `load_stock_document` already
  raises `PromptMissing`; apply must not swallow it. A typo'd override that silently
  no-ops is the producers-unwired failure mode this repo keeps paying for.
- `[[prompts]].body` runs the **same secret scan** as the arcui editor.
- Unknown `[[skills]]` → hard error.
- Denylist extends to the new sections; `[arcllm]`/`[arcrun]` get their own denied paths.

**Apply pipeline**, per artifact class:

1. `arcagent.toml` ← deep-merge under user values, tier = stringency-max *(exists)*
2. `arcllm.toml` / `arcrun.toml` ← same merge discipline *(new)*
3. `identity.md` ← write if absent; **never clobber** a customer-edited persona *(new)*
4. `[[prompts]]` ← author + **sign with the VM's own operator key** *(new, reuses arccli's
   `_confine` / `_operator_signer` / `find_secret`)*
5. `[[skills]]` ← arcskill install + verify *(new)*

Signing on the customer's VM with *its* operator key means overlays verify at run start and
**no key is ever distributed**.

## 6. Authoring blueprints (requirement #2)

Hand-writing a blueprint means hand-writing TOML plus up to 31 markdown bodies. Nobody will
do that twice. The authoring flow inverts it:

```
arc blueprint capture <agent-dir> --name sales-exec-assistant
```

Configure **one real agent** until it behaves right — `arc prompt edit`, tune the tomls,
write `identity.md`, install skills — then snapshot it. Capture diffs the agent against
stock and emits only what differs.

This is how Josh authors the sales blueprint personally: by using an agent, not by writing config.

**Two provenance classes** (already how the loader thinks):

- **Packaged / BlackArc** — ships in the wheel, provenance-trusted, no `.arcsig` needed.
- **User / client** — `~/.arc/blueprints/`, must be **operator-signed**; an unsigned or
  wrong-key preset reads as unsigned and is refused at apply.

Distribution beyond the wheel (a hub) is deliberately deferred — SkillVault is a separate
repo and blueprint sharing should ride that rail, not grow a second one.

## 6a. The extensibility spine (the actual product thesis)

> **Anyone can turn an Arc agent into anything — quickly, non-technically.**

That only works if adding a capability is *configuration*, not engineering. Arc already has
four extension surfaces; the ruling is **which one absorbs new work by default**:

| Surface | Adds | Cost to add one | Who can do it |
|---|---|---|---|
| **MCP server** | N tools at once | a URL + a credential | **non-technical** |
| Capability drop-in | one verb | a signed `.py` | developer |
| Skill (arcskill) | know-how, not verbs | a bundle | power user |
| Gateway adapter | a new inbound surface | a package + entry point | developer |
| **Blueprint** | *all of the above, as one file* | `arc blueprint capture` | **admin** |

**MCP is the default. Everything else is the exception.** A capability drop-in is what you
write when no MCP server exists; a gateway adapter is what you write when a new *human*
surface appears (iOS). Neither should be the path for "I want my agent to use Notion."

And **blueprint is the distribution unit** — it bundles MCP server declarations, skills,
prompt overlays, persona, and config into one signed, shareable file. That is the shape of
"turn arcagent into anything": *MCP supplies the verbs, skills supply the know-how,
blueprints package the whole thing so someone else can have it in five minutes.*

## 7. Tool attachment (requirement #3)

**The ruling: MCP is the spine.** Do not hand-write twelve integrations. The requested list
splits three ways, and only one of them is genuinely new work.

| Tool | Surface | Status |
|---|---|---|
| Telegram, Slack, Mattermost | **Gateway adapter** (inbound: where the human talks) | exists |
| iOS | Gateway adapter package | later, same pattern |
| Gmail, GCal, Notion, Jira, Confluence, GitHub | **MCP** (outbound: what the agent acts on) | **G7 — unwired** |
| Word, PowerPoint, Excel | **Local skills** (docx/pptx/xlsx) — file manipulation, not a network connector | skill work |

Two distinct directions that get conflated:

- **Inbound** = gateway adapters. One agent, many conversational surfaces, single pane.
  Already an entry-point plugin pattern; iOS slots in here later without core changes.
- **Outbound** = tools the agent calls. This is what "attach Notion" means.

**MCP is the leverage point, and it is cheaper than it looks.** The tool registry already
*models* the transport — `ToolTransport` declares `NATIVE | MCP | HTTP | PROCESS`
(`arcagent/tools/_transport.py:31`). MCP is not a missing abstraction, it is an **unfinished
one**: the enum, config surface, and docstrings exist; nothing dispatches. The work is
finishing a transport the registry was designed for, not bolting on a new subsystem.

Wire dispatch **once** and every one of those six becomes *configuration* — a server URL and
a credential. Without it, each is a hand-written capability, and twelve integrations become
twelve maintenance burdens.

**What a blueprint declares:**

```toml
[[mcp_servers]]
name      = "notion"
transport = "stdio"              # stdio | http
command   = "npx -y @notionhq/notion-mcp-server"
secret    = "NOTION_API_KEY"     # resolved from vault/env — never inlined
tools     = ["search", "get_page", "create_page"]   # allowlist, not "*"
```

**Least privilege is not optional here.** An MCP server exposes whatever it wants; a
blueprint must name the tools it actually needs (ASI02 tool misuse, LLM06 excessive agency).
`tools = ["*"]` should be refused outside personal tier. Every dispatched MCP call still
rides the existing envelope — signed `ToolCall`, `caller_did`, policy pipeline, audit —
because it enters through the same registry, which is exactly why finishing the transport
beats a side-channel.

**Credentials:** OAuth-per-user for Google/Notion/Atlassian; API tokens for GitHub/Jira.
Either way they land in the vault / env (`~/.arc/secrets/{name}`, 0600) — **never** in a
toml, never in a blueprint. A blueprint declares *which* connector it wants; onboarding
supplies the secret.

**This is where the Lethal Trifecta gets real.** A sales agent reads private email (private
data), talks to Slack and Gmail (external comms), and ingests inbound email from strangers
(untrusted input). That is the textbook trifecta, and it arrives the moment we attach these
tools. SPEC-057's context-resolved gate plus SPEC-035's mechanical operator approval
(pending row → `arc approve` → signed operator grant) are the designed answer. **The
blueprint must not be able to weaken it** — the denylist and tier stringency-max are what
guarantee that, and both already hold.

## 8. Sales memory tuning (requirement #4)

Config-only. Override the arcmemory distillation prompts so the agent's *lens* is revenue.

**Entity model:** contacts · companies · deals · meetings · commitments · tasks.

| Prompt | Retune to |
|---|---|
| `arcmemory/distill_fact` | extract contacts, companies, deals, roles, next steps — not generic entities |
| `arcmemory/distill_insight` | patterns across accounts ("champion went quiet before every slip") |
| `arcmemory/distill_procedure` | reusable sales motions (discovery → POC → procurement) |
| `arcmemory/distill_day` | a pipeline-shaped daily brief, not a diary |
| `arcmemory/distill_disambiguate` | same-person-different-company resolution (constant in sales) |
| `arcagent/context_maintainer_system` | `context.md` becomes a pipeline cockpit: open deals, owed follow-ups |
| `arcagent/planner_system` | decompose "prep for the Acme QBR" into real steps |

**Relationship to Rapport:** the blueprint is the **config-only sales lens available now**;
Rapport is the **code layer** (cards, bidirectional links, `crm_*` verbs) later. They
coexist by design — the rapport doc already resolved that the same session may distill into
both, each keeping its own lens. Ship the blueprint; it is the 80% that needs no package.

## 9. Preloaded skills (requirement #5)

Candidates: `pre-call-brief`, `follow-up-sweep`, `deal-review`, `meeting-notes-to-crm`,
`email-draft`, `qbr-prep`.

**Trust gotcha, from the scaffold's own behavior:** at personal tier TOFU denies any
agent-writable capability that is not signed by the agent's own pinned key unless
`auto_run_agent_code` is set. `arc agent create` already signs the scaffolded
`calculator.py` for exactly this reason. Blueprint-installed skills must be signed the same
way at apply time — **or they are dead on arrival and the customer sees a silent
non-feature.**

## 10. Guided onboarding (requirement #7)

**The elegant bit: the blueprint declares its own questions.** A `[[questions]]` block means
any front-end can render the interview for any blueprint, and adding a blueprint never means
touching a UI.

```
web form  ─┐
Telegram  ─┤
Slack     ─┼─→  answers.json  ─→  arc blueprint apply --answers answers.json  ─→  3 tomls
arctui    ─┤                          (arccli = the single application surface)
iOS       ─┘
```

One machine-readable answer schema; many surfaces. `arccli` stays the only thing that writes
config, so a non-technical web questionnaire and a developer's terminal converge on the same
code path — and the web path is testable by running the CLI.

**Question types needed:** provider choice (→ `arcllm.toml`, and the API key → env, per G8's
fix), agent name and persona, Telegram bot token + the operator's own Telegram user ID
(**`allowed_user_ids = []` means deny-all, fail-closed — omit it and the customer is locked
out of his own agent**), tier, connector selection + credentials.

## 11. Phasing

| Phase | Work | Blocked by |
|---|---|---|
| **1 — this branch** | Blueprint v2 (G1–G5), `capture`, `[[questions]]` schema | nothing |
| **2** | **MCP transport dispatch (G7)** + `[[mcp_servers]]` in blueprints | nothing |
| **3** | Sales blueprint authored by hand via `capture`; office-file skills | 1–2 |
| **4** | Site + wizard + provisioner + Stripe | registry push (credentials) |
| **5** | iOS gateway adapter; `rapport-arc` code verbs | 1–2 |

Phases 1–3 need **no credentials and no external accounts** — pure in-repo work, can start
immediately and in parallel with account setup.

**Why MCP moved ahead of authoring the sales blueprint:** a blueprint captured before the
agent can reach Gmail/Notion/Jira captures a half-agent, and would have to be re-captured
after. Finish the verbs, then snapshot the specialist.

## 12. Open questions

1. **MCP now or hand-rolled connectors first?** Wiring MCP is bigger up front but converts
   six integrations into config. Recommendation: MCP first, in Phase 2 — but it is a real
   fork worth deciding deliberately.
2. **Where does the questionnaire's API-key answer live in transit?** It must reach the VM's
   env without ever being written to a blueprint, a toml, or a log. cloud-init writes a 0600
   file today; is that sufficient for enterprise clients, or does this need vault-from-the-start?
3. **Client-authored blueprints and signing** — a client admin authoring a blueprint signs it
   with *their* operator key. Does BlackArc counter-sign for distribution, or is provenance
   purely local?
4. **Skill provenance across the fleet** — BlackArc-signed skills pinned to a BlackArc key,
   or re-signed per deployment by the local operator? The second is simpler and matches how
   overlays already work.
5. **How much does `capture` capture?** Everything that differs from stock risks snapshotting
   accidental local state (a stray memory file, a test skill). Probably needs an explicit
   allowlist of capturable artifact classes.

## 13. Anti-goals

- **No code in blueprints.** A blueprint that ships a `.py` is a plugin wearing a costume.
- **No new config file family.** Three tomls plus overlays is the surface; a blueprint
  composes them, it does not invent a fourth.
- **No bypass of the trifecta gate, tier floor, or denylist** — not for convenience, not for
  onboarding, not for a demo.
- **No blueprint-specific UI.** If a front-end needs to know a blueprint's name to render its
  interview, the `[[questions]]` schema has failed.
- **No second distribution rail.** Blueprint sharing rides SkillVault when it exists.
