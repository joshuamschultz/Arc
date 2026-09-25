# Durable model-call queue journal

The durable queue journal uses SQLite format **3**, encrypted job/control records,
and an independently custodied monotonic anchor. Its authenticated ordered indexes
bind each job to tenant, owner, state, update order, and call ID. A scoped page is
limited to 100 jobs. A cursor survives process restart and foreign-tenant changes;
a change to its own exact scope makes it stale and requires a fresh first page.

The journal file must remain a private regular file (`0600`) beneath an owned
private directory (`0700`). The anchor and record cipher are required at startup.
Missing or mismatched custody, an incomplete proof, rollback, or a corrupt row
returns `QueueStateUnavailableError` and stops queue operation. Startup does not
fall back to memory.

Format 1/2 files are refused **before schema changes**. Do not delete, rename,
or recreate an old journal to clear the error: doing so can discard accepted
queue metadata or detach it from its anchor. Preserve the SQLite file together
with its WAL/SHM files and the matching external anchor state, then use an
explicit, reviewed offline export/import procedure before starting format 3.
There is no automatic format migration. A backup is valid only when its journal
root matches the externally held anchor; an older backup cannot roll that anchor
back. This source is local-only and no prior production format migration is
claimed.

On restart, the host must first prove the prior owner epoch is fenced and then
call durable recovery with that exact epoch and an opaque recovery proof. The
injected recovery authority must verify a signed lease for the journal scope,
tenant, epoch, and `queue.recover` purpose, including expiry and revocation, in
the same authoritative compare-and-advance operation as the queue root. An
ordinary signed token check before the queue write is insufficient: takeover
could occur between that check and the root advance. Without this authority,
durable recovery refuses mutation and hosted startup must stay unready. Recovery
takes one authenticated snapshot of call IDs; it excludes calls accepted later and compares each current
row version before changing it. Proven queued calls become `failed`; calls that
may have entered a provider become `outcome_unknown`. Recovery never sends a
provider request. Another owner's live calls are untouched. The journal alone
does not establish who may fence an epoch or invoke recovery; that authority
belongs to the host composition.

Run one recovery at a time per coordinator. Its authenticated ID snapshot is
ephemeral and expires after one hour; a later snapshot invalidates prior cursors.
Recovery must finish before the host reports queue readiness. A local 10,000-call
benchmark took about 12 seconds to reopen the journal and 341 seconds to
reconcile all calls. Plan the startup readiness window and queue capacity around
the measured deployment workload; neither the library's journal open nor a
generic health response proves durable recovery is complete.
