import { useState } from 'react'
import { BookOpen } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { StatCard } from '@/components/stat-card'
import { JsonBlock } from '@/components/json-block'
import { FileTree } from '@/components/file-tree'
import { QueryState, EmptyState } from '@/components/states'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import { MemoryBrowser } from '@/components/knowledge-memories'
import { EntityBrowser } from '@/components/knowledge-entities'
import { InsightBrowser } from '@/components/knowledge-insights'
import { ProcedureBrowser } from '@/components/knowledge-procedures'
import { DailyNotesBrowser } from '@/components/knowledge-daily-notes'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useKnowledge, useRoster } from '@/lib/queries'

/** Uppercase micro-label used above each overview section. */
function SectionLabel({ children }: { children: string }) {
  return (
    <h2 className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
      <span aria-hidden className="h-3 w-[2px] rounded-full bg-primary/70" />
      {children}
    </h2>
  )
}

/** Render a flat dict of scalars as stat tiles; fall back to JSON. */
function ScalarStats({ data }: { data?: Record<string, unknown> }) {
  if (!data) return null
  const scalars = Object.entries(data).filter(
    ([, v]) => typeof v === 'number' || typeof v === 'string' || typeof v === 'boolean',
  )
  if (scalars.length === 0) return <JsonBlock value={data} />
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
      {scalars.map(([k, v]) => (
        <StatCard key={k} label={k.replace(/_/g, ' ')} value={String(v)} />
      ))}
    </div>
  )
}

export function KnowledgePage() {
  const roster = useRoster()
  const agents = (roster.data?.agents ?? []).filter((a) => !a.hidden)
  const [picked, setPicked] = useState<string | null>(null)
  const agentId = picked ?? agents[0]?.agent_id ?? null
  const setAgentId = setPicked

  const query = useKnowledge(agentId)
  const [selectedEntitySlug, setSelectedEntitySlug] = useState<string | null>(null)
  const [tab, setTab] = useState('overview')

  const focusEntity = (slug: string) => {
    setSelectedEntitySlug(slug)
    setTab('entities')
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Knowledge"
        description="Per-agent context budget, memory, entities, and graph."
        actions={
          <>
            <OperatorModeToggle />
            <Select value={agentId ?? ''} onValueChange={setAgentId}>
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
            icon={<BookOpen className="size-7" />}
            title="No agent selected"
            description="Register an agent and pick it above to inspect its knowledge."
          />
        </div>
      ) : (
        <Tabs value={tab} onValueChange={setTab} className="flex flex-1 flex-col overflow-hidden">
          <div className="border-b border-border px-6">
            <TabsList className="my-2">
              <TabsTrigger value="overview">Overview</TabsTrigger>
              <TabsTrigger value="insights">Insights</TabsTrigger>
              <TabsTrigger value="procedures">Procedures</TabsTrigger>
              <TabsTrigger value="entities">Entities</TabsTrigger>
              <TabsTrigger value="daily-notes">Daily Notes</TabsTrigger>
              <TabsTrigger value="memories">Raw stream</TabsTrigger>
            </TabsList>
          </div>

          <TabsContent value="overview" className="flex-1 space-y-6 overflow-auto p-6">
            <QueryState query={query} isEmpty={() => !query.data}>
              {(data) => (
                <>
                  <section className="space-y-2">
                    <SectionLabel>Context budget</SectionLabel>
                    <ScalarStats data={data.context} />
                  </section>
                  <section className="space-y-2">
                    <SectionLabel>Graph</SectionLabel>
                    <ScalarStats data={data.graph} />
                  </section>
                  <section className="space-y-2">
                    <SectionLabel>Memory</SectionLabel>
                    <JsonBlock value={data.memory ?? {}} className="max-h-72" />
                  </section>
                  <section className="space-y-2">
                    <SectionLabel>Workspace</SectionLabel>
                    <FileTree agentId={agentId} />
                  </section>
                </>
              )}
            </QueryState>
          </TabsContent>

          <TabsContent value="insights" className="flex-1 overflow-auto p-6">
            <InsightBrowser agentId={agentId} />
          </TabsContent>

          <TabsContent value="procedures" className="flex-1 overflow-auto p-6">
            <ProcedureBrowser agentId={agentId} />
          </TabsContent>

          <TabsContent value="entities" className="flex-1 overflow-auto p-6">
            <EntityBrowser
              agentId={agentId}
              selectedSlug={selectedEntitySlug}
              onSelectSlug={setSelectedEntitySlug}
            />
          </TabsContent>

          <TabsContent value="daily-notes" className="flex-1 overflow-auto p-6">
            <DailyNotesBrowser agentId={agentId} />
          </TabsContent>

          <TabsContent value="memories" className="flex-1 overflow-auto p-6">
            <MemoryBrowser agentId={agentId} onNavigateEntity={focusEntity} />
          </TabsContent>
        </Tabs>
      )}
    </div>
  )
}
