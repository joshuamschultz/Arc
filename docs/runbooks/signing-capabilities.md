# Signing a Gated Capability

> **Runbooks**  ·  Operate  ·  page 14 of 20  
> **For** Operators deploying and running Arc  
> [← Hardening](security/hardening.md)  ·  [Docs home](../README.md)  ·  [Staging module bundles →](staging-module-bundles.md)

An agent can write its own tools and skills. Arc does not let that code run
until a human operator approves it. This runbook is the approval procedure.

Use it when a capability shows up as gated, and you have decided the code is
safe to run.

---

## 1. What signing actually does

Approving a capability is **one action with three effects**. All three are
needed. Any one alone leaves the capability gated.

| # | Effect | Where it lands | Why the loader needs it |
|---|---|---|---|
| 1 | A detached signature over the artifact bytes | `<artifact>.arcsig` next to the file | Clears the signature floor at enterprise and federal. Proves the bytes did not change since you looked at them. |
| 2 | The signer's public key is pinned as a trusted capability-verification key | `[security.validators] trusted_keys` in the agent's `arcagent.toml` | Makes the signature *verifiable*. A signature with no pinned key is no floor at all, and the loader denies it. |
| 3 | The source hash is pinned, with your DID and a timestamp | `[[security.validators.approved]]` in the same file | Records that **you** approved **these exact bytes**. This is the TOFU pin. |

A signature proves integrity and attribution. It never proves authorization.
Effect 3 is where a human authorizes the code. Read
[The Security Model](../walkthrough/10-security-model.md) for the full model.

Both surfaces call the same function, `arcagent.sign_capability`. A browser
approval and a terminal approval are one code path.

**What the artifact is:**

| Kind | Signed artifact | Name the pin uses |
|---|---|---|
| Tool | the `.py` file | the file stem (`greet.py` → `greet`) |
| Skill | the folder's `SKILL.md` | the folder name, not the frontmatter name |

**Drift is a hard stop.** Edit an approved file by one byte and the signature
and the hash pin both stop matching. The capability returns to gated. Approve
it again after you review the change.

---

## 2. Before you start

You need three things.

- The **operator key** for this deployment, at `~/arc/state/operator`. There is no
  flag to supply a different identity. Only an operator-key holder can approve.
