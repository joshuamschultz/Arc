import { useMemo } from 'react'
import { Plus, Trash2 } from 'lucide-react'
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
import type { WorkflowNode, WorkflowNodeKind } from '@/lib/types'

/**
 * Form editor for one node.
 *
 * The raw-JSON editor this replaces was schema-agnostic on purpose — arcui is
 * not a second validator and still isn't: every value here is relayed verbatim
 * and the control plane decides whether the node is admissible (REQ-253). What
 * changed is discoverability. An operator could not tell from a JSON blob that
 * a tool node needs a tool name, that a router needs routes, or that an edge is
 * spelled `needs` — so the fields a kind admits are LISTED here, and the raw
 * JSON stays available underneath for anything this form does not cover.
 */

export const NODE_KINDS: WorkflowNodeKind[] = ['agent', 'tool', 'script', 'router', 'gate']

export const KIND_HELP: Record<WorkflowNodeKind, string> = {
  agent: 'A bounded agent run. Give it a prompt file, optionally a skill.',
  tool: 'Exactly one tool call with wired arguments. No model in the loop.',
  script: 'Sandboxed deterministic code from the bundle.',
  router: 'Picks one downstream branch — by rules (predicates) or by the model.',
  gate: 'Stops for a human. Approve, fail the run, or send it back with notes.',
}

export interface RouteDraft {
  to: string
  when: string
  default: boolean
}

/** The editable shape behind the form — flat strings so inputs stay controlled. */
export interface NodeDraft {
  id: string
  kind: WorkflowNodeKind
  agent: string
  needs: string[]
  join: 'all' | 'any'
  when: string
  // agent
  prompt: string
  skill: string
  strategy: string
  // tool
  tool: string
  args: string
  // script
  script: string
  // gate
  gate: string
  // router
  mode: 'rules' | 'llm'
  routes: RouteDraft[]
  // shared extras
  outputSchema: string
  artifacts: string
  loopBackTo: string
  maxIterations: string
  timeout: string
  maxAttempts: string
}

const asText = (value: unknown): string =>
  value === null || value === undefined ? '' : String(value)

const asList = (value: unknown): string =>
  Array.isArray(value) ? value.map(String).join(', ') : asText(value)

const splitList = (value: string): string[] =>
  value
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)

export function toDraft(node: WorkflowNode): NodeDraft {
  const raw = node as Record<string, unknown>
  const routes = Array.isArray(raw.routes) ? (raw.routes as Record<string, unknown>[]) : []
  return {
    id: node.id,
    kind: node.kind,
    agent: asText(raw.agent),
    needs: Array.isArray(node.needs) ? node.needs.map(String) : [],
    join: node.join === 'any' ? 'any' : 'all',
    when: asText(node.when),
    prompt: asText(raw.prompt),
    skill: asText(raw.skill),
    strategy: asList(raw.strategy),
    tool: asText(raw.tool),
    args: raw.args && Object.keys(raw.args as object).length > 0 ? JSON.stringify(raw.args, null, 2) : '',
    script: asText(raw.script),
    gate: asText(raw.gate),
    mode: raw.mode === 'llm' ? 'llm' : 'rules',
    routes: routes.map((r) => ({
      to: asText(r.to),
      when: asText(r.when),
      default: Boolean(r.default),
    })),
    outputSchema: asText(raw.output_schema),
    artifacts: asList(raw.artifacts),
    loopBackTo: asText(node.loop_back_to),
    maxIterations: asText(node.max_iterations),
    timeout: asText(raw.timeout_s),
    maxAttempts: asText(raw.max_attempts),
  }
}

