# Staging Module Bundles Across an Air Gap

> **Runbooks**  ·  Operate  ·  page 15 of 20  
> **For** Operators deploying and running Arc  
> [← Signing capabilities](signing-capabilities.md)  ·  [Docs home](../README.md)  ·  [Threat model →](security/threat-model.md)

A module reaches a deployment as a signed bundle and no other way. This runbook is the
offline procedure: build the bundle on a connected **low-side** host, carry it on approved
media, and install it inside the **high-side** enclave.

No step needs a network. `arc module bundle` reads a source folder and writes a directory.
`arc module install --from` reads that directory and writes the module. Neither contacts a
package index, a registry, or any other host.

Use this when the target deployment has no route to the build host. **On a box that can
reach its own module sources, do not run these commands at all** — `arc install` builds,
signs, installs, and verifies every module the fleet's configs enable in one pass, for every
agent, and is idempotent. See [Bring-up](deploy/up.md). This runbook is the air-gapped
procedure, where the build and the install happen on two different machines.

---

## 1. What you are carrying

A bundle is a **directory**, conventionally named `<module>.arcbundle`:

```
web.arcbundle/
├── manifest.json     # module, version, issuer, and one SHA-256 per payload file
├── manifest.sig      # 64-byte detached Ed25519 signature over the manifest
└── files/            # the payload tree
```

The signature covers the manifest. The manifest covers every payload byte. So one 64-byte
signature is the whole integrity claim, and there is nothing in the tree it does not reach.

Two properties decide how you handle the media.

- **The bundle is not secret.** It is module source code with a signature. It needs integrity
  protection, not confidentiality.
- **The bundle is not trusted by being present.** Copying it into the enclave grants it
  nothing. It is verified at install time, against the high side's own trust settings.

---

## 2. Before you start

**On the low side** you need:

- A checkout of the module source catalog.
- An operator key on that host, at `${ARC_CONFIG_DIR:-~/.arc}/operator`. `arc module bundle`
  signs with it and stamps its DID into the manifest as the issuer.
- That operator signer configured for **Ed25519**. A bundle signature is Ed25519 by
  definition, and a signer offering another algorithm is refused before anything is written:

  ```
  bundle signatures are ed25519; signer offers 'ecdsa-p256'
  ```

  A machine whose `~/.arc/arcagent.toml` sets `[security] tier = "federal"` forces
  `ecdsa-p256`, so **a federal-configured host cannot build a bundle.** That is why the build
  runs on the low side.

**On the high side** you need:

- The deployment directory, with its `team/` folder and the target agent.
- A decision about the issuer. See section 5.

---

## 3. Low side — build the bundle

Point the CLI at the source catalog if you are building from a checkout rather than the
installed package:

```bash
export ARC_MODULE_SOURCE=~/Projects/arc/packages/arcagent/src/arcagent/modules
```

Build one module, or several:

```bash
arc module bundle web
arc module bundle web browser scheduler
```

Each named module becomes **its own bundle**. Three names produce three
`<module>.arcbundle` directories, not one combined file.

By default they land in the deployment bundle store,
`${ARC_CONFIG_DIR:-~/.arc}/bundles/`. Write them straight to the transfer media with `-o`:

```bash
arc module bundle web browser -o /media/transfer/bundles
```

The output reports each path and the issuer it signed for:

```
Built /media/transfer/bundles/web.arcbundle
Built /media/transfer/bundles/browser.arcbundle

Signed by did:arc:acme:operator/1a2b3c4d. Install with: arc module install web browser
```

**Record that issuer DID.** The high side has to trust it, and section 5 is where you say so.

Two flags matter:

| Flag | Effect |
|---|---|
| `-o <dir>`, `--out <dir>` | Output **directory**. Each bundle is created inside it. Default: the deployment bundle store. |
| `--force` | Replace a bundle of the same name that already exists. Without it, an existing target stops the command. |

Build artifacts are never packaged: `__pycache__`, `.DS_Store`, `.pyc`, and `.pyo` are
skipped, and symlinks are skipped rather than followed. That is what makes the payload
hashes reproducible.

---

## 4. Carry it

Copy the `<module>.arcbundle` directory onto approved media, whole. Preserve the directory
structure — `manifest.json`, `manifest.sig`, and the `files/` tree must arrive together and
unchanged.

If your media handling needs a single file, archive and extract with a tool that preserves
bytes exactly:

```bash
# low side
tar -cf web.arcbundle.tar -C /media/transfer/bundles web.arcbundle

# high side
tar -xf web.arcbundle.tar -C /srv/staging
```

Do not repack the payload by hand, do not reformat `manifest.json`, and do not edit a file
inside `files/`. Every one of those is a refusal at install time, by design. Section 6 shows
what each looks like.

---

## 5. High side — trust the issuer

The install verifies the manifest signature against the issuers this deployment accepts.
There are exactly two sources, and both are explicit.

**Source 1 — the on-box operator key.** The high side's own operator DID is always accepted.
If the low side and the high side hold the same operator key, the bundle verifies with no
further setup.

