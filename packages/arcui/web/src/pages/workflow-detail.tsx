import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Archive,
  ArchiveRestore,
  Check,
  ChevronLeft,
  Pencil,
  Play,
  Plus,
  ShieldCheck,
  StopCircle,
  Trash2,
  X,
} from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { QueryState, EmptyState } from '@/components/states'
import { StatusText } from '@/components/status-badge'
import { GateCard } from '@/components/gate-card'
import { RunDetailDrawer } from '@/components/run-detail-drawer'
import { WorkflowGraph, type NodeStatusUpdate } from '@/components/workflow-graph'
import { WorkflowNodeForm } from '@/components/workflow-node-form'
import { fromDraft, toDraft, type NodeDraft } from '@/lib/workflow-node-draft'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useWorkflowRunLiveStatus } from '@/hooks/use-workflow-run-live-status'
import {
  useArchiveWorkflow,
  useCancelWorkflowRun,
  usePatchWorkflow,
  useRequestSignature,
  useRunWorkflow,
  useUnarchiveWorkflow,
  useWorkflow,
  useWorkflowFile,
  useWorkflowRun,
  useWorkflowRuns,
  useWriteWorkflowFile,
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

/** The instruction a node actually runs, editable in place.
 *
 * A node names `prompts/x.md`; the definition payload carries the path and not
 * the body, so without this the dashboard can show what a node is called and
 * never what it says — which is the part a human has to read before approving
 * a signature. Saving is a versioned definition edit: the bundle is the signed
 * unit, so changing an instruction drops the signature exactly as changing the
 * graph does.
 */
function FileBodyEditor({
  workflowId,
  version,
  path,
  label = 'prompt',
  placeholder = 'What this step should do, in plain language.',
}: {
  workflowId: string
  version: number
  path: string
  label?: string
  placeholder?: string
}) {
  const file = useWorkflowFile(workflowId, path)
  const write = useWriteWorkflowFile(workflowId)
  const [draft, setDraft] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const content = draft ?? file.data?.content ?? ''

  const save = async () => {
    setError(null)
    try {
      await write.mutateAsync({ path, content, expectedVersion: version })
      setDraft(null)
    } catch (e) {
      setError(describeError(e).message)
    }
  }

  return (
    <div className="space-y-1.5 rounded-lg border border-border p-2">
      <div className="flex items-center gap-2">
        <span className="font-mono text-[11px] text-muted-foreground">{path}</span>
        {file.isLoading && <span className="text-[11px] text-muted-foreground">loading…</span>}
        {file.isError && (
          <span className="text-[11px] text-muted-foreground">
            not written yet — type it and save
          </span>
        )}
      </div>
      {error && <div className="text-[11px] text-destructive">{error}</div>}
      <Textarea
        rows={10}
        value={content}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        className="text-xs"
      />
      <Button size="sm" variant="outline" disabled={write.isPending} onClick={save}>
        {write.isPending ? 'Saving…' : `Save ${label}`}
      </Button>
    </div>
  )
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
            <>
              <WorkflowNodeForm draft={draft} siblings={allNodes} onChange={setDraft} />
              {draft.kind === 'agent' && draft.prompt.trim() && (
                <div className="space-y-1">
                  <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    Prompt
                  </span>
                  <FileBodyEditor
                    workflowId={workflowId}
                    version={version}
                    path={draft.prompt.trim()}
                  />
                </div>
              )}
              {draft.kind === 'script' && draft.script.trim() && (
                <div className="space-y-1">
                  <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    Script
                  </span>
                  <FileBodyEditor
                    workflowId={workflowId}
                    version={version}
                    path={draft.script.trim()}
                    label="script"
                    placeholder={
                      '#!/usr/bin/env bash\n# The exact commands this step runs. ' +
                      'Print JSON on stdout to satisfy an output schema.'
                    }
                  />
                </div>
              )}
            </>
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

  /** Drawing an edge is editing the TARGET's `needs` — the graph has no edge
   *  list of its own, so this is the only thing an edge can mean. */
  const setNeeds = async (target: string, needs: string[]) => {
    setActionError(null)
    const nodes = workflow.nodes.map((n) => (n.id === target ? { ...n, needs } : n))
    try {
      await patchWorkflow.mutateAsync({ patch: { nodes }, expectedVersion: workflow.version })
      setFieldErrors([])
    } catch (e) {
      const { message, fieldErrors: errs } = describeError(e)
      setActionError(message)
      setFieldErrors(errs)
    }
  }

  const connectNodes = (source: string, target: string) => {
    const node = workflow.nodes.find((n) => n.id === target)
    const needs = node?.needs ?? []
    if (needs.includes(source)) return
    void setNeeds(target, [...needs, source])
  }

  const disconnectNodes = (source: string, target: string) => {
    const node = workflow.nodes.find((n) => n.id === target)
    void setNeeds(target, (node?.needs ?? []).filter((n) => n !== source))
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
        {actionError ? (
          <span className="text-xs text-destructive">{actionError}</span>
        ) : (
          <span className="text-[11px] text-muted-foreground">
            Drag from a node's right edge to another's left to order them. Click a node to edit it.
            Select an edge and press delete to unlink.
          </span>
        )}
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
            onConnectNodes={connectNodes}
            onDisconnectNodes={disconnectNodes}
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
  const current = (workflow.trigger ?? {}) as Record<string, string | undefined>
  const hours = (current.active_hours ?? {}) as unknown as Record<string, string | undefined>
  const [type, setType] = useState<string>(current.type ?? 'manual')
  const [expression, setExpression] = useState(current.expression ?? '')
  const [intervalS, setIntervalS] = useState(
    current.interval_s === undefined ? '' : String(current.interval_s),
  )
  const [start, setStart] = useState(hours.start ?? '')
  const [end, setEnd] = useState(hours.end ?? '')
  const [timezone, setTimezone] = useState(hours.timezone ?? '')
  const [channel, setChannel] = useState(workflow.channel ?? '')
  const [error, setError] = useState<string | null>(null)

  const saveTrigger = async () => {
    setError(null)
    // `manual` is the absence of a trigger, not a kind of one: the runner reads
    // "no trigger" as "a person starts it", so clearing is the honest write.
    const trigger: Record<string, unknown> | null =
      type === 'manual' ? null : { type }
    if (trigger) {
      if (type === 'cron' && expression.trim()) trigger.expression = expression.trim()
      if (type === 'interval' && intervalS.trim()) trigger.interval_s = Number(intervalS)
      if (start.trim() && end.trim()) {
        trigger.active_hours = { start: start.trim(), end: end.trim(), timezone: timezone.trim() }
      }
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
      <div className="space-y-3">
        <div className="space-y-1.5">
          <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            Starts
          </label>
          <Select value={type} onValueChange={setType}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="manual">when someone runs it</SelectItem>
              <SelectItem value="cron">on a schedule (cron)</SelectItem>
              <SelectItem value="interval">every N seconds</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {type === 'cron' && (
          <div className="space-y-1.5">
            <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Cron expression
            </label>
            <Input
              value={expression}
              placeholder="0 9 * * MON"
              className="font-mono text-xs"
              onChange={(e) => setExpression(e.target.value)}
            />
            <p className="text-[11px] text-muted-foreground">
              minute hour day month weekday — `0 9 * * MON` is 9am every Monday.
            </p>
          </div>
        )}
        {type === 'interval' && (
          <div className="space-y-1.5">
            <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Every (seconds)
            </label>
            <Input
              value={intervalS}
              inputMode="numeric"
              placeholder="3600"
              onChange={(e) => setIntervalS(e.target.value)}
            />
          </div>
        )}
        {type !== 'manual' && (
          <div className="grid grid-cols-3 gap-2">
            <div className="space-y-1.5">
              <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                Not before
              </label>
              <Input value={start} placeholder="08:00" onChange={(e) => setStart(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                Not after
              </label>
              <Input value={end} placeholder="18:00" onChange={(e) => setEnd(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                Timezone
              </label>
              <Input
                value={timezone}
                placeholder="America/Chicago"
                onChange={(e) => setTimezone(e.target.value)}
              />
            </div>
          </div>
        )}
        <Button size="sm" onClick={saveTrigger} disabled={patchWorkflow.isPending}>
          Save trigger
        </Button>
      </div>
      <div className="space-y-1.5">
        <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          Bound channel
        </label>
        <Input value={channel} onChange={(e) => setChannel(e.target.value)} placeholder="workflow-onboarding" />
        <p className="text-[11px] text-muted-foreground">
          Where runs narrate: node starts, gate decisions, and the outcome. Leave empty for none.
        </p>
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

  const waitingGates = (run.data?.nodes ?? []).filter(
    (n) => n.status === 'waiting_gate' && n.task_id,
  )

  return (
    <>
      {waitingGates.map((gate) => (
        <GateCard
          key={gate.task_id}
          taskId={gate.task_id!}
          nodeId={gate.node_id}
          body={`This run is waiting on ${gate.node_id}.`}
        />
      ))}
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

/** The workflow name in the header, editable in place for an operator.
 *
 * The name is the definition's display description; renaming is a versioned
 * definition edit through the same PATCH path as every other change, so it
 * bumps the version and drops the signature exactly as editing the graph does.
 * Falls back to the id whenever the definition carries no name.
 */
function WorkflowTitle({
  id,
  workflow,
  onBack,
}: {
  id: string
  workflow: WorkflowDetail | undefined
  onBack: () => void
}) {
  const [operatorMode] = useOperatorMode()
  const patch = usePatchWorkflow(id)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const displayName = workflow?.name || id

  const start = () => {
    setDraft(displayName)
    setError(null)
    setEditing(true)
  }

  const cancel = () => {
    setEditing(false)
    setError(null)
  }

  const save = async () => {
    if (!workflow) return
    const name = draft.trim()
    if (!name || name === displayName) {
      cancel()
      return
    }
    try {
      await patch.mutateAsync({ patch: { name }, expectedVersion: workflow.version })
      setEditing(false)
      setError(null)
    } catch (e) {
      setError(describeError(e).message)
    }
  }

  return (
    <span className="flex items-center gap-2">
      <Button variant="ghost" size="icon" onClick={onBack}>
        <ChevronLeft className="size-4" />
      </Button>
      {editing ? (
        <span className="flex items-center gap-1.5">
          <Input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                void save()
              } else if (e.key === 'Escape') {
                e.preventDefault()
                cancel()
              }
            }}
            className="h-8 w-56 text-sm"
          />
          <Button size="icon" variant="ghost" disabled={patch.isPending} onClick={save} title="Save name">
            <Check className="size-4" />
          </Button>
          <Button size="icon" variant="ghost" onClick={cancel} title="Cancel rename">
            <X className="size-4" />
          </Button>
          {error && <span className="text-xs text-destructive">{error}</span>}
        </span>
      ) : (
        <span className="flex items-center gap-1.5">
          {displayName}
          {operatorMode && workflow && (
            <Button size="icon" variant="ghost" onClick={start} title="Rename workflow">
              <Pencil className="size-3.5" />
            </Button>
          )}
        </span>
      )}
    </span>
  )
}

export function WorkflowDetailPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const workflow = useWorkflow(id)
  const archiveWorkflow = useArchiveWorkflow(id)
  const unarchiveWorkflow = useUnarchiveWorkflow(id)
  const runWorkflow = useRunWorkflow(id)
  const requestSignature = useRequestSignature(id)
  const [tab, setTab] = useState('graph')
  const [actionError, setActionError] = useState<string | null>(null)

  const requestSign = async () => {
    setActionError(null)
    try {
      await requestSignature.mutateAsync()
      setActionError(null)
    } catch (e) {
      setActionError(describeError(e).message)
    }
  }

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
          <WorkflowTitle id={id} workflow={workflow.data} onBack={() => navigate('/workflows')} />
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
            {workflow.data?.status === 'draft' && (
              <Button
                size="sm"
                variant="outline"
                disabled={requestSignature.isPending}
                onClick={requestSign}
                title="Puts this exact draft in the operator approvals queue"
              >
                <ShieldCheck className="size-3.5" />
                {requestSignature.isPending ? 'Requesting…' : 'Request signing'}
              </Button>
            )}
            <Button
              size="sm"
              onClick={runNow}
              disabled={runWorkflow.isPending}
              // An unsigned draft runs at personal tier and is refused above it.
              // The deployment decides that, not this button — a hard disable
              // here made a perfectly runnable draft look broken.
              title={
                workflow.data?.status === 'signed'
                  ? undefined
                  : 'Unsigned: runs at personal tier, refused at enterprise and federal'
              }
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
