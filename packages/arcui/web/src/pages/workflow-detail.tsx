import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Archive, ArchiveRestore, ChevronLeft, Play, Plus, StopCircle, Trash2 } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { QueryState, EmptyState } from '@/components/states'
import { StatusText } from '@/components/status-badge'
import { RunDetailDrawer } from '@/components/run-detail-drawer'
import { WorkflowGraph, type NodeStatusUpdate } from '@/components/workflow-graph'
import {
  WorkflowNodeForm,
  fromDraft,
  toDraft,
  type NodeDraft,
} from '@/components/workflow-node-form'
import { useWorkflowRunLiveStatus } from '@/hooks/use-workflow-run-live-status'
import {
  useArchiveWorkflow,
  useCancelWorkflowRun,
  usePatchWorkflow,
  useRunWorkflow,
  useUnarchiveWorkflow,
  useWorkflow,
  useWorkflowRun,
  useWorkflowRuns,
} from '@/lib/queries'
import { ApiError } from '@/lib/api'
import { fmtTime, shortId } from '@/lib/format'
import { asWorkflowFieldErrors } from '@/lib/types'
import type { RunSummary, WorkflowDetail, WorkflowFieldError, WorkflowNode } from '@/lib/types'

const STATUS_TONE: Record<string, string> = {
  draft: 'border-status-warning/30 bg-status-warning/10 text-status-warning',
  signed: 'border-status-online/30 bg-status-online/10 text-status-online',
  archived: 'border-border bg-muted/30 text-muted-foreground',
}

