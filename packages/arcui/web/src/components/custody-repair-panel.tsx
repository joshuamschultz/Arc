import { useState } from 'react'
import { KeyRound } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useCustody, useResolveCustody } from '@/lib/queries'
import type { CustodyDecision, CustodyReason, CustodyStatus } from '@/lib/types'

/* ---------------------------------------------------------------------------
 * Credential custody repair. When Arc cannot move an old saved credential file
 * into sealed storage on its own, the dashboard still starts, the connections
 * that use those credentials read "Credentials need review", and the operator
 * answers for each saved key HERE: map it to a connection field, keep it in the
 * file, or drop it (typing the key's name to confirm). Keys are shown by name
 * only. A value is never shown, sent or stored by this panel.
 * ------------------------------------------------------------------------- */

const WHY: Record<CustodyReason, string> = {
  undeclared: 'No connection uses this key.',
  custody_differs: 'Arc already holds a different value for this field.',
  app_slot_differs: 'The sign-in app already saved holds a different app.',
  app_pair_incomplete: 'This is half of a sign-in app. The other half is missing.',
  app_values_disagree: 'Two saved values for one sign-in app disagree.',
}

type Choice = '' | 'map' | 'keep' | 'drop'

interface Answer {
  choice: Choice
  target: string
  confirm: string
}

const BLANK: Answer = { choice: '', target: '', confirm: '' }

/** One answer, turned into the request row, or `null` while it is still incomplete. */
function decisionFor(key: string, answer: Answer): CustodyDecision | null {
  if (answer.choice === 'keep') return { key, action: 'keep' }
  if (answer.choice === 'drop' && answer.confirm === key) {
    return { key, action: 'drop', confirm: answer.confirm }
  }
  if (answer.choice === 'map' && answer.target) {
    const [connection, field] = answer.target.split('/')
    return { key, action: 'map', connection, field }
  }
  return null
}

function KeyRow({
  keyName,
  reason,
  targets,
  answer,
  onChange,
}: {
  keyName: string
  reason: CustodyReason
  targets: CustodyStatus['targets']
  answer: Answer
  onChange: (next: Answer) => void
}) {
  const mappable = reason === 'undeclared' && targets.length > 0
  return (
    <li className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-mono text-[13px] font-bold text-foreground">{keyName}</span>
        <span className="text-xs text-muted-foreground">{WHY[reason]}</span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label={`What to do with ${keyName}`}
          className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
          value={answer.choice}
          onChange={(e) => onChange({ ...answer, choice: e.target.value as Choice })}
        >
          <option value="">Choose...</option>
          {mappable && <option value="map">Map to a connection field</option>}
          <option value="keep">Keep it in the file</option>
          <option value="drop">Drop it</option>
        </select>
        {answer.choice === 'map' && (
          <select
            aria-label={`Field for ${keyName}`}
            className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
            value={answer.target}
            onChange={(e) => onChange({ ...answer, target: e.target.value })}
          >
            <option value="">Pick a field...</option>
            {targets.map((t) => (
              <option key={`${t.connection}/${t.field}`} value={`${t.connection}/${t.field}`}>
                {t.connection} / {t.field}
              </option>
            ))}
          </select>
        )}
        {answer.choice === 'drop' && (
          <input
            aria-label={`Type ${keyName} to confirm the drop`}
            className="h-9 rounded-md border border-input bg-transparent px-2 font-mono text-sm"
            placeholder={`Type ${keyName} to confirm`}
            value={answer.confirm}
            onChange={(e) => onChange({ ...answer, confirm: e.target.value })}
          />
        )}
      </div>
      {answer.choice === 'drop' && (
        <p className="text-xs text-muted-foreground">
          Dropping deletes this saved value for good. Arc records the key name, never the value.
        </p>
      )}
    </li>
  )
}

const BLOCKED_WHY: Record<string, string> = {
  SECRET_STORE_UNCONFIGURED: 'Arc has no secure place to keep credentials yet.',
}

/** The panel body: answer every key, then apply. */
export function CustodyRepairPanel({ status }: { status: CustodyStatus }) {
  const resolve = useResolveCustody()
  const [answers, setAnswers] = useState<Record<string, Answer>>({})
  const [error, setError] = useState<string | null>(null)

  if (status.state === 'blocked') {
    return (
      <p className="text-sm text-foreground" role="alert">
        {BLOCKED_WHY[status.code ?? ''] ??
          'Arc cannot safely read the old credential file, so it cannot be reviewed here.'}
      </p>
    )
  }

  const decisions = status.keys.map((k) =>
    decisionFor(k.key, answers[k.key] ?? BLANK),
  )
  const ready = status.keys.length > 0 && decisions.every((d) => d !== null)

  const apply = async () => {
    setError(null)
    try {
      await resolve.mutateAsync(decisions as CustodyDecision[])
      setAnswers({})
    } catch (e) {
      setError(
        e instanceof Error && /409|MIGRATION/.test(e.message)
          ? 'Some keys still need an answer. Nothing was changed.'
          : 'Arc could not apply these answers. Nothing was changed.',
      )
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <ul className="flex flex-col gap-2">
        {status.keys.map((k) => (
          <KeyRow
            key={k.key}
            keyName={k.key}
            reason={k.reason}
            targets={status.targets}
            answer={answers[k.key] ?? BLANK}
            onChange={(next) => setAnswers({ ...answers, [k.key]: next })}
          />
        ))}
      </ul>
      <div>
        <Button size="sm" disabled={!ready || resolve.isPending} onClick={apply}>
          Apply answers
        </Button>
      </div>
      {error && (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}

/** A banner for the app shell: shown only when credentials need review. */
export function CustodyBanner() {
  const { data } = useCustody()
  const [open, setOpen] = useState(false)
  if (!data || data.state === 'ok') return null
  return (
    <section
      aria-label="Credentials need review"
      className="flex flex-col gap-3 border-b border-status-warning/30 bg-status-warning/10 p-4"
    >
      <div className="flex flex-wrap items-center gap-3">
        <KeyRound className="size-4 shrink-0 text-status-warning" />
        <p className="min-w-0 flex-1 text-sm text-foreground">
          Credentials need review.
          {data.affected_connections.length > 0 &&
            ` Affected: ${data.affected_connections.join(', ')}.`}
        </p>
        <Button size="sm" variant="outline" onClick={() => setOpen(!open)}>
          {open ? 'Hide' : 'Review'}
        </Button>
      </div>
      {open && <CustodyRepairPanel status={data} />}
    </section>
  )
}
