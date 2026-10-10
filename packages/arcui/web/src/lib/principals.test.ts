// SPEC-087 T-1288 (COMP-013): the web client for app principals and a person's
// delegations. Shapes are fixed by the route tests:
//   packages/arcui/tests/test_principal_routes.py  (COMP-010)
//   packages/arcui/tests/test_delegation_routes.py (COMP-011)
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  DECISION_SCOPES,
  MAX_DELEGATION_HOURS,
  createDelegation,
  issuePrincipal,
  listDelegations,
  listPrincipals,
  maxExpiryDays,
  revokeDelegation,
  revokePrincipal,
} from '@/lib/principals'

afterEach(() => vi.unstubAllGlobals())

interface Call {
  method: string
  path: string
  body?: unknown
}

function stubFetch(reply: unknown = {}, status = 200): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        method: init?.method ?? 'GET',
        path: String(request),
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      })
      return new Response(JSON.stringify(reply), {
        status,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
  return calls
}

const ISSUE_BODY = {
  name: 'billing-app',
  public_key: 'ab'.repeat(32),
  scopes: { 'inbox:send': ['did:arc:default:agent/olivia'] },
  subjects: ['did:arc:default:user/ada'],
  expires_at: '2026-11-01T00:00:00.000Z',
}

describe('principals API client', () => {
  it('lists principals with GET /api/principals', async () => {
    const calls = stubFetch({ principals: [] })
    expect(await listPrincipals()).toEqual({ principals: [] })
    expect(calls).toEqual([{ method: 'GET', path: '/api/principals', body: undefined }])
  })

  it('issues with POST /api/principals and the exact issue body', async () => {
    const reply = { principal: { id: 'p1' }, secret: 'arcp_p1.s', status: 'active' }
    const calls = stubFetch(reply, 201)
    expect(await issuePrincipal(ISSUE_BODY)).toEqual(reply)
    expect(calls).toEqual([{ method: 'POST', path: '/api/principals', body: ISSUE_BODY }])
  })

  it('revokes with POST /api/principals/{id}/revoke and no body', async () => {
    const calls = stubFetch({ principal: { id: 'p1', status: 'revoked' } })
    await revokePrincipal('p1')
    expect(calls).toHaveLength(1)
    expect(calls[0].method).toBe('POST')
    expect(calls[0].path).toBe('/api/principals/p1/revoke')
    expect(calls[0].body).toBeUndefined()
  })

  it('URL-encodes the principal id so it cannot reach another route', async () => {
    const calls = stubFetch({})
    await revokePrincipal('../users/x')
    expect(calls[0].path).toBe('/api/principals/..%2Fusers%2Fx/revoke')
  })

  it('surfaces a refusal code instead of resolving', async () => {
    stubFetch({ error: 'expiry_too_long' }, 400)
    await expect(issuePrincipal(ISSUE_BODY)).rejects.toThrow('expiry_too_long')
  })
})

describe('delegations API client', () => {
  it('lists with GET /api/me/delegations', async () => {
    const calls = stubFetch({ delegations: [] })
    expect(await listDelegations()).toEqual({ delegations: [] })
    expect(calls).toEqual([{ method: 'GET', path: '/api/me/delegations', body: undefined }])
  })

  it('creates with POST /api/me/delegations {principal_did, scopes, ttl_seconds}', async () => {
    const body = { principal_did: 'did:arc:default:app/x', scopes: ['gates:resolve'], ttl_seconds: 3600 }
    const calls = stubFetch({ id: 'd1', ...body }, 201)
    await createDelegation(body)
    expect(calls).toEqual([{ method: 'POST', path: '/api/me/delegations', body }])
  })

  it('revokes with POST /api/me/delegations/{id}/revoke (encoded)', async () => {
    const calls = stubFetch({})
    await revokeDelegation('d/1')
    expect(calls).toHaveLength(1)
    expect(calls[0].method).toBe('POST')
    expect(calls[0].path).toBe('/api/me/delegations/d%2F1/revoke')
  })
})

describe('tier limits (REQ-574, REQ-563)', () => {
  it('caps principal expiry at 90 days for enterprise and 30 for federal', () => {
    expect(maxExpiryDays('enterprise')).toBe(90)
    expect(maxExpiryDays('federal')).toBe(30)
  })

  it('leaves expiry optional at personal', () => {
    expect(maxExpiryDays('personal')).toBeNull()
  })

  it('fails closed to the strictest cap for an unknown tier', () => {
    expect(maxExpiryDays('mystery')).toBe(30)
  })

  it('allows only the decision scopes for at most 8 hours in a delegation', () => {
    expect([...DECISION_SCOPES].sort()).toEqual(['gates:resolve', 'tasks:write'])
    expect(MAX_DELEGATION_HOURS).toBe(8)
  })
})
