import { useState, type FormEvent } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { AuditFilters } from '@/lib/audit-query'

// The filters that follow a row's causal chain. Typed values are held as a draft
// and sent only on Apply, so each keystroke does not re-query the ledger.

type CausalKey = Exclude<keyof AuditFilters, 'filter'>

const TEXT_FIELDS: { key: CausalKey; label: string }[] = [
  { key: 'agent', label: 'Agent' },
  { key: 'run_id', label: 'Run ID' },
  { key: 'tool_call_id', label: 'Tool call ID' },
  { key: 'llm_call_id', label: 'LLM call ID' },
  { key: 'workflow_run_id', label: 'Workflow run ID' },
  { key: 'connection_id', label: 'Connection ID' },
]

const INITIATORS = [
  { value: '', label: 'Any initiator' },
  { value: 'agent', label: 'Agent' },
  { value: 'operator', label: 'Operator' },
  { value: 'ui_session', label: 'UI session' },
  { value: 'scheduler', label: 'Scheduler' },
  { value: 'workflow', label: 'Workflow' },
  { value: 'connector_probe', label: 'Connector probe' },
  { value: 'system', label: 'System' },
]

export function CausalFilterBar({
  value,
  onApply,
}: {
  value: AuditFilters
  onApply: (next: AuditFilters) => void
}) {
  const [draft, setDraft] = useState<AuditFilters>(value)
  const set = (key: CausalKey, v: string) => setDraft((d) => ({ ...d, [key]: v }))

  const submit = (e: FormEvent) => {
    e.preventDefault()
    onApply(draft)
  }
  const clear = () => {
    const cleared: AuditFilters = { filter: value.filter }
    setDraft(cleared)
    onApply(cleared)
  }

  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-3" aria-label="Audit filters">
      <label className="grid gap-1 text-[11px] font-medium text-muted-foreground">
        Initiator
        <select
          value={draft.initiator ?? ''}
          onChange={(e) => set('initiator', e.target.value)}
          className="h-8 rounded-md border border-border bg-background px-2 text-xs text-foreground"
        >
          {INITIATORS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      {TEXT_FIELDS.map(({ key, label }) => (
        <label key={key} className="grid gap-1 text-[11px] font-medium text-muted-foreground">
          {label}
          <Input
            value={draft[key] ?? ''}
            onChange={(e) => set(key, e.target.value)}
            className="h-8 w-40 font-mono text-xs"
          />
        </label>
      ))}
      <Button type="submit" size="sm">
        Apply filters
      </Button>
      <Button type="button" size="sm" variant="ghost" onClick={clear}>
        Clear
      </Button>
    </form>
  )
}
