import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { JsonBlock } from '@/components/json-block'
import { cn } from '@/lib/utils'
import type { Dict } from '@/lib/types'

/** Human-readable timing summary for a scheduler `ScheduleEntry` dict — cron
 *  expression, interval seconds, or a one-time ISO timestamp. Shared between
 *  the Schedules tab's list column and the detail drawer so both agree. */
export function scheduleTiming(schedule: Dict): string {
  const type = String(schedule.type ?? '')
  if (type === 'cron' && schedule.expression) return String(schedule.expression)
  if (type === 'interval' && schedule.every_seconds != null) return `every ${schedule.every_seconds}s`
  if (type === 'once' && schedule.at) return String(schedule.at)
  return '—'
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="truncate font-mono text-sm text-foreground">{value || '—'}</div>
    </div>
  )
}

/** Read-only schedule detail drawer (SCHED1). `schedules.json` carries no
 *  authenticated write path exposed through arcui today — the only mutation
 *  surface is the agent's own scheduler capability tools (create/update/delete
 *  schedule), which run through the agent's tool-call loop, not an HTTP route
 *  arcui can call. Editing cron/prompt here would mean inventing a scheduler
 *  mutation API with no backing implementation, so this stays view-only. */
export function ScheduleDrawer({
  schedule,
  open,
  onOpenChange,
}: {
  schedule: Dict | null
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  if (schedule == null) return null
  const metadata = (schedule.metadata ?? {}) as Dict
  const enabled = schedule.enabled !== false

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="font-mono text-sm">{String(schedule.id ?? 'schedule')}</SheetTitle>
          <SheetDescription className="flex items-center gap-2">
            <span>{String(schedule.type ?? '—')}</span>
            <span
              className={cn(
                'rounded-full px-2 py-0.5 text-[11px] font-medium',
                enabled ? 'bg-status-online/15 text-status-online' : 'bg-muted text-muted-foreground',
              )}
            >
              {enabled ? 'enabled' : 'disabled'}
            </span>
            <span className="text-muted-foreground">read-only</span>
          </SheetDescription>
        </SheetHeader>

        <div className="flex-1 space-y-5 overflow-auto p-5">
          <section className="grid grid-cols-2 gap-3">
            <Field label="Timing" value={scheduleTiming(schedule)} />
            <Field label="Timeout" value={schedule.timeout_seconds != null ? `${schedule.timeout_seconds}s` : '—'} />
            <Field label="Created by" value={String(metadata.created_by ?? '—')} />
            <Field label="Run count" value={String(metadata.run_count ?? 0)} />
            <Field label="Last run" value={String(metadata.last_run ?? '—')} />
            <Field label="Last result" value={String(metadata.last_result ?? '—')} />
          </section>

          <section className="space-y-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Prompt</h3>
            <p className="whitespace-pre-wrap rounded-lg border border-border bg-muted/20 p-3 text-sm text-foreground">
              {String(schedule.prompt ?? '—')}
            </p>
          </section>

          <section className="space-y-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Raw entry</h3>
            <JsonBlock value={schedule} />
          </section>
        </div>
      </SheetContent>
    </Sheet>
  )
}
