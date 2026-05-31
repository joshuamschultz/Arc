import { useMemo, useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { StatusDot } from '@/components/status-badge'
import { StatCard } from '@/components/stat-card'
import { DataTable } from '@/components/data-table'
import { TraceTable } from '@/components/trace-table'
import { FileTree } from '@/components/file-tree'
import { Markdown } from '@/components/markdown'
import { JsonBlock } from '@/components/json-block'
import { PolicyBulletCard } from '@/components/policy-bullet'
import { RunReplayDrawer } from '@/components/run-replay-drawer'
import { QueryState, EmptyState } from '@/components/states'
import { useActiveAgent } from '@/hooks/use-arc-socket'
import { useLiveStore } from '@/store/live'
import {
  useAgent,
  useAgentConfig,
  useAgentPolicy,
  useAgentPolicyStats,
  useAgentSessions,
  useAgentSkills,
  useAgentTools,
  useAgentTraces,
  useKnowledge,
} from '@/lib/queries'
import { fmtBytes, initials, relativeTime, shortId } from '@/lib/format'
import type { ColumnDef } from '@tanstack/react-table'
import type { Dict, Trace } from '@/lib/types'

const TABS = [
  'overview', 'identity', 'runs', 'llm', 'skills', 'tools', 'policy', 'memory', 'files',
] as const
type TabId = (typeof TABS)[number]

function KV({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 rounded-lg border border-border bg-card p-3">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="break-all font-mono text-sm text-foreground">{value || '—'}</span>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      {children}
    </section>
  )
}

// --- Tabs ------------------------------------------------------------------

function OverviewTab({ agentId }: { agentId: string }) {
  const q = useAgent(agentId)
  return (
    <QueryState query={q} isEmpty={() => !q.data}>
      {(a) => (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <KV label="Model" value={String(a.model ?? '')} />
          <KV label="Provider" value={String(a.provider ?? '')} />
          <KV label="Type" value={String(a.type ?? '')} />
          <KV label="DID" value={String(a.did ?? '')} />
          <KV label="Workspace" value={String(a.workspace ?? a.workspace_path ?? '')} />
          <KV label="Tools" value={Array.isArray(a.tools) ? a.tools.length : '—'} />
          <KV label="Modules" value={Array.isArray(a.modules) ? a.modules.length : '—'} />
          <KV label="Org" value={String(a.org ?? '')} />
          <KV label="Role" value={String(a.role_label ?? '')} />
        </div>
      )}
    </QueryState>
  )
}

function IdentityTab({ agentId }: { agentId: string }) {
  const agent = useAgent(agentId)
  const config = useAgentConfig(agentId)
  const tools = useAgentTools(agentId)
  const a = agent.data ?? {}
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <KV label="DID" value={String(a.did ?? '')} />
        <KV label="Model" value={String(a.model ?? '')} />
        <KV label="Provider" value={String(a.provider ?? '')} />
      </div>
      <Section title="Tool policy">
        <div className="flex flex-wrap gap-3">
          <div className="flex-1 rounded-lg border border-border bg-card p-3">
            <div className="mb-1 text-xs font-medium text-status-online">Allowlist</div>
            <div className="flex flex-wrap gap-1">
              {(tools.data?.allowlist ?? []).length === 0 ? (
                <span className="text-xs text-muted-foreground">—</span>
              ) : (
                tools.data!.allowlist.map((t) => (
                  <span key={t} className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px]">{t}</span>
                ))
              )}
            </div>
          </div>
          <div className="flex-1 rounded-lg border border-border bg-card p-3">
            <div className="mb-1 text-xs font-medium text-status-error">Denylist</div>
            <div className="flex flex-wrap gap-1">
              {(tools.data?.denylist ?? []).length === 0 ? (
                <span className="text-xs text-muted-foreground">—</span>
              ) : (
                tools.data!.denylist.map((t) => (
                  <span key={t} className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px]">{t}</span>
                ))
              )}
            </div>
          </div>
        </div>
      </Section>
      {config.data?.raw && (
        <Section title="Config">
          <JsonBlock value={config.data.raw} className="max-h-80" />
        </Section>
      )}
    </div>
  )
}

