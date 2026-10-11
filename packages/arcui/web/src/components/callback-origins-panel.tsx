import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ErrorState, LoadingRows } from '@/components/states'
import {
  CLEARANCES,
  callbackRefusal,
  useAddCallback,
  useCallbacks,
  useRemoveCallback,
  type CallbackEntry,
} from '@/lib/callbacks'

const approvalHref = (approvalId: string) => `/approvals#${encodeURIComponent(approvalId)}`

const SELECT_CLASS =
  'h-9 rounded-md border border-input bg-transparent px-3 text-sm shadow-xs outline-none hover:border-muted-foreground/40 focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/60 dark:bg-input/30'

const STATUS_CLASS: Record<string, string> = {
  active:
    'border-emerald-500/30 bg-emerald-500/10 font-medium text-emerald-700 dark:text-emerald-400',
  pending: 'border-amber-500/30 bg-amber-500/10 font-medium text-amber-700 dark:text-amber-400',
  removed: 'border-border bg-muted/40 text-muted-foreground',
}

function AddOriginForm({ onPending }: { onPending: (approvalId: string) => void }) {
  const add = useAddCallback()
  const [origin, setOrigin] = useState('')
  const [allowPrivate, setAllowPrivate] = useState(false)
  const [clearance, setClearance] = useState<string>(CLEARANCES[0])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    try {
      const result = await add.mutateAsync({
        origin: origin.trim(),
        allow_private: allowPrivate,
        clearance,
      })
      if (result.status === 'pending' && result.approval_id) onPending(result.approval_id)
      setOrigin('')
      setAllowPrivate(false)
    } catch {
      /* shown from add.error below */
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-2 rounded-lg border border-border bg-card p-3">
      <div className="flex flex-wrap items-end gap-2">
        <label className="min-w-56 flex-1 space-y-1 text-xs font-medium text-foreground">
          <span className="block">Origin</span>
          <Input
            required
            placeholder="https://hooks.example.com"
            className="font-mono"
            value={origin}
            onChange={(e) => setOrigin(e.target.value)}
          />
        </label>
        <label className="space-y-1 text-xs font-medium text-foreground">
          <span className="block">Clearance</span>
          <select className={SELECT_CLASS} value={clearance} onChange={(e) => setClearance(e.target.value)}>
            {CLEARANCES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <Button type="submit" size="sm" className="h-9" disabled={add.isPending || !origin.trim()}>
          {add.isPending ? 'Adding…' : 'Add origin'}
        </Button>
      </div>
      <label className="flex items-center gap-2 text-sm text-foreground">
        <input type="checkbox" checked={allowPrivate} onChange={(e) => setAllowPrivate(e.target.checked)} />
        Allow private addresses
        <span className="text-xs text-muted-foreground">
          (loopback and internal networks; link-local and cloud metadata stay refused)
        </span>
      </label>
      {add.isError && (
        <p role="alert" className="text-sm text-destructive">
          {callbackRefusal(add.error)}
        </p>
      )}
    </form>
  )
}

function OriginRow({ entry }: { entry: CallbackEntry }) {
  const remove = useRemoveCallback()
  const confirmRemove = () => {
    if (!window.confirm(`Remove ${entry.origin}? Callbacks to it stop at the next check.`)) return
    remove.mutate(entry.id)
  }

  return (
    <li className="space-y-1 px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span
          className={`min-w-0 flex-1 truncate font-mono text-sm ${
            entry.status === 'removed' ? 'text-muted-foreground line-through' : 'text-foreground'
          }`}
        >
          {entry.origin}
        </span>
        <span className="text-[11px] text-muted-foreground">
          {entry.clearance}
          {entry.allow_private ? ', private allowed' : ''}
        </span>
        <span className={`rounded-sm border px-1.5 py-0.5 text-[11px] ${STATUS_CLASS[entry.status] ?? STATUS_CLASS.removed}`}>
          {entry.status}
        </span>
        {entry.status === 'pending' && (
          <Link className="text-xs underline" to="/approvals">
            Review approval
          </Link>
        )}
        {entry.status !== 'removed' && (
          <Button variant="outline" size="sm" disabled={remove.isPending} onClick={confirmRemove}>
            Remove
          </Button>
        )}
      </div>
      {remove.isError && (
        <p role="alert" className="text-sm text-destructive">
          {callbackRefusal(remove.error)}
        </p>
      )}
    </li>
  )
}

/**
 * Settings -> Apps -> Callback origins (SPEC-088 COMP-018, REQ-625).
 *
 * The operator-managed allowlist a run's `callback_url` must match exactly.
 * Above personal tier an add is pending until a second step approves it; the
 * add's reply names that approval, and this panel links straight to it.
 */
export function CallbackOriginsPanel() {
  const query = useCallbacks()
  const [pendingApproval, setPendingApproval] = useState<string | null>(null)
  const entries = query.data?.callbacks ?? []

  return (
    <section className="space-y-3">
      <div>
        <h2 className="font-display text-[15px] font-bold text-foreground">Callback origins</h2>
        <p className="mt-0.5 text-[13px] leading-snug text-muted-foreground">
          Arc sends signed work-event callbacks only to these origins. An app&rsquo;s callback URL
          must match one exactly.
        </p>
      </div>
      <AddOriginForm onPending={setPendingApproval} />
      {pendingApproval && (
        <p className="text-xs text-muted-foreground">
          Added as pending. It takes callbacks once an operator approves it.{' '}
          <Link className="underline" to={approvalHref(pendingApproval)}>
            Open the approval
          </Link>
        </p>
      )}
      {query.isLoading ? (
        <LoadingRows rows={2} />
      ) : query.isError ? (
        <ErrorState error={query.error} />
      ) : entries.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No origins yet. Add one before an app asks for callbacks.
        </p>
      ) : (
        <ul className="divide-y divide-border rounded-lg border border-border bg-card">
          {entries.map((entry) => (
            <OriginRow key={entry.id} entry={entry} />
          ))}
        </ul>
      )}
    </section>
  )
}
