import { useState, type FormEvent } from 'react'
import { Handshake } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { EmptyState, ErrorState, LoadingRows } from '@/components/states'
import { ApiError } from '@/lib/api'
import { fmtTime } from '@/lib/format'
import {
  DECISION_SCOPES,
  MAX_DELEGATION_HOURS,
  useCreateDelegation,
  useDelegations,
  useRevokeDelegation,
  type Delegation,
} from '@/lib/principals'

const errorText = (e: unknown) => (e instanceof Error ? e.message : 'Something went wrong.')

interface DelegationDraft {
  appDid: string
  scopes: string[]
  hours: string
}

/** Why the draft cannot be sent, or '' when it can. The server re-checks all of it. */
function draftProblem(draft: DelegationDraft): string {
  if (!draft.appDid.trim()) return "Enter the app's DID."
  if (draft.scopes.length === 0) return 'Pick at least one decision the app may make for you.'
  const hours = Number(draft.hours)
  if (!Number.isFinite(hours) || hours <= 0 || hours > MAX_DELEGATION_HOURS) {
    return `A delegation lasts more than 0 and at most ${MAX_DELEGATION_HOURS} hours.`
  }
  return ''
}

function DelegateForm() {
  const create = useCreateDelegation()
  const [draft, setDraft] = useState<DelegationDraft>({ appDid: '', scopes: [], hours: '1' })
  const [problem, setProblem] = useState('')

  const toggleScope = (scope: string) =>
    setDraft((d) => ({
      ...d,
      scopes: d.scopes.includes(scope) ? d.scopes.filter((s) => s !== scope) : [...d.scopes, scope],
    }))

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const reason = draftProblem(draft)
    setProblem(reason)
    if (reason) return
    create.mutate(
      {
        principal_did: draft.appDid.trim(),
        scopes: draft.scopes,
        ttl_seconds: Math.round(Number(draft.hours) * 3600),
      },
      { onSuccess: () => setDraft({ appDid: '', scopes: [], hours: '1' }) },
    )
  }

  const error = problem || (create.isError ? errorText(create.error) : '')

  return (
    <form noValidate onSubmit={submit} className="space-y-3 rounded-lg border border-border bg-card p-4">
      <h3 className="text-sm font-semibold text-foreground">Let an app decide for you</h3>
      <Input
        aria-label="App DID"
        placeholder="did:arc:…:app/…"
        className="font-mono"
        value={draft.appDid}
        onChange={(e) => setDraft({ ...draft, appDid: e.target.value })}
      />
      <fieldset className="space-y-1">
        <legend className="text-xs font-medium text-foreground">Decisions it may make</legend>
        {DECISION_SCOPES.map((scope) => (
          <label key={scope} className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={draft.scopes.includes(scope)} onChange={() => toggleScope(scope)} />
            <span className="font-mono">{scope}</span>
          </label>
        ))}
      </fieldset>
      <label className="flex items-center gap-2 text-xs font-medium text-foreground">
        <span>For how many hours (at most {MAX_DELEGATION_HOURS})</span>
        <Input
          type="number"
          className="w-20"
          min={1}
          max={MAX_DELEGATION_HOURS}
          value={draft.hours}
          onChange={(e) => setDraft({ ...draft, hours: e.target.value })}
        />
      </label>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <Button type="submit" disabled={create.isPending}>
        {create.isPending ? 'Delegating…' : 'Delegate'}
      </Button>
    </form>
  )
}

function DelegationRow({ delegation }: { delegation: Delegation }) {
  const revoke = useRevokeDelegation()
  const confirmRevoke = () => {
    if (!window.confirm('Revoke this delegation? The app can no longer decide for you.')) return
    revoke.mutate(delegation.id)
  }

  return (
    <li className="space-y-1 rounded-lg border border-border bg-card p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">{delegation.principal_did}</span>
        {delegation.revoked ? (
          <span className="text-[11px] text-muted-foreground">Revoked</span>
        ) : (
          <Button variant="outline" size="sm" disabled={revoke.isPending} onClick={confirmRevoke}>
            Revoke
          </Button>
        )}
      </div>
      <div className="flex flex-wrap gap-x-4 text-xs text-muted-foreground">
        <span className="font-mono">{delegation.scopes.join(', ')}</span>
        <span>Until {fmtTime(delegation.expires_at)}</span>
      </div>
      {revoke.isError && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(revoke.error)}
        </p>
      )}
    </li>
  )
}

/** A signed-in person's own delegations: which apps may make decisions for them, and until when. */
export function MyDelegationsPanel() {
  const query = useDelegations()

  if (query.isLoading) return <LoadingRows rows={2} />
  if (query.error instanceof ApiError && query.error.status === 403) {
    return (
      <EmptyState
        icon={<Handshake className="size-6" />}
        title="Sign in with your own account to manage delegations."
      />
    )
  }
  if (query.isError) return <ErrorState error={query.error} />

  const delegations = query.data?.delegations ?? []

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div>
        <h2 className="font-display text-[15px] font-bold text-foreground">My delegations</h2>
        <p className="mt-0.5 text-[13px] leading-snug text-muted-foreground">
          Let an app approve gates or update tasks in your name for a few hours. Every decision it
          makes is recorded as the app acting for you.
        </p>
      </div>
      <DelegateForm />
      {delegations.length > 0 && (
        <ul className="space-y-2">
          {delegations.map((d) => (
            <DelegationRow key={d.id} delegation={d} />
          ))}
        </ul>
      )}
    </div>
  )
}
