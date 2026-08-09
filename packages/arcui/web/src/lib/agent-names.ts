import type { Agent } from './types'

/**
 * The name a grant is written against: the agent's DIRECTORY name.
 *
 * Not `agent_id`, which is the roster's display key and need not match — a grant
 * carrying the wrong one is written, listed, and effective for nobody, which is
 * the silent no-op the whole grant surface exists to make visible. The server
 * refuses a name it has no agent for, so a wrong answer here fails loudly rather
 * than producing a connection an operator believes they handed over.
 */
export function grantName(agent: Agent): string {
  const segments = String(agent.workspace_path ?? '')
    .split('/')
    .filter(Boolean)
  return segments[segments.length - 1] ?? String(agent.agent_id ?? '')
}

/** What a person calls this agent. Never sent to the server. */
export function agentLabel(agent: Agent): string {
  return String(agent.display_name || agent.name || agent.agent_id || grantName(agent))
}
