import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Lock, Pencil, Check, X } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Button } from '@/components/ui/button'
import { QueryState } from '@/components/states'
import { apiPatch, ApiError } from '@/lib/api'
import { useArcllmConfig, useViewerConfig } from '@/lib/queries'
import { useConnectionStore } from '@/store/connection'
import type { Dict } from '@/lib/types'

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
  sectionKey,
  value,
  editable,
}: {
  endpoint: string
  sectionKey: string
  value: unknown
  editable: boolean
}) {
  const queryClient = useQueryClient()
  const queryKey = endpoint === '/api/arcllm-config' ? ['arcllm-config'] : ['config']
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
    <div className="rounded-xl border border-border bg-card p-4 shadow-xs">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="font-mono text-sm font-semibold text-foreground">{sectionKey}</h3>
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
        <div className="space-y-1">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            className="h-48 w-full rounded-lg border border-border bg-muted/30 p-2 font-mono text-xs text-foreground outline-none focus:ring-1 focus:ring-ring"
          />
          {error && <p className="text-xs text-destructive">{error}</p>}
        </div>
      ) : (
        <ReadValue value={value} />
      )}
    </div>
  )
}

function ConfigPanel({ endpoint, query, editable }: { endpoint: string; query: ReturnType<typeof useViewerConfig>; editable: boolean }) {
  return (
    <QueryState query={query} isEmpty={() => !query.data || Object.keys(query.data).length === 0}>
      {(cfg) => (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {Object.entries(cfg).map(([key, value]) => (
            <ConfigSection key={key} endpoint={endpoint} sectionKey={key} value={value} editable={editable} />
          ))}
        </div>
      )}
    </QueryState>
  )
}

export function SettingsPage() {
  const role = useConnectionStore((s) => s.role)
  const editable = role === 'operator'
  const arcllm = useArcllmConfig()
  const viewer = useViewerConfig()

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Settings"
        description="ArcLLM and viewer configuration."
        actions={
          <span className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 text-xs text-muted-foreground">
            <Lock className="size-3.5" />
            {editable ? 'Operator — editable' : 'Viewer — read-only'}
          </span>
        }
      />
      <Tabs defaultValue="arcllm" className="flex flex-1 flex-col overflow-hidden">
        <div className="border-b border-border px-6">
          <TabsList className="my-2">
            <TabsTrigger value="arcllm">ArcLLM</TabsTrigger>
            <TabsTrigger value="viewer">Viewer</TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="arcllm" className="flex-1 overflow-auto p-6">
          <ConfigPanel endpoint="/api/arcllm-config" query={arcllm} editable={editable} />
        </TabsContent>
        <TabsContent value="viewer" className="flex-1 overflow-auto p-6">
          <ConfigPanel endpoint="/api/config" query={viewer} editable={editable} />
        </TabsContent>
      </Tabs>
    </div>
  )
}
