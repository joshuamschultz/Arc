import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Pencil, Check, X } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Button } from '@/components/ui/button'
import { QueryState, EmptyState } from '@/components/states'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { apiPatch, ApiError } from '@/lib/api'
import { useRoster, useAgentConfigFile } from '@/lib/queries'
import type { Dict } from '@/lib/types'

// The three per-agent config files, one editor tab each.
const CONFIG_FILES = [
  { key: 'arcagent', label: 'ArcAgent' },
  { key: 'arcllm', label: 'ArcLLM' },
  { key: 'arcrun', label: 'ArcRun' },
] as const

function ReadValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-muted-foreground">—</span>
  if (Array.isArray(value)) {
    return value.length === 0 ? (
      <span className="text-muted-foreground">[]</span>
    ) : (
      <span className="font-mono text-xs">{value.map((v) => String(v)).join(', ')}</span>
    )
  }
  if (typeof value === 'object') return <KeyTree obj={value as Dict} />
  return <span className="font-mono text-xs text-foreground">{String(value)}</span>
}

function KeyTree({ obj }: { obj: Dict }) {
  const entries = Object.entries(obj)
  if (entries.length === 0) return <span className="text-muted-foreground">{'{}'}</span>
  return (
    <div className="space-y-1 border-l border-border pl-3">
      {entries.map(([k, v]) => (
        <div key={k} className="flex flex-wrap items-baseline gap-2 text-sm">
          <span className="text-muted-foreground">{k}:</span>
          <ReadValue value={v} />
        </div>
      ))}
    </div>
  )
}

function ConfigSection({
  endpoint,
  queryKey,
  sectionKey,
  value,
  editable,
}: {
  endpoint: string
  queryKey: unknown[]
  sectionKey: string
  value: unknown
  editable: boolean
}) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const startEdit = () => {
    setDraft(JSON.stringify(value, null, 2))
    setError(null)
    setEditing(true)
  }

  const save = async () => {
    let parsed: unknown
    try {
      parsed = JSON.parse(draft)
    } catch {
      setError('Invalid JSON')
      return
    }
    setSaving(true)
    try {
      await apiPatch(endpoint, { [sectionKey]: parsed })
      await queryClient.invalidateQueries({ queryKey })
      setEditing(false)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="rounded-lg border border-border bg-card p-4 shadow-xs transition-colors duration-150 hover:border-border/80">
      <div className="mb-2 flex items-center justify-between border-b border-border pb-2">
        <h3 className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs font-semibold text-foreground">
          {sectionKey}
        </h3>
        {editable && !editing && (
          <Button variant="ghost" size="sm" onClick={startEdit}>
            <Pencil className="size-3.5" /> Edit
          </Button>
        )}
        {editing && (
          <div className="flex gap-1">
            <Button variant="ghost" size="sm" onClick={() => setEditing(false)} disabled={saving}>
              <X className="size-3.5" /> Cancel
            </Button>
            <Button size="sm" onClick={save} disabled={saving}>
              <Check className="size-3.5" /> Save
            </Button>
          </div>
        )}
      </div>
      {editing ? (
        <div className="space-y-1 pt-3">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            className="h-48 w-full rounded-md border border-border bg-muted/30 p-2 font-mono text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          />
          {error && <p className="text-xs text-destructive">{error}</p>}
        </div>
      ) : (
        <div className="pt-3">
          <ReadValue value={value} />
        </div>
      )}
    </div>
  )
}

function ConfigFilePanel({
  agentId,
  file,
  label,
  editable,
}: {
  agentId: string
  file: string
  label: string
  editable: boolean
}) {
  const query = useAgentConfigFile(agentId, file)
  const endpoint = `/api/agents/${agentId}/config/${file}`
  const queryKey = ['agent', agentId, 'config', file]

  return (
    <QueryState
      query={query}
      isEmpty={(data) => !data.sections || Object.keys(data.sections).length === 0}
      empty={
        <EmptyState
          title={`No ${file}.toml for this agent`}
          description={`This agent has no ${label} config file (${file}.toml). Nothing to edit here.`}
        />
      }
    >
      {(data) => (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {Object.entries(data.sections).map(([key, value]) => (
            <ConfigSection
              key={key}
              endpoint={endpoint}
              queryKey={queryKey}
              sectionKey={key}
              value={value}
              editable={editable}
            />
          ))}
        </div>
      )}
    </QueryState>
  )
}

export function SettingsPage() {
  const roster = useRoster()
  const agents = (roster.data?.agents ?? []).filter((a) => !a.hidden)
  const [picked, setPicked] = useState<string | null>(null)
  const agentId = picked ?? agents[0]?.agent_id ?? null
  const [operatorMode] = useOperatorMode()

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Settings"
        description="Per-agent config editor — arcagent.toml, arcllm.toml, arcrun.toml."
        actions={
          <>
            <OperatorModeToggle />
            <Select value={agentId ?? ''} onValueChange={setPicked}>
              <SelectTrigger className="w-52">
                <SelectValue placeholder="Select agent" />
              </SelectTrigger>
              <SelectContent>
                {agents.map((a) => (
                  <SelectItem key={a.agent_id} value={a.agent_id ?? ''}>
                    {a.display_name || a.name || a.agent_id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </>
        }
      />
      {!agentId ? (
        <div className="flex-1 overflow-auto p-6">
          <EmptyState
            title="No agent selected"
            description="Pick an agent from the selector to view and edit its config."
          />
        </div>
      ) : (
        <Tabs defaultValue="arcagent" className="flex flex-1 flex-col overflow-hidden">
          <div className="border-b border-border px-6">
            <TabsList className="my-2">
              {CONFIG_FILES.map((f) => (
                <TabsTrigger key={f.key} value={f.key}>
                  {f.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </div>
          {CONFIG_FILES.map((f) => (
            <TabsContent key={f.key} value={f.key} className="flex-1 overflow-auto p-6">
              <ConfigFilePanel
                agentId={agentId}
                file={f.key}
                label={f.label}
                editable={operatorMode}
              />
            </TabsContent>
          ))}
        </Tabs>
      )}
    </div>
  )
}