function StatusPill({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex items-center rounded-md border px-1.5 py-0.5 text-[11px] font-medium capitalize ${
        STATUS_TONE[status] ?? 'border-border bg-muted/30 text-muted-foreground'
      }`}
    >
      {status}
    </span>
  )
}

/** Extracts the message + typed field errors (if any) from a failed mutation. */
function describeError(e: unknown): { message: string; fieldErrors: WorkflowFieldError[] } {
  if (e instanceof ApiError) {
    return { message: e.message, fieldErrors: asWorkflowFieldErrors(e.errors) }
  }
  return { message: 'Request failed', fieldErrors: [] }
}

/**
 * Node editor: a form over the fields each kind admits, with the raw JSON
 * underneath for anything the form does not cover.
 *
 * arcui is still not a validator — every value is relayed verbatim and the
 * control plane decides whether the node is admissible, returning a typed
 * error this panel renders against this exact node (REQ-253). The form exists
 * for discoverability: a JSON blob never told an operator that a tool node
 * needs a tool name, that a router needs routes, or that an edge is `needs`.
 */
function NodeInspector({
  node,
  allNodes,
  workflowId,
  version,
  fieldErrors,
  onValidationErrors,
  onClose,
  onDelete,
}: {
  node: WorkflowNode
  allNodes: WorkflowNode[]
  workflowId: string
  version: number
  fieldErrors: WorkflowFieldError[]
  onValidationErrors: (errors: WorkflowFieldError[]) => void
  onClose: () => void
  onDelete: () => void
}) {
  const patchWorkflow = usePatchWorkflow(workflowId)
  const [draft, setDraft] = useState<NodeDraft>(() => toDraft(node))
  const [raw, setRaw] = useState<string | null>(null)
  const [parseError, setParseError] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  const nodeErrors = fieldErrors.filter((e) => e.node_id === node.id)

  const save = async () => {
    setParseError(null)
    setSaveError(null)
    let next: Record<string, unknown>
    try {
      next = raw === null ? fromDraft(draft) : { id: node.id, ...(JSON.parse(raw) as object) }
    } catch {
      setParseError('Arguments or raw JSON is not valid JSON')
      return
    }
    // `nodes` always replaces the WHOLE array (same contract addNode/delete
    // use) — one consistent patch shape for every node mutation, never a
    // singular per-node merge.
    const nodes = allNodes.map((n) => (n.id === node.id ? next : n)) as WorkflowNode[]
    try {
      await patchWorkflow.mutateAsync({ patch: { nodes }, expectedVersion: version })
      onValidationErrors([])
      onClose()
    } catch (e) {
      const { message, fieldErrors: errs } = describeError(e)
      setSaveError(message)
      onValidationErrors(errs)
    }
  }

  return (
    <Sheet open onOpenChange={(o) => !o && onClose()}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-md">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="font-mono text-sm">{node.id}</SheetTitle>
          <SheetDescription>kind: {draft.kind}</SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-3 overflow-auto p-5">
          {nodeErrors.map((e, i) => (
            <div
              key={i}
              className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive"
            >
              <span className="font-semibold">{e.field}</span>: {e.error}
              {e.admissible && e.admissible.length > 0 && (
                <div className="mt-1 text-[11px] opacity-80">
                  admissible: {e.admissible.map(String).join(', ')}
                </div>
              )}
            </div>
          ))}
          {parseError && (
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {parseError}
            </div>
          )}
          {saveError && (
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {saveError}
            </div>
          )}
          {raw === null ? (
            <WorkflowNodeForm draft={draft} siblings={allNodes} onChange={setDraft} />
          ) : (
            <Textarea
              rows={16}
              value={raw}
              onChange={(e) => setRaw(e.target.value)}
              className="font-mono text-xs"
            />
          )}
          <button
            type="button"
            className="text-[11px] text-muted-foreground underline"
            onClick={() => {
              if (raw === null) {
                const { id: _id, ...rest } = fromDraft(draft)
                setRaw(JSON.stringify(rest, null, 2))
              } else {
                setRaw(null)
              }
            }}
          >
            {raw === null ? 'Edit as JSON' : 'Back to the form'}
          </button>
          <div className="flex items-center gap-2">
            <Button className="flex-1" disabled={patchWorkflow.isPending} onClick={save}>
              {patchWorkflow.isPending ? 'Saving…' : 'Save node'}
            </Button>
            <Button variant="ghost" className="text-destructive hover:text-destructive" onClick={onDelete}>
              <Trash2 className="size-3.5" /> Delete
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  )
}

/** Definition editing surface: the graph plus node-property editing over it
 * (DESIGN.md §8's open question resolves toward this, not drag-and-drop
 * authoring — positions are always dagre-computed, never hand-placed). */
function GraphTab({ workflow }: { workflow: WorkflowDetail }) {
  const patchWorkflow = usePatchWorkflow(workflow.id)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<WorkflowFieldError[]>([])
  const [actionError, setActionError] = useState<string | null>(null)

  const errorNodeIds = useMemo(() => new Set(fieldErrors.map((e) => e.node_id)), [fieldErrors])
  const selectedNode = selectedNodeId
    ? (workflow.nodes.find((n) => n.id === selectedNodeId) ?? null)
    : null

  const addNode = async () => {
    setActionError(null)
    const id = `node_${workflow.nodes.length + 1}`
    try {
      // An agent node is the only kind that validates with nothing but an id;
      // every other kind needs a field the operator has not typed yet, so
      // starting there would refuse the click that created it.
      await patchWorkflow.mutateAsync({
        patch: { nodes: [...workflow.nodes, { id, kind: 'agent' }] },
        expectedVersion: workflow.version,
      })
      setFieldErrors([])
      setSelectedNodeId(id)
    } catch (e) {
      const { message, fieldErrors: errs } = describeError(e)
      setActionError(message)
      setFieldErrors(errs)
    }
  }

  const deleteSelectedNode = async () => {
    if (!selectedNodeId) return
    setActionError(null)
    const nodes = workflow.nodes.filter((n) => n.id !== selectedNodeId)
    const edges = workflow.edges.filter((e) => e.from !== selectedNodeId && e.to !== selectedNodeId)
    try {
      await patchWorkflow.mutateAsync({ patch: { nodes, edges }, expectedVersion: workflow.version })
      setFieldErrors([])
      setSelectedNodeId(null)
    } catch (e) {
      const { message, fieldErrors: errs } = describeError(e)
      setActionError(message)
      setFieldErrors(errs)
    }
  }

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex items-center justify-between">
        {actionError && <span className="text-xs text-destructive">{actionError}</span>}
        <Button size="sm" variant="ghost" className="ml-auto" onClick={addNode} disabled={patchWorkflow.isPending}>
          <Plus className="size-3.5" /> Add node
        </Button>
      </div>
      <div className="min-h-[420px] flex-1 overflow-hidden rounded-lg border border-border">
        {workflow.nodes.length === 0 ? (
          <EmptyState title="No nodes yet" description="Add a node to start building the graph." />
        ) : (
          <WorkflowGraph
            workflowId={workflow.id}
            version={workflow.version}
            nodes={workflow.nodes}
            edges={workflow.edges}
            errorNodeIds={errorNodeIds}
            onNodeClick={setSelectedNodeId}
          />
        )}
      </div>
      {selectedNode && (
        <NodeInspector
          node={selectedNode}
          allNodes={workflow.nodes}
          workflowId={workflow.id}
          version={workflow.version}
          fieldErrors={fieldErrors}
          onValidationErrors={setFieldErrors}
          onClose={() => setSelectedNodeId(null)}
          onDelete={deleteSelectedNode}
        />
      )}
    </div>
  )
}

function TriggerChannelTab({ workflow }: { workflow: WorkflowDetail }) {
  const patchWorkflow = usePatchWorkflow(workflow.id)
  const [triggerText, setTriggerText] = useState(() => JSON.stringify(workflow.trigger ?? {}, null, 2))
  const [channel, setChannel] = useState(workflow.channel ?? '')
  const [error, setError] = useState<string | null>(null)

  const saveTrigger = async () => {
    setError(null)
    let trigger: Record<string, unknown>
    try {
      trigger = JSON.parse(triggerText) as Record<string, unknown>
    } catch {
      setError('Trigger is not valid JSON')
      return
    }
    try {
      await patchWorkflow.mutateAsync({ patch: { trigger }, expectedVersion: workflow.version })
    } catch (e) {
      setError(describeError(e).message)
    }
  }

  const saveChannel = async () => {
    setError(null)
    try {
      await patchWorkflow.mutateAsync({ patch: { channel }, expectedVersion: workflow.version })
    } catch (e) {
      setError(describeError(e).message)
    }
  }

  return (
    <div className="mx-auto max-w-xl space-y-6">
      {error && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {error}
        </div>
      )}
      <div className="space-y-1.5">
        <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          Trigger (JSON)
        </label>
        <Textarea
          rows={8}
          value={triggerText}
          onChange={(e) => setTriggerText(e.target.value)}
          className="font-mono text-xs"
        />
        <Button size="sm" onClick={saveTrigger} disabled={patchWorkflow.isPending}>
          Save trigger
        </Button>
      </div>
      <div className="space-y-1.5">
        <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          Bound channel
        </label>
        <Input value={channel} onChange={(e) => setChannel(e.target.value)} placeholder="workflow-onboarding" />
        <Button size="sm" onClick={saveChannel} disabled={patchWorkflow.isPending}>
          Save channel
        </Button>
      </div>
    </div>
  )
}

function VersionsTab({ workflow }: { workflow: WorkflowDetail }) {
  const versions = workflow.versions ?? []
  if (versions.length === 0) return <EmptyState title="No version history yet" />
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Version</TableHead>
          <TableHead>Signer</TableHead>
          <TableHead>Reason</TableHead>
          <TableHead>Created</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {versions.map((v) => (
          <TableRow key={v.version}>
            <TableCell>v{v.version}</TableCell>
            <TableCell className="font-mono text-xs">{v.signer ?? 'unsigned draft'}</TableCell>
            <TableCell>{v.reason ?? '—'}</TableCell>
            <TableCell>{v.created_at ? fmtTime(v.created_at) : '—'}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

/** Live per-node status for one selected run, computed from the REST
 * snapshot (backfill) plus the workflow channel's live frames — reused via
 * `useWorkflowRunLiveStatus` instead of a second polling loop (DESIGN.md §8).
 * A node absent from the run's reached set renders `skipped` once the run is
 * terminal, `pending` while still in flight — lazy materialization means an
 * untaken branch never gets a task row to read a status from.
 */
function RunGraph({ workflow, runId }: { workflow: WorkflowDetail; runId: string }) {
  const run = useWorkflowRun(runId, 4000)
  const liveStatus = useWorkflowRunLiveStatus(workflow.channel ?? null, runId)
  const [timelineRun, setTimelineRun] = useState<RunSummary | null>(null)

  const reached = useMemo(() => {
    const map = new Map(run.data?.nodes.map((n) => [n.node_id, n]))
    for (const [nodeId, s] of Object.entries(liveStatus)) map.set(nodeId, s)
    return map
  }, [run.data, liveStatus])

  const nodeStatus = useMemo<Record<string, NodeStatusUpdate>>(() => {
    const terminal = run.data ? ['done', 'failed', 'cancelled'].includes(run.data.status) : false
    const out: Record<string, NodeStatusUpdate> = {}
    for (const n of workflow.nodes) {
      const r = reached.get(n.id)
      out[n.id] = r
        ? { status: r.status, iteration: r.iteration, max_iterations: r.max_iterations }
        : { status: terminal ? 'skipped' : 'pending' }
    }
    return out
  }, [workflow.nodes, reached, run.data])

  const openTimeline = (nodeId: string) => {
    const taskRunId = reached.get(nodeId)?.task_run_id
    if (!taskRunId) return
    setTimelineRun({
      run_id: taskRunId,
      agent: nodeId,
      turns: 0,
      tool_calls: 0,
      llm_calls: 0,
      prompt_tokens: 0,
      completion_tokens: 0,
      total_tokens: 0,
      cost_usd: 0,
      status: nodeStatus[nodeId]?.status ?? 'pending',
    })
  }

  return (
    <>
      <div className="min-h-[380px] overflow-hidden rounded-lg border border-border">
        <WorkflowGraph
          workflowId={workflow.id}
          version={workflow.version}
          nodes={workflow.nodes}
          edges={workflow.edges}
          nodeStatus={nodeStatus}
          onNodeClick={openTimeline}
        />
      </div>
      <RunDetailDrawer
        run={timelineRun}
        open={timelineRun !== null}
        onOpenChange={(o) => !o && setTimelineRun(null)}
      />
    </>
  )
}

function RunsTab({ workflow }: { workflow: WorkflowDetail }) {
  const runs = useWorkflowRuns(workflow.id)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const cancelRun = useCancelWorkflowRun(selectedRunId ?? '')

  return (
    <QueryState
      query={runs}
      isEmpty={(data) => data.runs.length === 0}
      empty={<EmptyState title="No runs yet" description="Start one from the header's Run button." />}
    >
      {(data) => (
        <div className="space-y-3">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Run</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Started</TableHead>
                <TableHead>Ended</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.runs.map((r) => (
                <TableRow
                  key={r.run_id}
                  className="cursor-pointer"
                  data-state={selectedRunId === r.run_id ? 'selected' : undefined}
                  onClick={() => setSelectedRunId(r.run_id === selectedRunId ? null : r.run_id)}
                >
                  <TableCell className="font-mono text-xs">{shortId(r.run_id, 18)}</TableCell>
                  <TableCell>
                    <StatusText value={r.status} />
                  </TableCell>
                  <TableCell>{r.started_at ? fmtTime(r.started_at) : '—'}</TableCell>
                  <TableCell>{r.ended_at ? fmtTime(r.ended_at) : '—'}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {selectedRunId && (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <span className="font-mono text-xs text-muted-foreground">{shortId(selectedRunId, 18)}</span>
                <Button
                  size="sm"
                  variant="ghost"
                  className="text-destructive hover:text-destructive"
                  disabled={cancelRun.isPending}
                  onClick={() => cancelRun.mutate()}
                >
                  <StopCircle className="size-3.5" /> Cancel run
                </Button>
              </div>
              <RunGraph key={selectedRunId} workflow={workflow} runId={selectedRunId} />
            </div>
          )}
        </div>
      )}
    </QueryState>
  )
}

export function WorkflowDetailPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const workflow = useWorkflow(id)
  const archiveWorkflow = useArchiveWorkflow(id)
  const unarchiveWorkflow = useUnarchiveWorkflow(id)
  const runWorkflow = useRunWorkflow(id)
  const [tab, setTab] = useState('graph')
  const [actionError, setActionError] = useState<string | null>(null)

  const runNow = async () => {
    setActionError(null)
    try {
      await runWorkflow.mutateAsync(undefined)
      setTab('runs')
    } catch (e) {
      setActionError(describeError(e).message)
    }
  }

  const toggleArchive = async () => {
    setActionError(null)
    try {
      if (workflow.data?.status === 'archived') await unarchiveWorkflow.mutateAsync()
      else await archiveWorkflow.mutateAsync()
    } catch (e) {
      setActionError(describeError(e).message)
    }
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title={
          <span className="flex items-center gap-2">
            <Button variant="ghost" size="icon" onClick={() => navigate('/workflows')}>
              <ChevronLeft className="size-4" />
            </Button>
            {workflow.data?.name ?? id}
          </span>
        }
        description={
          workflow.data ? (
            <span className="flex items-center gap-2">
              <StatusPill status={workflow.data.status} />
              <span>v{workflow.data.version}</span>
            </span>
          ) : (
            'Loading…'
          )
        }
        actions={
          <div className="flex items-center gap-2">
            {actionError && <span className="text-xs text-destructive">{actionError}</span>}
            <Button
              size="sm"
              onClick={runNow}
              disabled={workflow.data?.status !== 'signed' || runWorkflow.isPending}
              title={workflow.data?.status !== 'signed' ? 'Only a signed workflow can run' : undefined}
            >
              <Play className="size-3.5" /> Run
            </Button>
            <Button size="sm" variant="ghost" onClick={toggleArchive}>
              {workflow.data?.status === 'archived' ? (
                <>
                  <ArchiveRestore className="size-3.5" /> Unarchive
                </>
              ) : (
                <>
                  <Archive className="size-3.5" /> Archive
                </>
              )}
            </Button>
          </div>
        }
      />
      <div className="flex-1 overflow-auto p-6">
        <QueryState query={workflow}>
          {(data) => (
            <Tabs value={tab} onValueChange={setTab} className="flex h-full flex-col">
              <TabsList>
                <TabsTrigger value="graph">Graph</TabsTrigger>
                <TabsTrigger value="trigger">Trigger &amp; Channel</TabsTrigger>
                <TabsTrigger value="versions">Versions</TabsTrigger>
                <TabsTrigger value="runs">Runs</TabsTrigger>
              </TabsList>
              <TabsContent value="graph" className="flex-1">
                <GraphTab workflow={data} />
              </TabsContent>
              <TabsContent value="trigger">
                <TriggerChannelTab workflow={data} />
              </TabsContent>
              <TabsContent value="versions">
                <VersionsTab workflow={data} />
              </TabsContent>
              <TabsContent value="runs">
                <RunsTab workflow={data} />
              </TabsContent>
            </Tabs>
          )}
        </QueryState>
      </div>
    </div>
  )
}