/** The draft as the node object the control plane parses. Empty fields drop out. */
export function fromDraft(draft: NodeDraft): Record<string, unknown> {
  const node: Record<string, unknown> = { id: draft.id.trim(), kind: draft.kind }
  const put = (key: string, value: string) => {
    if (value.trim()) node[key] = value.trim()
  }
  put('agent', draft.agent)
  if (draft.needs.length > 0) {
    node.needs = draft.needs
    if (draft.join === 'any') node.join = 'any'
  }
  put('when', draft.when)
  put('output_schema', draft.outputSchema)
  if (splitList(draft.artifacts).length > 0) node.artifacts = splitList(draft.artifacts)
  put('loop_back_to', draft.loopBackTo)
  if (draft.maxIterations.trim()) node.max_iterations = Number(draft.maxIterations)
  if (draft.timeout.trim()) node.timeout_s = Number(draft.timeout)
  if (draft.maxAttempts.trim()) node.max_attempts = Number(draft.maxAttempts)

  if (draft.kind === 'agent') {
    put('prompt', draft.prompt)
    put('skill', draft.skill)
    if (splitList(draft.strategy).length > 0) node.strategy = splitList(draft.strategy)
  }
  if (draft.kind === 'tool') {
    put('tool', draft.tool)
    if (draft.args.trim()) node.args = JSON.parse(draft.args) as Record<string, unknown>
  }
  if (draft.kind === 'script') put('script', draft.script)
  if (draft.kind === 'gate') put('gate', draft.gate)
  if (draft.kind === 'router') {
    node.mode = draft.mode
    node.routes = draft.routes
      .filter((r) => r.to.trim())
      .map((r) => {
        const route: Record<string, unknown> = { to: r.to.trim() }
        if (r.when.trim()) route.when = r.when.trim()
        if (r.default) route.default = true
        return route
      })
  }
  return node
}

function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <label className="block space-y-1">
      <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      {children}
      {hint && <span className="block text-[11px] text-muted-foreground">{hint}</span>}
    </label>
  )
}

