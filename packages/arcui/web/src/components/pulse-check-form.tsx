import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { ApiError, apiPost, apiPut } from '@/lib/api'
import { humanizeInterval } from '@/lib/schedule-format'
import { pulseKey, pulsePath, type PulseCheckDraft } from '@/lib/pulse'


type Unit = 'minutes' | 'hours' | 'days'
const UNIT_MINUTES: Record<Unit, number> = { minutes: 1, hours: 60, days: 1440 }
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/
const MAX_MINUTES = 525_600

/** Largest unit that divides the interval evenly, so an edit shows "2 hours", not "120 minutes". */
function splitInterval(minutes: number): { count: number; unit: Unit } {
  if (minutes % 1440 === 0) return { count: minutes / 1440, unit: 'days' }
  if (minutes % 60 === 0) return { count: minutes / 60, unit: 'hours' }
  return { count: minutes, unit: 'minutes' }
}


/**
 * Add or edit one pulse check. Saving writes pulse.md through the operator-only
 * audited route; the check then reads "pending approval" and is approved with the
 * existing Approve button, so nothing runs from this form alone.
 */
export function PulseCheckForm({
  agentId,
  draft,
  editing,
  onDone,
}: {
  agentId: string
  draft: PulseCheckDraft | null
  editing: boolean
  onDone: (saved: boolean) => void
}) {
  const queryClient = useQueryClient()
  const start = splitInterval(draft?.interval_minutes ?? 60)
  const [name, setName] = useState(draft?.name ?? '')
  const [action, setAction] = useState(draft?.action ?? '')
  const [count, setCount] = useState(String(start.count))
  const [unit, setUnit] = useState<Unit>(start.unit)
  const [error, setError] = useState<string | null>(null)

  const minutes = Number(count) * UNIT_MINUTES[unit]
  const intervalOk = Number.isInteger(minutes) && minutes >= 1 && minutes <= MAX_MINUTES

  const save = useMutation<unknown, Error, void>({
    mutationFn: () => {
      const body = { interval_minutes: minutes, action: action.trim() }
      if (editing) {
        return apiPut(`${pulsePath(agentId)}/${encodeURIComponent(name)}`, {
          ...body,
          definition_digest: draft?.definition_digest,
        })
      }
      return apiPost(pulsePath(agentId), {
        name,
        ...body,
        ...(draft?.proposal ? { proposal: draft.proposal } : {}),
      })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: pulseKey(agentId) })
      onDone(true)
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : 'Could not save this check'),
  })

  const submit = () => {
    setError(null)
    if (!NAME_RE.test(name)) {
      setError('Name must be letters, digits, "-", "_" or "." with no spaces.')
    } else if (!action.trim()) {
      setError('Say what the agent should check.')
    } else if (!intervalOk) {
      setError('Pick a whole-number interval between 1 minute and 1 year.')
    } else {
      save.mutate()
    }
  }

  return (
    <form
      className="space-y-3 rounded-md border border-border p-3"
      onSubmit={(event) => {
        event.preventDefault()
        submit()
      }}
    >
      <div className="space-y-1">
        <label htmlFor="pulse-name" className="text-xs font-medium text-foreground">
          Name
        </label>
        <Input
          id="pulse-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={editing}
          placeholder="inbox-sweep"
        />
      </div>
      <div className="space-y-1">
        <label htmlFor="pulse-action" className="text-xs font-medium text-foreground">
          What to check
        </label>
        <Textarea
          id="pulse-action"
          value={action}
          onChange={(e) => setAction(e.target.value)}
          rows={3}
          placeholder="Look for unanswered customer emails older than a day and tell me who is waiting."
        />
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <div className="space-y-1">
          <label htmlFor="pulse-every" className="text-xs font-medium text-foreground">
            Every
          </label>
          <Input
            id="pulse-every"
            type="number"
            min={1}
            className="w-24"
            value={count}
            onChange={(e) => setCount(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <label htmlFor="pulse-unit" className="text-xs font-medium text-foreground">
            Unit
          </label>
          <select
            id="pulse-unit"
            value={unit}
            onChange={(e) => setUnit(e.target.value as Unit)}
            className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
          >
            <option value="minutes">minutes</option>
            <option value="hours">hours</option>
            <option value="days">days</option>
          </select>
        </div>
        <p className="pb-2 text-xs text-muted-foreground">
          {intervalOk ? `Runs ${humanizeInterval(minutes * 60).replace(/^Every/, 'every')}` : 'Enter an interval'}
        </p>
      </div>
      <p className="text-xs text-muted-foreground">
        Saving does not start the check. It waits for your approval below.
      </p>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={save.isPending}>
          {save.isPending ? 'Saving…' : 'Save check'}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => onDone(false)}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
