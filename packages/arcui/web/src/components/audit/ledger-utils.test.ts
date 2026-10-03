import { describe, expect, it } from 'vitest'
import {
  auditLinks,
  auditQuery,
  causalChain,
  initiatorOf,
  signedByLabel,
  verificationOf,
} from './ledger-utils'
import type { AuditEvent } from '@/lib/types'

const agentRow: AuditEvent = {
  actor_did: 'did:arc:t:olivia/1',
  action: 'policy.evaluate',
  initiator: 'agent',
  initiator_id: 'did:arc:t:olivia/1',
  run_id: 'run-1',
  tool_call_id: 'tc-1',
  llm_call_id: 'llm-1',
  workflow_run_id: 'wf-1',
  node_id: 'n1',
  connection_id: 'conn-1',
  chain: 'audit-chain-olivia',
  signer: 'abc123',
  verified: true,
  causal: { initiator: 'agent', initiator_id: 'did:arc:t:olivia/1', run_id: 'run-1' },
}

describe('initiatorOf', () => {
  it('names the initiator kind and id', () => {
    expect(initiatorOf(agentRow)).toEqual({ kind: 'Agent', id: 'did:arc:t:olivia/1' })
  })

  it('reads the nested causal chain when the flat columns are absent', () => {
    const e: AuditEvent = { causal: { initiator: 'ui_session', initiator_id: 'did:arc:ui:session:x' } }
    expect(initiatorOf(e)).toEqual({ kind: 'UI session', id: 'did:arc:ui:session:x' })
  })

  it('is undefined for a row that predates causality', () => {
    expect(initiatorOf({ actor_did: 'did:arc:old' })).toBeUndefined()
  })
})

describe('signedByLabel', () => {
  it('says the operator signed a row on the arcui chain', () => {
    expect(signedByLabel({ chain: 'audit-chain-arcui', signer: 'k' })).toBe('signed by operator')
  })

  it('says the agent signed its own chain', () => {
    expect(signedByLabel(agentRow)).toBe('signed by agent')
  })

  it('is undefined for an unsigned row', () => {
    expect(signedByLabel({ chain: 'audit-chain-arcui' })).toBeUndefined()
  })
})

describe('auditLinks', () => {
  it('links a row to its run, LLM call, tool call, workflow run and connection', () => {
    const links = auditLinks(agentRow)
    expect(links.map((l) => l.kind)).toEqual(['run', 'llm_call', 'tool_call', 'workflow', 'connection'])
    expect(links.find((l) => l.kind === 'run')?.to).toBe('/arcrun?run=run-1')
    expect(links.find((l) => l.kind === 'tool_call')?.to).toBe('/arcrun?run=run-1')
    expect(links.find((l) => l.kind === 'llm_call')?.traceId).toBe('llm-1')
    expect(links.find((l) => l.kind === 'workflow')?.to).toBe('/workflows?run=wf-1')
    expect(links.find((l) => l.kind === 'connection')?.to).toBe('/connections?connection=conn-1')
  })

  it('gives nothing for a row with no causal ids', () => {
    expect(auditLinks({ actor_did: 'x' })).toEqual([])
  })
})

describe('verificationOf', () => {
  it('reports a verified row', () => {
    expect(verificationOf({ verified: 1, signature: 's' })).toEqual({ state: 'verified' })
  })

  it('reports the seq where a chain broke', () => {
    expect(verificationOf({ action: 'audit.chain.broken', seq: 7 })).toEqual({
      state: 'broken',
      seq: 7,
    })
  })

  it('reports an unverified signed row', () => {
    expect(verificationOf({ verified: 0, signature: 's' })).toEqual({ state: 'unverified' })
  })

  it('reports an unsigned row', () => {
    expect(verificationOf({ verified: 0 })).toEqual({ state: 'unsigned' })
  })
})

describe('causalChain', () => {
  it('lists the set causal ids in a stable order', () => {
    expect(causalChain(agentRow).map((c) => c.label)).toEqual([
      'Initiator',
      'Run',
      'LLM call',
      'Tool call',
      'Workflow run',
      'Workflow node',
      'Connection',
    ])
  })
})

describe('auditQuery', () => {
  it('builds a query string from the set filters only', () => {
    expect(auditQuery({ filter: 'deny', run_id: 'run-1', initiator: '' }, 50)).toBe(
      'limit=50&filter=deny&run_id=run-1',
    )
  })

  it('url-encodes values', () => {
    expect(auditQuery({ agent: 'did:arc:t:a/1' }, 10)).toBe('limit=10&agent=did%3Aarc%3At%3Aa%2F1')
  })
})