**Source 2 — `${ARC_CONFIG_DIR:-~/.arc}/trust/issuers.toml`.** For any other issuer, add its
entry:

```toml
[issuers."did:arc:acme:operator/1a2b3c4d"]
public_key = "BASE64_ENCODED_32_BYTE_ED25519_PUBKEY"
added_at = "2026-08-12T00:00:00Z"
role = "manifest-signer"
```

```bash
chmod 0600 ~/.arc/trust/issuers.toml
```

**The file must be `0600`.** Any group- or other-readable mode is refused with
`TRUST_STORE_INSECURE_PERMS`, because a federal deployment cannot silently accept a trust
anchor another user could have tampered with.

**There is no CLI that writes `issuers.toml` today.** Edit it directly, and carry the public
key across the gap the same way you carry the bundle — as something you verified out of band,
not as something the bundle told you.

### Adding an issuer key grants that issuer code execution

Be clear about what the entry above buys. A module bundle's capability surface — its tools,
hooks, background tasks, and `@capability` classes — is first-party code. Once the signature
verifies, that code is imported and executed **in this process, uncontained**: no AST import
allowlist, no isolated runner, no sandbox. That is deliberate. Containment exists to hold
code the *model* wrote; applying it to modules stops `@hook` / `@background_task` /
`@capability` from ever registering, which is the whole reason a module is delivered signed
instead of contained.

So the trust decision is the containment. Adding a key to `issuers.toml` means: *anything this
issuer signs, from now on, runs with the agent's full privileges.* It is the apt/npm model —
you vet the publisher once, out of band, rather than vetting every artifact. Treat it exactly
like adding a package repository's signing key to a production host:

- Do it deliberately, by hand, from a key you verified through a channel the bundle had no
  part in. Never from a DID a bundle, a chat message, or a model told you about.
- Add the smallest set of issuers the deployment actually needs. Every extra key is another
  party who can execute code on this box.
- Removing the entry is how you revoke. There is no separate revocation list — an issuer with
  no key produces no accepted bundles.

This is why nothing in Arc writes this file for you, and why no tool, chat socket, or agent can
reach it. The manual edit **is** the authorization step.

A name is not a permission. The install reads the issuer the bundle *claims* only in order to
look up a key for it. An unknown name resolves to no key and the bundle is refused. A known
name still has to produce that issuer's signature.

If the trust store itself is unusable — bad permissions, broken TOML — the issuer is simply
left out of the accepted set, so the bundle is refused rather than admitted. The command
still prints the cause, so you can tell a permissions problem from a policy decision:

```
arc module: trust store unusable for issuer 'did:arc:acme:operator/1a2b3c4d' — ...
```

---

## 6. High side — install

```bash
arc module install --from /srv/staging/web.arcbundle --agent olivia
```

`--from` takes the **bundle directory**, and installs exactly that one bundle. Do not also
name modules on the same command line; naming both is an error.

`--agent` names the agent the module is enabled for. It is optional when the deployment holds
exactly one agent. With more than one, the command stops and prints the known ids. There is
no "all agents" default: silently enabling a capability on an agent you did not name is the
excessive agency this whole path exists to close.

A successful install reports what landed:

```
Installed web 0.16.0 (issuer did:arc:acme:operator/1a2b3c4d, tier federal) — runtime /home/arc/.arc/modules/web, enabled for olivia.
```

One command did four things, in this order:

1. **Verified** the signature, the manifest's canonical form, every declared file hash, and
   that the payload carries nothing undeclared.
2. **Materialized** the runtime to `${ARC_CONFIG_DIR:-~/.arc}/modules/web`, `0444` files
   inside `0555` directories, outside every agent's tool fence.
3. **Copied** the capability surface — `capabilities.py` and `skills/` — into
   `team/olivia/capabilities/modules/web/`, which is the ONLY place the loader reads a
   module's tools and skills from. `_runtime.py` is never copied.
4. **Enabled** it, by writing `[modules.web] enabled = true` into the agent's
   `arcagent.toml`.

Step 4 is not a convenience. A materialized module that no config enables is a capability
sitting on disk doing nothing, which is the exact ambiguity signed distribution removes.

### The tier is read, never passed

No flag selects the stringency a bundle is verified at. It is the **stricter** of two
configured tiers:

- the machine's `${ARC_CONFIG_DIR:-~/.arc}/arcagent.toml` `[security] tier`
- the target agent's `<agent_dir>/arcagent.toml` `[security] tier`

So a federal agent cannot be handed a weakly-signed module by running the install from a
machine whose config says personal. A config file that exists but cannot be parsed stops the
command rather than defaulting: guessing `personal` for an unreadable federal config would
verify a bundle on a box that forbids it.

### Verify what landed

```bash
arc module list --agent olivia
```

```
Module      Bundled  Installed  Enabled (olivia)
web         -        yes        yes

module root: /home/arc/.arc/modules
bundle store: /home/arc/.arc/bundles
```

