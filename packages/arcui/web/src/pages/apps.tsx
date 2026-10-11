import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { AppWindow, Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { CallbackOriginsPanel } from '@/components/callback-origins-panel'
import { CopyButton } from '@/components/copy-button'
import { EmptyState, ErrorState, LoadingRows } from '@/components/states'
import { ApiError } from '@/lib/api'
import { fmtTime } from '@/lib/format'
import { usePublicAddress, useRoster } from '@/lib/queries'
import { useUsers, type ArcUser } from '@/lib/users'
import {
  SCOPES,
  issuePrincipal,
  maxExpiryDays,
  usePrincipals,
  useRefreshPrincipals,
  useRevokePrincipal,
  type AppPrincipal,
  type IssuePrincipalResult,
} from '@/lib/principals'

const DAY_MS = 86_400_000
const PUBLIC_KEY_RE = /^[0-9a-fA-F]{64}$/

const errorText = (e: unknown) => (e instanceof Error ? e.message : 'Something went wrong.')

const approvalHref = (approvalId: string) => `/approvals#${encodeURIComponent(approvalId)}`

const fmtDate = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })

/** A roster agent the form can grant a scope on. */
interface AgentChoice {
  did: string
  label: string
}

/** DID -> display name for roster agents, and the agents the form may offer. */
function useAgentChoices(): { choices: AgentChoice[]; nameOf: (did: string) => string } {
  const roster = useRoster()
  const agents = roster.data?.agents ?? []
  const names = new Map<string, string>()
  const choices: AgentChoice[] = []
  for (const agent of agents) {
    if (!agent.did) continue
    const label = agent.display_name || agent.name || agent.agent_id || agent.did
    names.set(agent.did, label)
    if (!agent.hidden) choices.push({ did: agent.did, label })
  }
  return { choices, nameOf: (did) => names.get(did) ?? did }
}

/** DID -> email for people; the people list is operator-only, as is this page. */
function usePeople(): { people: ArcUser[]; emailOf: (did: string) => string } {
  const users = useUsers()
  const people = users.data?.users ?? []
  const emails = new Map(people.map((u) => [u.did, u.email]))
  return { people, emailOf: (did) => emails.get(did) ?? did }
}

// --- issue form ---------------------------------------------------------------------

type Grants = Record<string, string[]>

function toggleGrant(grants: Grants, scope: string, did: string): Grants {
  const current = grants[scope] ?? []
  const next = current.includes(did) ? current.filter((d) => d !== did) : [...current, did]
  const copy = { ...grants, [scope]: next }
  if (next.length === 0) delete copy[scope]
  return copy
}

/** Local midnight of a date input's YYYY-MM-DD value. */
function startOfLocalDay(value: string): Date {
  const [year, month, day] = value.split('-').map(Number)
  return new Date(year, month - 1, day)
}

