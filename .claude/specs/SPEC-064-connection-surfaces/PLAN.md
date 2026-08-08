# SPEC-064 — Connection surfaces: keys and connectors from CLI, TUI, and web

**Status**: PENDING

## Why

SPEC-062 built the connector mechanism and one surface for it (`arc connector`, eight
verbs). Two things are still missing before a person can actually use it:

1. **Nothing writes a provider API key.** `arc init` prints
   `echo 'ANTHROPIC_API_KEY=sk-...' >> ~/.arc/.env` and leaves. There is no verb, no
   panel, no store — the operator hand-edits a dotfile.
2. **The web and the terminal UI cannot connect anything.** `arc connector add` is
   the only door, and it prompts with `getpass`, which a Textual screen and an HTTP
   request both cannot drive.

Bundles are also undiscoverable: `extensions_root` is one directory
(`<agent>/extensions`), so the eight shipped bundles reach an agent only if somebody
copies them there per agent.

## Scope

| # | Deliverable |
|---|-------------|
| A | Provider keys have a store, and three surfaces write to it |
| B | Bundles resolve from a search path, and a surface can list what is available |
| C | The web can add, probe, doctor, approve, and remove a connection |
| D | The terminal UI can do the same without `getpass` |

## Decisions

- **D-581 — arcllm declares which env var a provider reads; nothing else may.**
  `providers/*.toml` already carries `api_key_env`. `arccli.commands.init._PROVIDER_ENV_VARS`
  is a second copy of that map and is deleted. A new `arcllm.list_provider_keys()`
  is the one reader.
- **D-582 — the provider-key store is one file, `~/.arc/.env`, written the way
  connector credentials are.** `LocalFileSecretBackend`'s recipe (0600 from creation,
  owner + mode check on read, private temp file, `os.replace`) becomes an `EnvFile`
  primitive that both stores use. One recipe, not two.
- **D-583 — a key value is write-only across every surface.** `list` answers
  `present: true|false` and never a value or a prefix. There is no read verb and no
  GET that returns one. A key set in the web is never echoed back.
- **D-584 — bundles resolve from an ordered search path**, `<agent>/extensions`, then
  `$ARC_EXTENSIONS_ROOT`, then `~/.arc/extensions`. First hit wins, containment is
  checked per root. An agent-local bundle overrides a fleet one by name rather than
  hiding the whole fleet.
- **D-585 — the web collects credentials over the same operator gate that edits
  config, and audits them as coordinates.** Operator role required, body capped,
  value never logged, never returned, never in an error message. The install
  sequence is `arcagent.modules.connectors.install` — the web re-derives nothing.
- **D-586 — the TUI drives the same install module through a modal, not the CLI
  handler.** `arc connector add` is `getpass`-shaped by design (D-555); a Textual
  screen collects into masked inputs and calls `install_connector` directly.

## Tasks

### Phase 1 — Foundation

- [ ] T-001 `arcllm.list_provider_keys()` — name, env var, whether a key is required,
      from the packaged `providers/*.toml`. Public export.
- [ ] T-002 Extract `EnvFile` from `LocalFileSecretBackend`; the backend uses it.
- [ ] T-003 `arcagent.keys.KeyStore` over `~/.arc/.env` — `list`/`set`/`delete`,
      audited, refuses an env var no provider declares.
- [ ] T-004 Delete `_PROVIDER_ENV_VARS` from `arccli.commands.init`; read arcllm.
- [ ] T-005 `ExtensionCatalog` takes `roots`; add `available()`; add
      `resolve_extension_roots(agent_dir)`.
- [ ] T-006 `description` on `ExtensionHeader`; the eight bundles fill it in.

### Phase 2 — CLI

- [ ] T-010 `arc keys list|set|remove`.
- [ ] T-011 `arc connector available`.

### Phase 3 — Web

- [ ] T-020 `/api/keys` GET, PUT, DELETE.
- [ ] T-021 `/api/connectors/catalog`, and the agent-scoped connector verbs.
- [ ] T-022 Connections page + a Keys section in Settings.

### Phase 4 — TUI

- [ ] T-030 `/connect` — pick a bundle, name the instance, fill masked fields, probe.

### Phase 5 — Prove and ship

- [ ] T-040 One test per surface that goes through the real install path.
- [ ] T-041 Full gate, merge, deploy, live-verify on the DGX.