`Bundled` reads `-` here because the bundle is on staging media, not in this deployment's
bundle store. That is expected for the air-gapped path.

The agent picks the module up on its next restart.

---

## 7. How to read a refusal

Every refusal has the same shape and the same consequence:

```
arc module install: refused /srv/staging/web.arcbundle — <reason>
Nothing was installed.
```

**That last line is literal.** Verification never writes, creates, or removes anything, so a
refusal cannot leave a partial tree — there was nothing written to leave behind. The
deployment is byte-identical to its state before you ran the command. This holds for a batch
too: every bundle in the request is verified before any of them is materialized, so one bad
bundle in a set of five installs none of the five.

| Reason | What happened | What to do |
|---|---|---|
| `issuer '<did>' is not trusted at '<tier>' tier` | The claimed issuer has no key in the accepted set. | Go back to section 5. Confirm the DID matches what the low side printed, and that `issuers.toml` is present and `0600`. |
| `manifest signature does not verify for issuer '<did>'` | A key was found, but it does not match the signature. | The wrong key is pinned for that DID, or the manifest changed after signing. Re-check the public key out of band. **Treat an unexplained mismatch as tampering.** |
| `manifest.json is not in canonical form` | The signed bytes verify, but the file is not in the one canonical spelling. | Something re-serialized the manifest in transit. Re-copy from the low side without editing or reformatting it. |
| `payload file '<path>' hashes to <a>, manifest declares <b>` | A payload file's bytes changed. | Media corruption or tampering. Re-copy and re-run. If it repeats, stop and investigate the media. |
| `declared payload file '<path>' is not a file` | A declared file is missing or is not a regular file. | The copy was incomplete. Re-copy the whole directory. |
| `payload carries undeclared file '<path>'` | The tree holds something the manifest never declared. | Unverified code riding along with verified code. Never "clean it up and retry" — find out how it got there. |
| `payload file '<path>' escapes the payload root` | A path resolves outside the payload tree. | A traversal attempt. Do not install. Escalate. |
| `no bundle directory at <path>` | `--from` was given a path that is not a directory. | Point it at the `<module>.arcbundle` directory itself, not at an archive or a file inside it. |

Refusals are audited as they are decided, not as they are reported:
`module.signature_invalid` and `module.content_hash_mismatch` name the bundle, the module,
and the issuer. A successful verify emits `module.bundle.verified`, and the write emits
`module.installed`. Every surface that installs a bundle records the same facts.

---

## 8. Installing several bundles at once

Stage them into the deployment bundle store and install by name:

```bash
cp -r /srv/staging/*.arcbundle ~/.arc/bundles/
arc module install web browser scheduler --agent olivia
```

Or install everything staged:

```bash
arc module install --all --agent olivia
```

`--all` is the one place the batch behavior differs. A bundle this deployment is **not
permitted** to install is skipped and reported, rather than failing the whole command:

```
Skipped browser.arcbundle: issuer 'did:arc:other:operator/9f8e7d6c' is not trusted at 'federal' tier
```

That leniency covers **signature refusals only** — an untrusted issuer, or a tier that
forbids the signer. A content-hash mismatch or a malformed manifest is tampering, not policy,
and stops the command at every invocation including `--all`.

---

## 9. Removing a module

```bash
arc module remove web --agent olivia
```

The exact inverse of install: the runtime tree at the deployment root, the agent's capability
copies, and the `[modules.web]` config entry. All three are attempted even when one fails,
because stopping at the first would leave a half-removed module whose remaining half is still
loadable. Any failure exits non-zero and names each part that survived.

```
Removed web from olivia: runtime, capabilities, config entry.
```

---

## 10. Where everything lives

| Thing | Path |
|---|---|
| Module source catalog (low side) | `arcagent/modules/`, or `$ARC_MODULE_SOURCE` |
| Staged bundles | `${ARC_CONFIG_DIR:-~/.arc}/bundles/` |
| Installed module runtime | `${ARC_CONFIG_DIR:-~/.arc}/modules/<name>/` — `0444` in `0555` |
| Per-agent capability copies | `<agent_dir>/capabilities/modules/<name>/` |
| Trusted bundle issuers | `${ARC_CONFIG_DIR:-~/.arc}/trust/issuers.toml` — `0600` |
| Operator key | `${ARC_CONFIG_DIR:-~/.arc}/operator` |
| Activation | `[modules.<name>]` in `<agent_dir>/arcagent.toml` |

`${ARC_CONFIG_DIR}` scopes all of it. An isolated deployment never reaches into the invoking
user's real `~/.arc` for the key that decides what installs.

---

## See also

- [Writing modules](../building/modules.md): the bundle format and the three filesystem
  locations, from the author's side
- [Signing a gated capability](signing-capabilities.md): approving code an agent wrote for
  itself, which is a different trust decision with a different procedure
- [Hardening](security/hardening.md): the rest of the operator posture
- [The Security Model](../walkthrough/10-security-model.md): why signing, authorizing, and
  auditing are separate pillars
