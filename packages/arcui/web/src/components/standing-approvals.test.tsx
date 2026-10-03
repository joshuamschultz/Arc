// "Always allow" on the approval card + the Standing approvals list (SPEC-035
// OQ-3, ruled 2026-10-03). Drives the buttons an operator clicks and asserts
// the exact endpoint each one reaches.
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { ApprovalRequest } from '@/components/hitl'
import { StandingApprovals } from '@/components/standing-approvals'
import type { PendingApproval } from '@/lib/queries'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

const DID = 'did:arc:local:executor/c0bef560'

function json(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function stubFetch(routes: Record<string, unknown>) {
  const calls: Array<{ method: string; path: string }> = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      calls.push({ method: init?.method ?? 'GET', path })
      const hit = Object.keys(routes).find((key) => path.includes(key))
      return json(hit ? routes[hit] : {})
    }),
  )
  return calls
}

function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

function request(overrides: Partial<PendingApproval> = {}): PendingApproval {
  return {
    id: 'req1',
    agent_did: DID,
    agent_label: 'Olivia',
    tool: 'dropbox_upload',
    legs: ['external_comms', 'private_data', 'untrusted_input'],
    call_hash: 'h',
    status: 'pending',
    created_at: '2026-10-03T14:47:29Z',
    expires_at: '2026-10-03T14:52:29Z',
    destination: 'personal_dropbox',
    grant_tool: 'dropbox_upload',
    standing_eligible: true,
    ...overrides,
  }
}

it('Always allow posts to the always endpoint and names the scope', async () => {
  const calls = stubFetch({ '/api/team/roster': { agents: [] } })
  render(wrap(<ApprovalRequest operatorMode a={request()} />))

  expect(screen.getByText(/run dropbox_upload to personal_dropbox/)).toBeTruthy()
  await userEvent.click(screen.getByRole('button', { name: /Always allow/ }))

  await waitFor(() =>
    expect(calls).toContainEqual({ method: 'POST', path: '/api/approvals/req1/always' }),
  )
})

it('a federal request offers no Always allow', () => {
  stubFetch({ '/api/team/roster': { agents: [] } })
  render(wrap(<ApprovalRequest operatorMode a={request({ standing_eligible: false })} />))

  expect(screen.queryByRole('button', { name: /Always allow/ })).toBeNull()
  expect(screen.getByRole('button', { name: /Approve/ })).toBeTruthy()
})

it('lists standing approvals with scope, grantor and uses, and revokes one', async () => {
  localStorage.setItem('arcui_operator_mode', '1')
  const calls = stubFetch({
    '/api/agents/josh_agent': { did: DID },
    '/api/standing-grants?': {
      grants: [
        {
          id: 'sg-1',
          agent_did: DID,
          agent_label: 'Olivia',
          tool: 'dropbox_upload',
          composition: ['external_comms', 'private_data', 'untrusted_input'],
          destination: 'personal_dropbox',
          status: 'active',
          granted_by: 'did:arc:operator:approver/bf6ee9f7',
          granted_at: '2026-10-03T14:48:23Z',
          source_approval_id: 'req1',
          revoked_by: null,
          revoked_at: null,
          use_count: 6,
          last_used_at: null,
        },
      ],
    },
    '/revoke': { id: 'sg-1', status: 'revoked' },
  })
  render(wrap(<StandingApprovals agentId="josh_agent" />))

  expect(await screen.findByText('personal_dropbox')).toBeTruthy()
  expect(screen.getByText('External comms + Private data + Untrusted input')).toBeTruthy()
  expect(screen.getByText('bf6ee9f7')).toBeTruthy()
  expect(screen.getByText('6')).toBeTruthy()
  expect(
    calls.some((c) => c.path === `/api/standing-grants?agent_did=${encodeURIComponent(DID)}`),
  ).toBe(true)

  await userEvent.click(screen.getByRole('button', { name: 'Revoke' }))
  await waitFor(() =>
    expect(calls).toContainEqual({ method: 'POST', path: '/api/standing-grants/sg-1/revoke' }),
  )
})
