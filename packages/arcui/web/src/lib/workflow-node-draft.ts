import type { WorkflowNode, WorkflowNodeKind } from './types'

// The editable shape behind the node form, and the two conversions between it
// and the node object the control plane parses. arcui is not a second
// validator (REQ-253): every value is relayed verbatim and empty fields simply
// drop out.

export const NODE_KINDS: WorkflowNodeKind[] = ['agent', 'tool', 'script', 'router', 'gate']

/** arcrun's registered strategies (arcrun.strategies.STRATEGIES). A node may
 * pin one or hand the loop a shortlist to choose from. */
export const STRATEGIES = ['react', 'code', 'plan_execute'] as const

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

export const splitList = (value: string): string[] =>
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
