import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { PageHeader } from '@/components/page-header'
import { FieldHelp } from '@/components/help'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ErrorState } from '@/components/states'
import { ApiError } from '@/lib/api'
import {
  cancelQueueJob,
  getQueueControl,
  getQueueJobs,
  setQueueLimits,
  setQueuePaused,
  type QueueLimits,
  type QueueState,
} from '@/lib/queue'

const states: QueueState[] = [
  'queued', 'running', 'cancel_requested', 'completed', 'failed',
  'cancelled', 'timed_out', 'outcome_unknown',
]

export function QueuePage() {
  const queryClient = useQueryClient()
  const [owner, setOwner] = useState('')
  const [state, setState] = useState<QueueState | ''>('')
  const [cursors, setCursors] = useState<string[]>([])
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [now, setNow] = useState(0)
  const cursor = cursors.at(-1)
  const control = useQuery({
    queryKey: ['queue', 'control'], queryFn: getQueueControl, refetchInterval: 5000,
  })
  const jobs = useQuery({
    queryKey: ['queue', 'jobs', owner, state, cursor],
    queryFn: () => getQueueJobs({
      owner_id: owner || undefined,
      state: state || undefined,
      cursor,
      limit: 50,
    }),
    refetchInterval: 5000,
  })
  const [draft, setDraft] = useState<QueueLimits | null>(null)
  const [draftRevision, setDraftRevision] = useState<number | null>(null)
  const limits = draft ?? control.data?.limits

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  async function act(operation: () => Promise<unknown>, success: string) {
    setBusy(true)
    setNotice('')
    try {
      const result = await operation()
      setNotice(typeof result === 'string' ? result : success)
      await queryClient.invalidateQueries({ queryKey: ['queue'] })
    } catch (error) {
      setNotice(error instanceof ApiError && error.status === 409
        ? 'Queue state changed. Reload the current values before trying again.'
        : error instanceof Error ? error.message : 'Queue control failed')
      await queryClient.invalidateQueries({ queryKey: ['queue'] })
    } finally {
      setBusy(false)
    }
  }

  function changeLimit(key: keyof QueueLimits, raw: string) {
    if (!limits) return
    if (draftRevision === null) setDraftRevision(control.data?.revision ?? null)
    setDraft({ ...limits, [key]: Number(raw) })
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Model call queue"
        description="Inspect call state and control admission for this deployment."
      />
      <div className="space-y-6 overflow-auto p-6">
        {notice && <p role="status" className="rounded border border-border p-3">{notice}</p>}
        {control.error && <ErrorState error={control.error} />}
        {control.data && limits && (
          <section className="space-y-4 rounded border border-border p-4" aria-label="Queue controls">
            <div className="flex items-center gap-3">
              <h2 className="font-semibold">Admission</h2>
              <span>{control.data.paused ? 'Paused' : 'Running'}</span>
              <FieldHelp helpKey="queue.control.paused" route="queue" />
              <Button
                size="sm"
                disabled={busy}
                onClick={() => act(
                  () => setQueuePaused(!control.data.paused, control.data.revision),
                  control.data.paused ? 'Admission resumed' : 'Admission paused',
                )}
              >
                {control.data.paused ? 'Resume' : 'Pause'}
              </Button>
              <span className="text-sm text-muted-foreground">Revision {control.data.revision}</span>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {([
                ['max_concurrent', 'Concurrent calls', 'queue.limits.max_concurrent'],
                ['max_queued', 'Waiting calls', 'queue.limits.max_queued'],
                ['wait_timeout', 'Wait timeout (seconds)', 'queue.limits.wait_timeout'],
                ['history_limit', 'Stored history', 'queue.limits.history_limit'],
              ] as const).map(([key, label, helpKey]) => (
                <div key={key} className="space-y-1 text-sm">
                  <span className="flex items-center gap-1">
                    <label htmlFor={`queue-${key}`}>{label}</label>
                    <FieldHelp helpKey={helpKey} route="queue" />
                  </span>
                  <Input
                    id={`queue-${key}`}
                    type="number"
                    min={key === 'max_queued' ? 0 : 1}
                    step={key === 'wait_timeout' ? 'any' : 1}
                    value={limits[key]}
                    onChange={(event) => changeLimit(key, event.target.value)}
                  />
                </div>
              ))}
            </div>
            <Button
              size="sm"
              disabled={busy || !draft || draftRevision === null}
              onClick={() => act(
                async () => {
                  if (draftRevision === null) return
                  await setQueueLimits(limits, draftRevision)
                  setDraft(null)
                  setDraftRevision(null)
                },
                'Queue limits saved',
              )}
            >Save limits</Button>
            {draft && (
              <Button size="sm" variant="outline" onClick={() => {
                setDraft(null)
                setDraftRevision(null)
                setNotice('Current limits loaded for review')
              }}>Reload limits</Button>
            )}
          </section>
        )}

        <section className="space-y-3" aria-label="Queue jobs">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="font-semibold">Calls</h2>
            <Input
              aria-label="Filter by owner"
              placeholder="Owner ID"
              className="w-52"
              value={owner}
              onChange={(event) => { setOwner(event.target.value); setCursors([]) }}
            />
            <select
              aria-label="Filter by state"
              className="rounded border border-border bg-background p-2 text-sm"
              value={state}
              onChange={(event) => { setState(event.target.value as QueueState | ''); setCursors([]) }}
            >
              <option value="">All states</option>
              {states.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
            <FieldHelp helpKey="queue.jobs.state" route="queue" />
            <Button size="sm" variant="outline" onClick={() => jobs.refetch()}>Refresh</Button>
          </div>
          {jobs.error && <ErrorState error={jobs.error} />}
          {jobs.error && cursors.length > 0 && (
            <Button size="sm" variant="outline" onClick={() => setCursors([])}>
              Reset to first page
            </Button>
          )}
          {jobs.isPending && <p>Loading queue…</p>}
          {jobs.data && (
            <>
              <div className="overflow-x-auto rounded border border-border">
                <table className="w-full text-left text-sm">
                  <thead className="border-b border-border bg-muted/40">
                    <tr><th className="p-3">Call</th><th className="p-3">State</th><th className="p-3">Age</th><th className="p-3">Agent</th><th className="p-3">Owner</th><th className="p-3">Action</th></tr>
                  </thead>
                  <tbody>
                    {jobs.data.jobs.map((job) => (
                      <tr key={job.call_id} className="border-b border-border">
                        <td className="p-3 font-mono">{job.call_id}</td>
                        <td className="p-3">
                          {job.state}
                          <span className="block text-xs text-muted-foreground">
                            {job.state === 'queued'
                              ? (control.data?.paused ? 'Admission paused' : 'Waiting for a provider slot')
                              : job.state === 'cancel_requested'
                                ? 'Cancellation requested; completion is not confirmed'
                                : job.state === 'outcome_unknown'
                                  ? 'Provider outcome is uncertain'
                                  : ''}
                          </span>
                        </td>
                        <td className="p-3">{Math.max(0, Math.floor((now - job.created_at * 1000) / 1000))}s</td>
                        <td className="p-3">{job.agent_id ?? '—'}</td>
                        <td className="p-3 font-mono">{job.owner_id}</td>
                        <td className="p-3">
                          {(['queued', 'running', 'cancel_requested'] as string[]).includes(job.state) && (
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={busy}
                              onClick={() => act(async () => {
                                const result = await cancelQueueJob(job.call_id, job.version)
                                return result.status === 'confirmed'
                                  ? 'Cancellation confirmed'
                                  : 'Cancellation requested; the provider may still finish'
                              }, '')}
                            >Cancel</Button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {jobs.data.jobs.length === 0 && <p className="p-4">No calls match these filters.</p>}
              </div>
              <div className="flex gap-2">
                <Button size="sm" variant="outline" disabled={!cursors.length} onClick={() => setCursors(cursors.slice(0, -1))}>Previous</Button>
                <Button size="sm" variant="outline" disabled={!jobs.data.next_cursor} onClick={() => setCursors([...cursors, jobs.data!.next_cursor!])}>Next</Button>
              </div>
            </>
          )}
          <FieldHelp helpKey="queue.cancel.status" route="queue" />
        </section>
      </div>
    </div>
  )
}