export function WorkflowNodeForm({
  draft,
  siblings,
  onChange,
}: {
  draft: NodeDraft
  siblings: WorkflowNode[]
  onChange: (next: NodeDraft) => void
}) {
  const set = <K extends keyof NodeDraft>(key: K, value: NodeDraft[K]) =>
    onChange({ ...draft, [key]: value })

  const upstream = useMemo(
    () => siblings.filter((n) => n.id !== draft.id).map((n) => n.id),
    [siblings, draft.id],
  )

  const toggleNeed = (id: string) =>
    set('needs', draft.needs.includes(id) ? draft.needs.filter((n) => n !== id) : [...draft.needs, id])

  return (
    <div className="space-y-4">
      <Field label="Node id">
        <Input value={draft.id} onChange={(e) => set('id', e.target.value)} className="font-mono" />
      </Field>

      <Field label="Kind" hint={KIND_HELP[draft.kind]}>
        <Select value={draft.kind} onValueChange={(v) => set('kind', v as WorkflowNodeKind)}>
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {NODE_KINDS.map((kind) => (
              <SelectItem key={kind} value={kind}>
                {kind}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>

      {draft.kind !== 'gate' && (
        <Field label="Agent" hint="@handle that runs this node. Defaults to the workflow owner.">
          <Input
            value={draft.agent}
            placeholder="@sales"
            onChange={(e) => set('agent', e.target.value)}
          />
        </Field>
      )}

      {draft.kind === 'agent' && (
        <>
          <Field label="Prompt file" hint="Path inside the bundle, e.g. prompts/collect.md">
            <Input value={draft.prompt} onChange={(e) => set('prompt', e.target.value)} />
          </Field>
          <Field label="Skill" hint="Skill activated for this node (optional).">
            <Input value={draft.skill} onChange={(e) => set('skill', e.target.value)} />
          </Field>
          <Field label="Strategy" hint="Comma separated, e.g. react, reflect.">
            <Input value={draft.strategy} onChange={(e) => set('strategy', e.target.value)} />
          </Field>
        </>
      )}

      {draft.kind === 'tool' && (
        <>
          <Field label="Tool" hint="The single tool this node calls, e.g. web_search.">
            <Input value={draft.tool} onChange={(e) => set('tool', e.target.value)} />
          </Field>
          <Field
            label="Arguments (JSON)"
            hint='Wire upstream values with "$nodes.<id>.output.<field>".'
          >
            <Textarea
              rows={4}
              value={draft.args}
              placeholder={'{\n  "query": "$input.topic"\n}'}
              onChange={(e) => set('args', e.target.value)}
              className="font-mono text-xs"
            />
          </Field>
        </>
      )}

      {draft.kind === 'script' && (
        <Field label="Script" hint="Path inside the bundle, e.g. scripts/publish.py">
          <Input value={draft.script} onChange={(e) => set('script', e.target.value)} />
        </Field>
      )}

      {draft.kind === 'gate' && (
        <Field label="Gate" hint="Label for the human decision, e.g. human:approve_publish.">
          <Input value={draft.gate} onChange={(e) => set('gate', e.target.value)} />
        </Field>
      )}

      {draft.kind === 'router' && (
        <>
          <Field
            label="Mode"
            hint={
              draft.mode === 'rules'
                ? 'Rules: the first matching condition wins.'
                : 'LLM: the node picks one of the declared route ids.'
            }
          >
            <Select value={draft.mode} onValueChange={(v) => set('mode', v as 'rules' | 'llm')}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="rules">rules</SelectItem>
                <SelectItem value="llm">llm</SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <div className="space-y-2">
            <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              Routes
            </span>
            {draft.routes.map((route, i) => (
              <div key={i} className="space-y-1 rounded-lg border border-border p-2">
                <div className="flex items-center gap-2">
                  <Input
                    value={route.to}
                    placeholder="target node id"
                    className="font-mono text-xs"
                    onChange={(e) => {
                      const routes = [...draft.routes]
                      routes[i] = { ...route, to: e.target.value }
                      set('routes', routes)
                    }}
                  />
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-destructive hover:text-destructive"
                    onClick={() => set('routes', draft.routes.filter((_, j) => j !== i))}
                  >
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
                <Input
                  value={route.when}
                  placeholder="$nodes.verify.output.risk == 'low'"
                  className="font-mono text-xs"
                  disabled={route.default}
                  onChange={(e) => {
                    const routes = [...draft.routes]
                    routes[i] = { ...route, when: e.target.value }
                    set('routes', routes)
                  }}
                />
                <label className="flex items-center gap-2 text-[11px] text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={route.default}
                    onChange={(e) => {
                      const routes = draft.routes.map((r, j) => ({
                        ...r,
                        default: j === i ? e.target.checked : false,
                      }))
                      set('routes', routes)
                    }}
                  />
                  fallback when nothing matches
                </label>
              </div>
            ))}
            <Button
              size="sm"
              variant="ghost"
              onClick={() => set('routes', [...draft.routes, { to: '', when: '', default: false }])}
            >
              <Plus className="size-3.5" /> Add route
            </Button>
          </div>
        </>
      )}

      <div className="space-y-2">
        <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          Runs after (edges)
        </span>
        {upstream.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">No other nodes yet.</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {upstream.map((id) => (
              <button
                key={id}
                type="button"
                onClick={() => toggleNeed(id)}
                className={`rounded-md border px-2 py-0.5 font-mono text-[11px] ${
                  draft.needs.includes(id)
                    ? 'border-status-online/40 bg-status-online/10 text-status-online'
                    : 'border-border bg-muted/30 text-muted-foreground'
                }`}
              >
                {id}
              </button>
            ))}
          </div>
        )}
        {draft.needs.length > 1 && (
          <Field label="Join" hint="all: wait for every upstream. any: first one through wins.">
            <Select value={draft.join} onValueChange={(v) => set('join', v as 'all' | 'any')}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">all</SelectItem>
                <SelectItem value="any">any</SelectItem>
              </SelectContent>
            </Select>
          </Field>
        )}
      </div>

      <Field label="Condition" hint="Skip this node unless the expression is true (optional).">
        <Input
          value={draft.when}
          placeholder="$nodes.qa.output.verdict == 'revise'"
          className="font-mono text-xs"
          onChange={(e) => set('when', e.target.value)}
        />
      </Field>

      <details className="rounded-lg border border-border p-2">
        <summary className="cursor-pointer text-[11px] uppercase tracking-wide text-muted-foreground">
          More
        </summary>
        <div className="mt-2 space-y-3">
          <Field label="Output schema" hint="Bundle path to a JSON Schema the output must match.">
            <Input value={draft.outputSchema} onChange={(e) => set('outputSchema', e.target.value)} />
          </Field>
          <Field label="Artifacts" hint="Comma separated files that must exist when the node finishes.">
            <Input value={draft.artifacts} onChange={(e) => set('artifacts', e.target.value)} />
          </Field>
          <Field label="Loop back to" hint="Declared back-edge target. Needs a max iterations bound.">
            <Input value={draft.loopBackTo} onChange={(e) => set('loopBackTo', e.target.value)} />
          </Field>
          <Field label="Max iterations">
            <Input
              value={draft.maxIterations}
              inputMode="numeric"
              onChange={(e) => set('maxIterations', e.target.value)}
            />
          </Field>
          <Field label="Timeout (seconds)">
            <Input
              value={draft.timeout}
              inputMode="numeric"
              onChange={(e) => set('timeout', e.target.value)}
            />
          </Field>
          <Field label="Max attempts">
            <Input
              value={draft.maxAttempts}
              inputMode="numeric"
              onChange={(e) => set('maxAttempts', e.target.value)}
            />
          </Field>
        </div>
      </details>
    </div>
  )
}