const runColumns: ColumnDef<Dict, unknown>[] = [
  { accessorKey: 'sid', header: 'Run', cell: (c) => <span className="font-mono text-xs text-primary">{shortId(c.getValue() as string, 18)}</span> },
  { accessorKey: 'size', header: 'Size', cell: (c) => <span className="font-mono text-xs tabular-nums text-muted-foreground">{fmtBytes(c.getValue() as number)}</span> },
  { accessorKey: 'mtime', header: 'Updated', cell: (c) => <span className="text-xs text-muted-foreground">{relativeTime(c.getValue() as number)}</span> },
]

function RunsTab({ agentId }: { agentId: string }) {
  const q = useAgentSessions(agentId)
  const [active, setActive] = useState<string | null>(null)
  const rows = (q.data?.sessions ?? []) as unknown as Dict[]
  return (
    <>
      <QueryState query={q} isEmpty={() => rows.length === 0}
        empty={<EmptyState title="No runs for this agent yet" />}>
        {() => (
          <DataTable columns={runColumns} data={rows} searchable searchPlaceholder="Search runs…"
            onRowClick={(r) => setActive(String(r.sid))} emptyTitle="No runs" />
        )}
      </QueryState>
      <RunReplayDrawer agentId={agentId} sid={active} open={!!active} onOpenChange={(o) => !o && setActive(null)} />
    </>
  )
}

function LlmTab({ agentId }: { agentId: string }) {
  const q = useAgentTraces(agentId)
  const live = useLiveStore((s) => s.traces)
  const traces = useMemo(() => {
    const rest = q.data?.traces ?? []
    const mine = live.filter((t) => t.agent === agentId || t.agent_label === agentId)
    const seen = new Set<string>()
    const out: Trace[] = []
    for (const t of [...mine, ...rest]) {
      const id = t.trace_id ?? `${t.timestamp}-${t.model}`
      if (seen.has(id)) continue
      seen.add(id)
      out.push(t)
    }
    return out
  }, [q.data, live, agentId])
  return <TraceTable traces={traces} />
}

function SkillsTab({ agentId }: { agentId: string }) {
  const q = useAgentSkills(agentId)
  const skills = (q.data?.skills ?? []) as Dict[]
  return (
    <QueryState query={q} isEmpty={() => skills.length === 0}
      empty={<EmptyState title="No skills" />}>
      {() => (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {skills.map((s, i) => (
            <div key={i} className="rounded-xl border border-border bg-card p-4 shadow-xs">
              <div className="font-medium text-foreground">{String(s.name ?? 'unnamed')}</div>
              {s.description != null && <p className="mt-1 line-clamp-3 text-xs text-muted-foreground">{String(s.description)}</p>}
            </div>
          ))}
        </div>
      )}
    </QueryState>
  )
}

const toolColumns: ColumnDef<Dict, unknown>[] = [
  { accessorKey: 'name', header: 'Tool', cell: (c) => <span className="font-mono text-xs text-foreground">{String(c.getValue() ?? '—')}</span> },
  { accessorKey: 'description', header: 'Description', cell: (c) => <span className="text-xs text-muted-foreground">{String(c.getValue() ?? '')}</span> },
]

function ToolsTab({ agentId }: { agentId: string }) {
  const q = useAgentTools(agentId)
  const tools = (q.data?.tools ?? []) as Dict[]
  return (
    <QueryState query={q} isEmpty={() => tools.length === 0}
      empty={<EmptyState title="No tools registered" />}>
      {() => <DataTable columns={toolColumns} data={tools} searchable searchPlaceholder="Search tools…" />}
    </QueryState>
  )
}