- Operator-key custody set to `in_process`. See
  [section 7](#7-the-vault_transit-limit) if it is not.
- The agent's directory under the deployment's `team/` folder.

Read the code first. You are authorizing a program to execute inside your
deployment. `arc trust list` prints the path of every gated artifact. Open it.

---

## 3. Procedure: the CLI

### Step 1. List what is gated

```bash
arc trust list
arc trust list --agent olivia
```

Run this from the deployment directory. `--agent` is optional when the team
holds exactly one agent. With more than one, the command stops and prints the
known agent ids.

The output is a table:

```
Name       Kind   Status        Signed  Hash          Path
greet      tool   new_sighting  no      9f2b1c4d7e01  /home/arc/team/olivia/workspace/capabilities/greet.py
```

`Status` is the loader's verdict. Section 5 explains each value.

### Step 2. Read the source

```bash
cat /home/arc/team/olivia/workspace/capabilities/greet.py
```

Do not skip this. Everything after this step is you saying the code is safe.

### Step 3. Approve

```bash
arc trust approve greet
arc trust approve greet --agent olivia
```

The command signs the artifact, pins the key, pins the hash, then re-scans and
reports the new verdict:

```
Approved greet on olivia — signed, key pinned, hash pinned; status now: loaded (approver did:arc:acme:operator/1a2b…).
```

If the capability is still gated after signing, the command says so and prints
the reason:

```
Still gated: tofu decision deny
```

### Step 4. Verify

```bash
arc trust list --all
```

`--all` includes loaded capabilities, so the approved one is still in the
table. Check that its `Status` is `loaded` and its `Signed` column reads `yes`.

The agent picks the capability up on its next capability reload or restart.

---

## 4. Procedure: arcui

Use this when you operate the fleet from the dashboard.

1. Open the dashboard and go to **Gated capabilities**.
2. Turn on **operator mode**. A viewer can read, but cannot approve.
3. Find the row. Each row shows the agent, the name, the kind, the verdict, the
   short hash, and the artifact path.
4. Click **Review source** on the row. The panel fetches the artifact text from
   `GET /api/trust/source` and checks it against the hash the row carries, so
   you can prove the text on screen is the text you are about to sign.
5. Read it.
6. Click **Approve**.

If the artifact changes while you are reading it, the panel says so and
**Approve** locks again. The unlock is tied to the hash of the text you were
shown, not to the fact that you opened the panel. This stops a file that was
swapped mid-review from being approved on a stale reading.

**The approve control stays unavailable until the source has been displayed.**
This is deliberate. You are authorizing code to execute. The control exists so
approval cannot be rubber-stamped from a list view. Reading the artifact is the
work; the click only records it.

Approval posts to `POST /api/trust/approve`. The server signs with the on-box
operator key, records your operator DID as the approver, and writes an audit
event for the mutation. **Disapprove** on the same row posts to
`POST /api/trust/disapprove`.

The chat socket cannot reach any of this. Approval is operator-authenticated
HTTP only, and it is registered as a tool on no registry. An agent can never
authorize its own code.

---

## 5. How to read a denial

`Status` in the CLI table, and the badge in the arcui row, is the loader's
verdict, unchanged. These are the values you will see.

| Status | What it means | What to do |
|---|---|---|
| `loaded` | The capability passed every gate and is registered. | Nothing. |
| `new_sighting` | The loader has never seen this name approved. This is the normal first state of new code at enterprise and federal. | Read the source, then `arc trust approve <name>`. |
| `deny` | TOFU refused. At enterprise and federal: the name is approved, but the current bytes do not match the approved hash, so the file changed after approval. **Treat this as tamper until proven otherwise.** At personal: the artifact is unsigned and `auto_run_agent_code` is off. | Diff the file against what you approved. If the change is legitimate, re-approve. If you did not expect a change, investigate before approving anything. At personal, approve it. |
| `unsigned` | The signature floor rejected the artifact. Two details appear under it, below. | See the two rows below. |
| `unsigned` + detail `missing or invalid signature` | The artifact has no `.arcsig` sidecar, or the sidecar does not verify. | Approve it. Signing writes the sidecar. If it was signed before, the file was edited after signing. |
| `unsigned` + detail `required but no pinned key` | A signature is required, but the agent has **no trusted verification key at all**. Nothing can pass. An unpinned floor is no floor, so the loader fails closed before it reads a single signature. | Approve any capability on this agent once. Approval pins the operator key. Also check that the agent's identity DID resolves, because the agent's own key is part of the same trusted set. |
| `invalid` | The artifact failed validation before the trust gate ran. For a tool, the AST validator rejected it (a blocked import, or a sandbox-escape pattern). For a skill, the `SKILL.md` folder is malformed. | Signing will **not** fix this. Fix the code or the folder. The detail names the failing rule. |
| `error` | The trust gate itself raised while evaluating. The file may be unreadable, or not valid UTF-8. | Read the detail. Check file permissions and encoding. Fail-closed is working as designed. |

Two verdicts are not a signing problem. `invalid` is a code problem, and
`error` is usually a file problem. Do not reach for `approve` on either.

---

## 6. Revocation

```bash
arc trust disapprove greet
arc trust disapprove greet --agent olivia
```

This is the exact inverse of approval. It removes the signature sidecar, the
hash pin, and the trusted key. The capability returns to gated.

The trusted key is removed **only when no other artifact under that agent still
carries a signature from it**. One operator key usually signs several
capabilities. Revoking one must not silently gate the rest.

**A pin can outlive its artifact.** Deleting a capability file never touched the
agent's config, so the hash pin and the trusted key stay behind. `disapprove`
still clears what it can reach by name:

```
Removed the hash pin for greet on olivia (artifact already deleted). Any trusted key it pinned could not be resolved from a missing artifact — check [security.validators] trusted_keys in /home/arc/team/olivia/arcagent.toml if it is now unused.
```

Do that check. A trust anchor that outlives the code it was minted for is a
standing risk.

Revocation is idempotent. An interrupted one is completed by running it again.

---

## 7. The `vault_transit` limit

**Capability signing needs the operator seed inside the signing process.** It
derives the verify key that gets pinned into the agent's config. Under
`custody = "vault_transit"` the seed lives in a vault, an HSM, or a notary, and
never enters the process.

Under that custody **both surfaces refuse, and nothing is signed.** This is
deliberate. Signing with any other key would pin a trust anchor the deployment
never authorized.

The CLI stops before touching anything:

```
arc trust: capability signing requires in-process operator-key custody, but this machine is configured custody=vault_transit — the operator seed never enters this process, so nothing was signed. Run the approval on a host holding the operator key with custody=in_process in ~/arc/config/arcagent.toml, or extend the notary transit to capability signing.
```

arcui returns HTTP 500 with the same cause and audits the refusal as a denied
mutation.

**What you do instead.** Pick one:

- Run the approval on a host that holds the operator seed with
  `custody = "in_process"` in that host's `~/arc/config/arcagent.toml`, then ship the
  resulting `.arcsig` sidecar and the updated `arcagent.toml` to the target
  deployment.
- Extend the notary transit to cover capability signing.

Custody is read from the **machine** `~/arc/config/arcagent.toml`, not from the
agent's config. The machine tier sets its default: a `federal` machine forces
`vault_transit` and rejects a weaker value, and an `enterprise` machine defaults
to `vault_transit` but may be set to `in_process`. So a federal signing host
cannot approve capabilities today. Plan for a separate approval host, or for
transit support.

---

## 8. What changes per tier

The tier is the agent's `[security] tier`. Signing is the same action at every
tier. What differs is what the loader demands.

| | Personal | Enterprise | Federal |
|---|---|---|---|
| Is a signature required? | No | **Yes** | **Yes** |
| Unsigned code | Loads only if `[security.validators] auto_run_agent_code = true` | Denied at the floor | Denied at the floor |
| Signed, never approved | Loads | `new_sighting`, waits for you | `new_sighting`, waits for you |
| Signed, approved, bytes match | Loads | Loads | Loads |
| Bytes drifted after approval | The signature stops verifying, so the artifact counts as unsigned again: `deny` unless `auto_run_agent_code` is on | `deny` | `deny` |
| Operator-key custody default | `in_process` | `vault_transit` (may be relaxed) | `vault_transit` (forced) |

Read the middle column pair carefully. **Federal is signed AND
operator-approved.** It is strictly stronger than enterprise, not a different
rule. A signature alone never gets code past federal, because an agent can sign
its own new tool.

At personal tier a signature is enough on its own. The loader verifies it
against the agent's own pinned identity key as well as any pinned operator key,
so an attacker who can write into the workspace still cannot forge one. This is
what makes a scaffolded agent work out of the box with no operator step.

---

## 9. Where the trust lives

| Thing | Path |
|---|---|
| The signature | `<artifact>.arcsig`, beside the artifact |
| The trusted keys and the hash pins | `[security.validators]` in `<agent>/arcagent.toml` |
| The operator key | `~/arc/state/operator` |
| The custody setting | `[security] custody` in `~/arc/config/arcagent.toml` |

`[security.validators]` sits at agent root, never inside the workspace. The
agent has no write access to it. Only an operator changes it, through the two
surfaces in this runbook.

Every sign, revoke, and refusal is an audit event carrying the operator DID, the
artifact path, and the source hash.

---

## See also

- [The Security Model](../walkthrough/10-security-model.md): the four pillars,
  and why the signature floor runs before the TOFU layer
- [Security reference](../reference/security.md): the config block, the pin
  format, and the full load pipeline
- [Hardening](security/hardening.md): the rest of the operator posture
