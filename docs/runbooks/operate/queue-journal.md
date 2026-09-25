# Durable model-call queue journal

The durable queue journal uses SQLite format **4**, encrypted job/control records,
and an independently custodied monotonic anchor. Its compressed authenticated indexes
bind each job to tenant, owner, state, update order, and call ID. A scoped page is
limited to 100 jobs. A cursor survives process restart and foreign-tenant changes;
a change to its own exact scope makes it stale and requires a fresh first page.

The journal file must remain a private regular file (`0600`) beneath an owned
private directory (`0700`). The anchor and record cipher are required at startup.
Missing or mismatched custody, an incomplete proof, rollback, or a corrupt row
returns `QueueStateUnavailableError` and stops queue operation. Startup does not
fall back to memory.

Older format files, including the source-only format 3 experiment, are refused
**before schema changes**. Do not delete, rename,
or recreate an old journal to clear the error: doing so can discard accepted
queue metadata or detach it from its anchor. Preserve the SQLite file together
with its WAL/SHM files and the matching external anchor state, then use an
explicit, reviewed offline export/import procedure before starting format 4.
There is no automatic format migration. A backup is valid only when its journal
root matches the externally held anchor; an older backup cannot roll that anchor
back. This source is local-only and no prior production format migration is
claimed.

On restart, the host must first prove the prior owner epoch is fenced and then
call durable recovery with that exact epoch and an opaque signed recovery proof
for each selected tenant. Missing tenant proofs stop recovery before the first
mutation. The host preflights every selected tenant proof, and the injected
recovery authority must verify a signed lease for the journal scope,
tenant, epoch, and `queue.recover` purpose, including expiry and revocation, in
the same authoritative compare-and-advance operation as the queue root. An
ordinary signed token check before the queue write is insufficient: takeover
could occur between that check and the root advance. Without this authority,
durable recovery refuses mutation and hosted startup must stay unready. Recovery
takes one authenticated snapshot of call IDs; it excludes calls accepted later
and compares each current
row version before changing it. Proven queued calls become `failed`; calls that
may have entered a provider become `outcome_unknown`. Recovery never sends a
provider request. Another owner's live calls are untouched. The journal alone
does not establish who may fence an epoch or invoke recovery; that authority
belongs to the host composition.

Each selected tenant is reconciled in batches of at most 100 calls. Proofs are
checked again in the atomic root advance for every batch. If a proof expires or
is revoked after preflight, earlier authorized batches remain terminal, the
current batch is refused, and a later recovery with a fresh proof resumes the
remaining calls. Recovery never rolls back an already anchored terminal result.

Run one recovery at a time per coordinator. Its authenticated ID snapshot is
ephemeral and expires after one hour; a later snapshot invalidates prior cursors.
Recovery must finish before the host reports queue readiness. In a local
10,000-call format 4 benchmark, admission took 26.15 seconds, the journal used
75,436 authenticated nodes and 66.4 MB, reopening took 0.20 seconds, and
recovery took 11.29 seconds with 100 authoritative root advances. These are
local fake-anchor timings, not a production broker service-level target. Plan
the startup readiness window around the measured deployment workload; neither
the library's journal open nor a generic health response proves durable
recovery is complete.