function PolicyTab({ agentId }: { agentId: string }) {
  const policy = useAgentPolicy(agentId)
  const stats = useAgentPolicyStats(agentId)
  const bullets = policy.data?.bullets ?? []
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard label="Total" value={stats.data?.total ?? 0} />
        <StatCard label="Active" value={stats.data?.active ?? 0} />
        <StatCard label="Retired" value={stats.data?.retired ?? 0} />
        <StatCard label="Avg score" value={(stats.data?.avg_score ?? 0).toFixed(2)} />
      </div>
      <QueryState query={policy} isEmpty={() => bullets.length === 0}
        empty={<EmptyState title="No policy bullets" />}>
        {() => <div className="space-y-2">{bullets.map((b, i) => <PolicyBulletCard key={i} bullet={b} />)}</div>}
      </QueryState>
      {policy.data?.raw && (
        <Section title="Policy document">
          <div className="rounded-xl border border-border bg-card p-4">
            <Markdown>{policy.data.raw}</Markdown>
          </div>
        </Section>
      )}
    </div>
  )
}

function MemoryTab({ agentId }: { agentId: string }) {
  const q = useKnowledge(agentId)
  return (
    <QueryState query={q} isEmpty={() => !q.data}>
      {(data) => (
        <div className="space-y-5">
          <Section title="Context">
            <JsonBlock value={data.context ?? {}} className="max-h-60" />
          </Section>
          <Section title="Memory">
            <JsonBlock value={data.memory ?? {}} className="max-h-96" />
          </Section>
        </div>
      )}
    </QueryState>
  )
}

const TAB_RENDER: Record<TabId, (agentId: string) => ReactNode> = {
  overview: (id) => <OverviewTab agentId={id} />,
  identity: (id) => <IdentityTab agentId={id} />,
  runs: (id) => <RunsTab agentId={id} />,
  llm: (id) => <LlmTab agentId={id} />,
  skills: (id) => <SkillsTab agentId={id} />,
  tools: (id) => <ToolsTab agentId={id} />,
  policy: (id) => <PolicyTab agentId={id} />,
  memory: (id) => <MemoryTab agentId={id} />,
  files: (id) => <FileTree agentId={id} />,
}

const TAB_LABEL: Record<TabId, string> = {
  overview: 'Overview', identity: 'Identity', runs: 'Runs', llm: 'LLM', skills: 'Skills',
  tools: 'Tools', policy: 'Policy', memory: 'Memory', files: 'Files',
}

export function AgentDetailPage() {
  const { id = '', tab } = useParams()
  const navigate = useNavigate()
  const current: TabId = (TABS.includes(tab as TabId) ? tab : 'overview') as TabId
  const agent = useAgent(id)
  useActiveAgent(id)

  const a = agent.data ?? {}
  const label = String(a.display_name || a.name || id)

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-border px-6 py-4">
        <button
          type="button"
          onClick={() => navigate('/agents')}
          className="flex size-8 items-center justify-center rounded-lg border border-border text-muted-foreground hover:bg-muted/50 hover:text-foreground"
          aria-label="Back to agents"
        >
          <ArrowLeft className="size-4" />
        </button>
        <span
          className="flex size-9 items-center justify-center rounded-lg text-sm font-semibold text-primary-foreground"
          style={{ background: (a.color as string) || 'var(--primary)' }}
        >
          {initials(label)}
        </span>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-lg font-semibold tracking-tight text-foreground">{label}</h1>
          {a.did != null && <div className="truncate font-mono text-xs text-muted-foreground">{String(a.did)}</div>}
        </div>
        <StatusDot online={Boolean(a.online)} degraded={Boolean(a.degraded)} />
      </div>

      <Tabs value={current} onValueChange={(v) => navigate(`/agents/${id}/${v}`)} className="border-b border-border px-6">
        <TabsList className="my-2 flex-wrap">
          {TABS.map((t) => (
            <TabsTrigger key={t} value={t}>{TAB_LABEL[t]}</TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      <div className="flex-1 overflow-auto p-6">{TAB_RENDER[current](id)}</div>
    </div>
  )
}
