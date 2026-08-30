import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { BookOpen } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { QueryState, EmptyState } from '@/components/states'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import { KnowledgeOverview } from '@/components/knowledge-view/overview'
import { GraphViewer } from '@/components/knowledge-graph'
import { MemoryBrowser } from '@/components/knowledge-memories'
import { ChunkBrowser } from '@/components/knowledge-chunks'
import { EntityBrowser } from '@/components/knowledge-entities'
import { InsightBrowser } from '@/components/knowledge-insights'
import { ProcedureBrowser } from '@/components/knowledge-procedures'
import { EventBrowser } from '@/components/knowledge-events'
import { DailyNotesBrowser } from '@/components/knowledge-daily-notes'
import { ConnectionsBrowser } from '@/components/knowledge-connections'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useKnowledge, useRoster } from '@/lib/queries'

const TABS = [
  { value: 'overview', label: 'Overview' },
  { value: 'graph', label: 'Graph' },
  { value: 'insights', label: 'Insights' },
  { value: 'procedures', label: 'Procedures' },
  { value: 'entities', label: 'Entities' },
  { value: 'events', label: 'Events' },
  { value: 'daily-notes', label: 'Daily Notes' },
  { value: 'connections', label: 'Connections' },
  { value: 'memories', label: 'Raw stream' },
  { value: 'chunks', label: 'Chunks' },
]

export function KnowledgePage() {
  const [searchParams] = useSearchParams()
  const roster = useRoster()
  const agents = (roster.data?.agents ?? []).filter((a) => !a.hidden)
  const [picked, setPicked] = useState<string | null>(searchParams.get('agent'))
  const agentId = picked ?? agents[0]?.agent_id ?? null
  const setAgentId = setPicked

  const query = useKnowledge(agentId)
  const [selectedEntitySlug, setSelectedEntitySlug] = useState<string | null>(null)
  const [tab, setTab] = useState(searchParams.get('tab') ?? 'overview')

  const focusEntity = (slug: string) => {
    setSelectedEntitySlug(slug)
    setTab('entities')
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Knowledge"
        description="What an agent has learned — memory, entities, insights, and its knowledge graph."
        actions={
          <>
            <OperatorModeToggle />
            <Select value={agentId ?? ''} onValueChange={setAgentId}>
              <SelectTrigger className="w-52">
                <SelectValue placeholder="Select agent" />
              </SelectTrigger>
              <SelectContent>
                {/* An agent with no id cannot be selected, and Radix throws on an
                    empty Select value — which takes the whole page down rather than
                    dropping one unusable row. */}
                {agents
                  .filter((a): a is typeof a & { agent_id: string } => Boolean(a.agent_id))
                  .map((a) => (
                    <SelectItem key={a.agent_id} value={a.agent_id}>
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
              {TABS.map((t) => (
                <TabsTrigger key={t.value} value={t.value}>
                  {t.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </div>

          <TabsContent value="overview" className="flex-1 overflow-auto p-6">
            <QueryState query={query} isEmpty={() => !query.data}>
              {(data) => (
                <KnowledgeOverview data={data} agentId={agentId} onNavigate={setTab} />
              )}
            </QueryState>
          </TabsContent>

          <TabsContent value="graph" className="flex-1 overflow-auto p-6">
            <GraphViewer agentId={agentId} />
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

          <TabsContent value="events" className="flex-1 overflow-auto p-6">
            <EventBrowser agentId={agentId} />
          </TabsContent>

          <TabsContent value="daily-notes" className="flex-1 overflow-auto p-6">
            <DailyNotesBrowser agentId={agentId} />
          </TabsContent>

          <TabsContent value="connections" className="flex-1 overflow-auto p-6">
            <ConnectionsBrowser
              agentId={agentId}
              initialConnectionId={searchParams.get('connection')}
            />
          </TabsContent>

          <TabsContent value="memories" className="flex-1 overflow-auto p-6">
            <MemoryBrowser agentId={agentId} onNavigateEntity={focusEntity} />
          </TabsContent>

          <TabsContent value="chunks" className="flex-1 overflow-auto p-6">
            <ChunkBrowser agentId={agentId} />
          </TabsContent>
        </Tabs>
      )}
    </div>
  )
}