/** The local YYYY-MM-DD `days` from today: the date input's `max`. */
function dateInputValue(days: number): string {
  const d = new Date(Date.now() + days * DAY_MS)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

interface IssueDraft {
  name: string
  publicKey: string
  grants: Grants
  expires: string
}

/** Why the draft cannot be sent, or '' when it can. The server re-checks all of it. */
function draftProblem(draft: IssueDraft, tier: string): string {
  if (!draft.name.trim()) return 'Give the app a name.'
  if (!PUBLIC_KEY_RE.test(draft.publicKey.trim())) {
    return 'The public key is 64 hex characters (an Ed25519 key).'
  }
  if (Object.keys(draft.grants).length === 0) return 'Grant at least one scope on one agent.'
  const cap = maxExpiryDays(tier)
  if (!draft.expires) return cap === null ? '' : `At the ${tier} tier an app must have an expiry date.`
  const ms = startOfLocalDay(draft.expires).getTime() - Date.now()
  if (ms <= 0) return 'Pick an expiry date in the future.'
  if (cap !== null && ms > cap * DAY_MS) return `At the ${tier} tier an app can last at most ${cap} days.`
  return ''
}

function ScopeGrid({
  agents,
  grants,
  onToggle,
}: {
  agents: AgentChoice[]
  grants: Grants
  onToggle: (scope: string, did: string) => void
}) {
  if (agents.length === 0) {
    return <p className="text-xs text-muted-foreground">No agents yet. Add an agent first.</p>
  }
  return (
    <fieldset className="space-y-1">
      <legend className="text-xs font-medium text-foreground">What it may do, on which agent</legend>
      <div className="overflow-x-auto">
        <table className="text-xs">
          <thead>
            <tr>
              <th className="pr-3 text-left font-normal text-muted-foreground">Scope</th>
              {agents.map((a) => (
                <th key={a.did} className="px-2 font-normal text-muted-foreground">
                  {a.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {SCOPES.map((scope) => (
              <tr key={scope}>
                <td className="pr-3 font-mono">{scope}</td>
                {agents.map((a) => (
                  <td key={a.did} className="px-2 text-center">
                    <input
                      type="checkbox"
                      aria-label={`${scope} on ${a.label}`}
                      checked={grants[scope]?.includes(a.did) ?? false}
                      onChange={() => onToggle(scope, a.did)}
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </fieldset>
  )
}

function SubjectPicker({
  people,
  chosen,
  onToggle,
}: {
  people: ArcUser[]
  chosen: string[]
  onToggle: (did: string) => void
}) {
  return (
    <fieldset className="space-y-1">
      <legend className="text-xs font-medium text-foreground">People it may act for (optional)</legend>
      {people.length === 0 && <p className="text-xs text-muted-foreground">No people yet.</p>}
      {people.map((person) => (
        <label key={person.did} className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={chosen.includes(person.did)} onChange={() => onToggle(person.did)} />
          {person.email}
        </label>
      ))}
    </fieldset>
  )
}

function IssueForm({
  tier,
  onIssued,
  onCancel,
}: {
  tier: string
  onIssued: (result: IssuePrincipalResult) => void
  onCancel: () => void
}) {
  const { choices } = useAgentChoices()
  const { people } = usePeople()
  const [draft, setDraft] = useState<IssueDraft>({ name: '', publicKey: '', grants: {}, expires: '' })
  const [subjects, setSubjects] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const cap = maxExpiryDays(tier)

  const toggleSubject = (did: string) =>
    setSubjects((s) => (s.includes(did) ? s.filter((d) => d !== did) : [...s, did]))

  // The issue call goes around the query cache on purpose: its reply holds the
  // one-time secret, which must live only in the reveal's component state.
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const problem = draftProblem(draft, tier)
    if (problem) return setError(problem)
    setError('')
    setBusy(true)
    try {
      onIssued(
        await issuePrincipal({
          name: draft.name.trim(),
          public_key: draft.publicKey.trim(),
          scopes: draft.grants,
          subjects,
          expires_at: draft.expires ? startOfLocalDay(draft.expires).toISOString() : null,
        }),
      )
    } catch (e) {
      setError(errorText(e))
      setBusy(false)
    }
  }

  // noValidate: the date bounds are picker hints; draftProblem names the reason.
  return (
    <form noValidate onSubmit={(e) => void submit(e)} className="space-y-3 rounded-lg border border-border bg-card p-4">
      <h3 className="text-sm font-semibold text-foreground">Issue an app credential</h3>
      <Input
        aria-label="App name"
        placeholder="app name"
        value={draft.name}
        onChange={(e) => setDraft({ ...draft, name: e.target.value })}
      />
      <Input
        aria-label="Public key"
        placeholder="Ed25519 public key (64 hex characters)"
        className="font-mono"
        value={draft.publicKey}
        onChange={(e) => setDraft({ ...draft, publicKey: e.target.value })}
      />
      <ScopeGrid
        agents={choices}
        grants={draft.grants}
        onToggle={(scope, did) => setDraft({ ...draft, grants: toggleGrant(draft.grants, scope, did) })}
      />
      <SubjectPicker people={people} chosen={subjects} onToggle={toggleSubject} />
      <label className="block space-y-1 text-xs font-medium text-foreground">
        <span>{cap === null ? 'Expires (optional)' : `Expires (at most ${cap} days)`}</span>
        <Input
          type="date"
          className="w-48"
          min={dateInputValue(1)}
          max={cap === null ? undefined : dateInputValue(cap)}
          value={draft.expires}
          onChange={(e) => setDraft({ ...draft, expires: e.target.value })}
        />
      </label>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <Button type="submit" disabled={busy}>
          {busy ? 'Issuing…' : 'Issue app'}
        </Button>
        <Button type="button" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

/** The one-time secret. It lives only in this component's parent state; closing forgets it. */
function SecretReveal({ issued, onClose }: { issued: IssuePrincipalResult; onClose: () => void }) {
  return (
    <div className="space-y-2 rounded-lg border border-border bg-muted/30 p-3">
      <div className="text-sm font-medium text-foreground">Secret for {issued.principal.name}</div>
      <div className="flex flex-wrap items-center gap-2">
        <code className="min-w-0 flex-1 break-all rounded bg-muted px-2 py-1 text-xs">{issued.secret}</code>
        <CopyButton text={issued.secret} label="Copy secret" />
      </div>
      <p className="text-xs text-muted-foreground">
        Arc shows this once and keeps only a hash. Put it in the app&rsquo;s secret store now.
      </p>
      {issued.status === 'pending' && issued.approval_id && (
        <p className="text-xs text-muted-foreground">
          It works once an operator approves it.{' '}
          <Link className="underline" to={approvalHref(issued.approval_id)}>
            Open the approval
          </Link>
        </p>
      )}
      <Button size="sm" variant="outline" onClick={onClose}>
        I saved it
      </Button>
    </div>
  )
}

// --- list -------------------------------------------------------------------------------

const STATUS_CLASS: Record<string, string> = {
  active:
    'border-emerald-500/30 bg-emerald-500/10 font-medium text-emerald-700 dark:text-emerald-400',
  pending: 'border-amber-500/30 bg-amber-500/10 font-medium text-amber-700 dark:text-amber-400',
  revoked: 'border-border bg-muted/40 text-muted-foreground',
}

function PrincipalRow({
  principal,
  nameOf,
  emailOf,
}: {
  principal: AppPrincipal
  nameOf: (did: string) => string
  emailOf: (did: string) => string
}) {
  const revoke = useRevokePrincipal()
  const confirmRevoke = () => {
    if (!window.confirm(`Revoke ${principal.name}? Its next request is refused.`)) return
    revoke.mutate(principal.id)
  }

  return (
    <li className="space-y-2 rounded-lg border border-border bg-card p-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-foreground">{principal.name}</div>
          <div className="truncate font-mono text-[11px] text-muted-foreground">{principal.did}</div>
        </div>
        <span className={`rounded-sm border px-1.5 py-0.5 text-[11px] ${STATUS_CLASS[principal.status] ?? STATUS_CLASS.revoked}`}>
          {principal.status}
        </span>
        {principal.status === 'pending' && principal.approval_id && (
          <Link className="text-xs underline" to={approvalHref(principal.approval_id)}>
            Review approval
          </Link>
        )}
        {principal.status !== 'revoked' && (
          <Button variant="outline" size="sm" disabled={revoke.isPending} onClick={confirmRevoke}>
            Revoke
          </Button>
        )}
      </div>
      <ul className="space-y-0.5 text-xs">
        {Object.entries(principal.scopes).map(([scope, agents]) => (
          <li key={scope}>
            <span className="font-mono">{scope}</span>
            <span className="text-muted-foreground"> on {agents.map(nameOf).join(', ')}</span>
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span>Acts for: {principal.subjects.length ? principal.subjects.map(emailOf).join(', ') : 'no one'}</span>
        <span>Last used: {principal.last_used_at ? fmtTime(principal.last_used_at) : 'never'}</span>
        <span>Expires: {principal.expires_at ? fmtDate(principal.expires_at) : 'never'}</span>
      </div>
      {revoke.isError && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(revoke.error)}
        </p>
      )}
    </li>
  )
}

/** Apps that call Arc with their own credential. Operators manage them; viewers see a notice. */
export function AppsPanel() {
  const query = usePrincipals()
  const tier = usePublicAddress().data?.tier
  const refresh = useRefreshPrincipals()
  const { nameOf } = useAgentChoices()
  const { emailOf } = usePeople()
  const [formOpen, setFormOpen] = useState(false)
  const [issued, setIssued] = useState<IssuePrincipalResult | null>(null)

  if (query.isLoading) return <LoadingRows rows={3} />
  if (query.error instanceof ApiError && query.error.status === 403) {
    return <EmptyState icon={<AppWindow className="size-6" />} title="Only an operator can manage apps." />
  }
  if (query.isError) return <ErrorState error={query.error} />

  const principals = query.data?.principals ?? []
  const onIssued = (result: IssuePrincipalResult) => {
    setIssued(result)
    setFormOpen(false)
    void refresh()
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="min-w-0 flex-1 font-display text-[15px] font-bold text-foreground">Apps</h2>
        <Button size="sm" onClick={() => setFormOpen(true)}>
          <Plus /> New app
        </Button>
      </div>
      {formOpen && (
        // An unknown tier (still loading) gets the strictest cap: fail closed.
        <IssueForm tier={tier ?? 'unknown'} onIssued={onIssued} onCancel={() => setFormOpen(false)} />
      )}
      {issued && <SecretReveal issued={issued} onClose={() => setIssued(null)} />}
      {principals.length === 0 ? (
        <EmptyState title="No apps yet" description="Issue a credential so an app can call Arc." />
      ) : (
        <ul className="space-y-2">
          {principals.map((p) => (
            <PrincipalRow key={p.id} principal={p} nameOf={nameOf} emailOf={emailOf} />
          ))}
        </ul>
      )}
      <div className="border-t border-border pt-5">
        <CallbackOriginsPanel />
      </div>
    </div>
  )
}
