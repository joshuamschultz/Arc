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
import { useAgentCapabilities, useRoster } from '@/lib/queries'
import {
  KIND_HELP,
  NODE_KINDS,
  STRATEGIES,
  splitList,
  type NodeDraft,
} from '@/lib/workflow-node-draft'
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

/** Free text with suggestions — the operator may always type something the
 * deployment does not know about yet, and the control plane still decides. */
function Suggested({
  value,
  onChange,
  options,
  placeholder,
  listId,
}: {
  value: string
  onChange: (v: string) => void
  options: string[]
  placeholder?: string
  listId: string
}) {
  return (
    <>
      <Input
        value={value}
        placeholder={placeholder}
        list={listId}
        onChange={(e) => onChange(e.target.value)}
      />
      <datalist id={listId}>
        {options.map((option) => (
          <option key={option} value={option} />
        ))}
      </datalist>
    </>
  )
}

function ChipMultiSelect({
  selected,
  options,
  onToggle,
}: {
  selected: string[]
  options: readonly string[]
  onToggle: (value: string) => void
}) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map((option) => (
        <button
          key={option}
          type="button"
          onClick={() => onToggle(option)}
          className={`rounded-md border px-2 py-0.5 font-mono text-[11px] ${
            selected.includes(option)
              ? 'border-status-online/40 bg-status-online/10 text-status-online'
              : 'border-border bg-muted/30 text-muted-foreground'
          }`}
        >
          {option}
        </button>
      ))}
    </div>
  )
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

  // Suggestions come from the LIVE deployment, never a hardcoded list: the
  // handles the runner can resolve, and the skills/tools the chosen agent
  // actually has loaded. A node naming something absent is a real error the
  // control plane reports — this only stops the operator from guessing.
  const roster = useRoster()
  const handles = useMemo(
    () =>
      (roster.data?.agents ?? [])
        .filter((a) => !a.hidden)
        .map((a) => `@${a.agent_id ?? a.name ?? ''}`)
        .filter((h) => h !== '@'),
    [roster.data],
  )
  const agentId = draft.agent.replace(/^@/, '')
  const capabilities = useAgentCapabilities(agentId || (roster.data?.agents ?? [])[0]?.agent_id || '')
  const skills = useMemo(
    () =>
      (capabilities.data?.items ?? [])
        .filter((item) => item.kind === 'skill')
        .map((item) => item.name),
    [capabilities.data],
  )
  const tools = useMemo(
    () =>
      (capabilities.data?.items ?? [])
        .filter((item) => item.kind === 'tool')
        .map((item) => item.name),
    [capabilities.data],
  )
  const strategySelected = splitList(draft.strategy)
  const toggleStrategy = (value: string) =>
    set(
      'strategy',
      (strategySelected.includes(value)
        ? strategySelected.filter((s) => s !== value)
        : [...strategySelected, value]
      ).join(', '),
    )

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
          <Suggested
            listId={`agents-${draft.id}`}
            value={draft.agent}
            options={handles}
            placeholder={handles[0] ?? '@sales'}
            onChange={(v) => set('agent', v)}
          />
        </Field>
      )}

      {draft.kind === 'agent' && (
        <>
          <Field label="Prompt file" hint="Path inside the bundle, e.g. prompts/collect.md">
            <Input value={draft.prompt} onChange={(e) => set('prompt', e.target.value)} />
          </Field>
          <Field
            label="Skill"
            hint={
              skills.length > 0
                ? 'Activated for this node. Suggestions are what this agent has loaded.'
                : 'Activated for this node (optional).'
            }
          >
            <Suggested
              listId={`skills-${draft.id}`}
              value={draft.skill}
              options={skills}
              onChange={(v) => set('skill', v)}
            />
          </Field>
          <Field
            label="Strategy"
            hint="Pin one, or offer a shortlist and let the loop choose. None = react."
          >
            <ChipMultiSelect
              selected={strategySelected}
              options={STRATEGIES}
              onToggle={toggleStrategy}
            />
          </Field>
        </>
      )}

      {draft.kind === 'tool' && (
        <>
          <Field
            label="Tool"
            hint={
              tools.length > 0
                ? 'The single tool this node calls. Suggestions are what the named agent has.'
                : 'The single tool this node calls, e.g. web_search.'
            }
          >
            <Suggested
              listId={`tools-${draft.id}`}
              value={draft.tool}
              options={tools}
              onChange={(v) => set('tool', v)}
            />
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
                  <Suggested
                    listId={`routes-${draft.id}`}
                    value={route.to}
                    options={upstream}
                    placeholder="target node id"
                    onChange={(v) => {
                      const routes = [...draft.routes]
                      routes[i] = { ...route, to: v }
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
