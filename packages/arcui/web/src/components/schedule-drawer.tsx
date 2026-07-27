import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { JsonBlock } from '@/components/json-block'
import { apiPatch, ApiError } from '@/lib/api'
import {
  buildCron,
  CRON_DAYS,
  cronToProse,
  humanizeInterval,
  parseCron,
  scheduleTiming,
  scheduleTitle,
  type CronFrequency,
  type CronSpec,
} from '@/lib/schedule-format'
import { useAgentChannels } from '@/lib/queries'
import { cn } from '@/lib/utils'
import type { Dict } from '@/lib/types'

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

/** Friendly cron editor: frequency dropdown + time/day boxes that serialize to a
 *  cron string, with a raw-expression escape hatch for anything it can't model. */
function CronBuilder({ value, onChange }: { value: string; onChange: (expr: string) => void }) {
  const parsed = parseCron(value)
  const [raw, setRaw] = useState(value.trim() !== '' && parsed == null)
  const spec: CronSpec = parsed ?? { frequency: 'daily', minute: 0, hour: 9, dow: 1, dom: 1 }
  const update = (patch: Partial<CronSpec>) => onChange(buildCron({ ...spec, ...patch }))
  const timeValue = `${pad2(spec.hour)}:${pad2(spec.minute)}`
  const onTime = (t: string) => {
    const [h, m] = t.split(':')
    update({ hour: Number(h), minute: Number(m) })
  }

  if (raw) {
    return (
      <div className="space-y-1.5">
        <Input value={value} onChange={(e) => onChange(e.target.value)} placeholder="40 10 * * *" />
        <button
          type="button"
          className="text-xs text-primary underline"
          onClick={() => {
            if (parseCron(value) == null) onChange(buildCron(spec))
            setRaw(false)
          }}
        >
          Use the simple builder
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={spec.frequency}
          onChange={(e) => update({ frequency: e.target.value as CronFrequency })}
          className="h-9 rounded-md border border-border bg-background px-2 text-sm"
        >
          <option value="hourly">Every hour</option>
          <option value="daily">Every day</option>
          <option value="weekly">Every week</option>
          <option value="monthly">Every month</option>
        </select>

        {spec.frequency === 'weekly' && (
          <select
            value={spec.dow}
            onChange={(e) => update({ dow: Number(e.target.value) })}
            className="h-9 rounded-md border border-border bg-background px-2 text-sm"
          >
            {CRON_DAYS.map((d, i) => (
              <option key={d} value={i}>
                {d}
              </option>
            ))}
          </select>
        )}

        {spec.frequency === 'monthly' && (
          <label className="flex items-center gap-1 text-sm text-muted-foreground">
            on day
            <Input
              type="number"
              min={1}
              max={31}
              value={spec.dom}
              onChange={(e) => update({ dom: Number(e.target.value) })}
              className="h-9 w-16"
            />
          </label>
        )}

        {spec.frequency === 'hourly' ? (
          <label className="flex items-center gap-1 text-sm text-muted-foreground">
            at minute
            <Input
              type="number"
              min={0}
              max={59}
              value={spec.minute}
              onChange={(e) => update({ minute: Number(e.target.value) })}
              className="h-9 w-16"
            />
          </label>
        ) : (
          <label className="flex items-center gap-1 text-sm text-muted-foreground">
            at
            <Input type="time" value={timeValue} onChange={(e) => onTime(e.target.value)} className="h-9 w-32" />
          </label>
        )}
      </div>
      <button type="button" className="text-xs text-muted-foreground underline" onClick={() => setRaw(true)}>
        Advanced (raw cron)
      </button>
    </div>
  )
}

/** Delivery-target picker: a dropdown of channels the agent has been reached on
 *  (so a non-technical operator never types a raw platform:chat_id), with a
 *  "None" option and a custom escape hatch. */
function ChannelSelect({
  agentId,
  value,
  onChange,
}: {
  agentId: string
  value: string
  onChange: (target: string) => void
}) {
  const { data } = useAgentChannels(agentId)
  const channels = data?.channels ?? []
  const known = channels.some((c) => c.target === value)
  const [custom, setCustom] = useState(value.trim() !== '' && !known)

  if (custom) {
    return (
      <div className="space-y-1.5">
        <Input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="telegram:12345"
        />
        <button
          type="button"
          className="text-xs text-primary underline"
          onClick={() => {
            onChange('')
            setCustom(false)
          }}
        >
          Choose from known channels
        </button>
      </div>
    )
  }

  return (
    <select
      value={value}
      onChange={(e) => {
        if (e.target.value === '__custom__') setCustom(true)
        else onChange(e.target.value)
      }}
      className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm"
    >
      <option value="">None — keep result internal</option>
      {channels.map((c) => (
        <option key={c.target} value={c.target}>
          {c.label}
        </option>
      ))}
      {value && !known && <option value={value}>{value}</option>}
      <option value="__custom__">Custom…</option>
    </select>
  )
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">{label}</div>
      <div className="truncate font-mono text-sm text-foreground">{value || '—'}</div>
    </div>
  )
}

/** Live plain-English preview of the timing field the operator is editing. */
function timingPreview(type: string, expression: string, everySeconds: string, at: string): string {
  if (type === 'cron') return expression.trim() ? cronToProse(expression) : '—'
  if (type === 'interval') {
    const n = Number(everySeconds)
    return Number.isFinite(n) && n > 0 ? humanizeInterval(n) : '—'
  }
  if (type === 'once') return at.trim() || '—'
  return '—'
}

/** Edit form + read view for one schedule, keyed by ``schedule.id`` in the
 *  parent so switching schedules re-initializes state without an effect. */
function ScheduleDetail({
  schedule,
  agentId,
  operatorMode,
  onClose,
}: {
  schedule: Dict
  agentId: string
  operatorMode: boolean
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const type = String(schedule.type ?? '')
  const metadata = (schedule.metadata ?? {}) as Dict

  const [editing, setEditing] = useState(false)
  const [enabled, setEnabled] = useState(schedule.enabled !== false)
  const [prompt, setPrompt] = useState(String(schedule.prompt ?? ''))
  const [expression, setExpression] = useState(String(schedule.expression ?? ''))
  const [everySeconds, setEverySeconds] = useState(String(schedule.every_seconds ?? ''))
  const [at, setAt] = useState(String(schedule.at ?? ''))
  const [timeoutSeconds, setTimeoutSeconds] = useState(String(schedule.timeout_seconds ?? ''))
  const [deliverTo, setDeliverTo] = useState(String(schedule.deliver_to ?? ''))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
    setSaving(true)
    setError(null)
    const body: Dict = {
      enabled,
      prompt,
      timeout_seconds: Number(timeoutSeconds),
      // Empty clears delivery (result stays internal); the route normalizes "" → null.
      deliver_to: deliverTo.trim(),
    }
    if (type === 'cron') body.expression = expression
    else if (type === 'interval') body.every_seconds = Number(everySeconds)
    else if (type === 'once') body.at = at
    try {
      await apiPatch(
        `/api/agents/${encodeURIComponent(agentId)}/schedules/${encodeURIComponent(String(schedule.id))}`,
        body,
      )
      // The scheduler engine reloads schedules.json on its next tick; the table
      // refetches here. Close the drawer so the operator sees the fresh row
      // (this snapshot's `schedule` prop is now stale).
      await queryClient.invalidateQueries({ predicate: (q) => q.queryKey.includes('schedules') })
      onClose()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to save schedule')
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <SheetHeader className="border-b border-border px-5 py-4">
        <SheetTitle className="truncate text-sm">{scheduleTitle(schedule)}</SheetTitle>
        <SheetDescription className="flex flex-wrap items-center gap-2">
          <span className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
            {type || '—'}
          </span>
          <span>{scheduleTiming(schedule)}</span>
          <span
            className={cn(
              'inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium',
              enabled
                ? 'bg-status-online/15 text-status-online'
                : 'bg-muted text-muted-foreground',
            )}
          >
            {enabled ? 'enabled' : 'disabled'}
          </span>
        </SheetDescription>
      </SheetHeader>

      <div className="flex-1 space-y-5 overflow-auto p-5">
        {editing ? (
          <section className="space-y-3">
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
                className="size-4 accent-primary"
              />
              Enabled
            </label>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">
                {type === 'cron'
                  ? 'Cron expression'
                  : type === 'interval'
                    ? 'Every (seconds)'
                    : 'Run at (ISO 8601)'}
              </label>
              {type === 'cron' && <CronBuilder value={expression} onChange={setExpression} />}
              {type === 'interval' && (
                <Input
                  type="number"
                  min={60}
                  value={everySeconds}
                  onChange={(e) => setEverySeconds(e.target.value)}
                  placeholder="3600"
                />
              )}
              {type === 'once' && (
                <Input
                  value={at}
                  onChange={(e) => setAt(e.target.value)}
                  placeholder="2026-07-12T10:40:00Z"
                />
              )}
              <p className="text-xs text-muted-foreground">
                {timingPreview(type, expression, everySeconds, at)}
              </p>
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Prompt</label>
              <Textarea rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Timeout (seconds)</label>
              <Input
                type="number"
                min={1}
                value={timeoutSeconds}
                onChange={(e) => setTimeoutSeconds(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Deliver to (channel)</label>
              <ChannelSelect agentId={agentId} value={deliverTo} onChange={setDeliverTo} />
              <p className="text-xs text-muted-foreground">
                Where the run's output is sent when it fires. Pick a channel the agent has
                talked on, or keep it internal.
              </p>
            </div>
            {error && <p className="text-xs text-destructive">{error}</p>}
            <div className="flex gap-2">
              <Button size="sm" disabled={saving || !prompt.trim()} onClick={save}>
                {saving ? 'Saving…' : 'Save'}
              </Button>
              <Button size="sm" variant="outline" disabled={saving} onClick={() => setEditing(false)}>
                Cancel
              </Button>
            </div>
          </section>
        ) : (
          <section className="grid grid-cols-2 gap-3">
            <Field label="Runs" value={scheduleTiming(schedule)} />
            <Field
              label="Timeout"
              value={schedule.timeout_seconds != null ? `${schedule.timeout_seconds}s` : '—'}
            />
            <Field label="Deliver to" value={String(schedule.deliver_to ?? '—')} />
            <Field label="Schedule id" value={String(schedule.id ?? '—')} />
            <Field label="Created by" value={String(metadata.created_by ?? '—')} />
            <Field label="Run count" value={String(metadata.run_count ?? 0)} />
            <Field label="Last run" value={String(metadata.last_run ?? '—')} />
            <Field label="Last result" value={String(metadata.last_result ?? '—')} />
          </section>
        )}

        {!editing && (
          <section className="space-y-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Prompt
            </h3>
            <p className="whitespace-pre-wrap rounded-lg border border-border bg-muted/20 p-3 text-sm text-foreground">
              {String(schedule.prompt ?? '—')}
            </p>
          </section>
        )}

        <section className="space-y-2">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Raw entry
          </h3>
          <JsonBlock value={schedule} />
        </section>
      </div>

      {operatorMode && !editing && (
        <div className="border-t border-border p-4">
          <Button size="sm" onClick={() => setEditing(true)}>
            Edit
          </Button>
        </div>
      )}
    </>
  )
}

/** Schedule detail drawer. Operators (operator mode on) can edit the timing,
 *  prompt, timeout, and enabled flag; the edit lands in ``schedules.json`` via
 *  ``PATCH /api/agents/{id}/schedules/{sid}`` and the agent's scheduler picks
 *  it up on its next tick. Viewers see a read-only view. */
export function ScheduleDrawer({
  schedule,
  open,
  onOpenChange,
  agentId,
  operatorMode,
}: {
  schedule: Dict | null
  open: boolean
  onOpenChange: (o: boolean) => void
  agentId: string
  operatorMode: boolean
}) {
  if (schedule == null) return null
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        <ScheduleDetail
          key={String(schedule.id)}
          schedule={schedule}
          agentId={agentId}
          operatorMode={operatorMode}
          onClose={() => onOpenChange(false)}
        />
      </SheetContent>
    </Sheet>
  )
}
