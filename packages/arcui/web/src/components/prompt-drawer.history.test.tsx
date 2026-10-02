// J2 F3 — the prompt drawer's History tab: signed versions, a server diff, and Revert.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { PromptDrawer } from '@/components/prompt-drawer'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

const BASE = '/api/agents/olivia/prompts/arcagent/base_system'
const DETAIL = {
  package: 'arcagent',
  name: 'base_system',
  description: 'Base system prompt',
  status: 'overridden',
  rejection_reason: null,
  stock: 'stock body',
  effective: 'effective body',
  diff: '',
}
const HISTORY = {
  package: 'arcagent',
  name: 'base_system',
  versions: [
    { version: 2, sha256: 'sha256:bb', signer_did: 'did:arc:op', signed_at: '2026-10-01', current: true },
    { version: 1, sha256: 'sha256:aa', signer_did: 'did:arc:op', signed_at: '2026-09-30', current: false },
  ],
}

function stubServer() {
  const calls: { path: string; method: string }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const method = init?.method ?? 'GET'
      calls.push({ path, method })
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
      if (path.startsWith(`${BASE}/history/diff`)) {
        return json({
          package: 'arcagent',
          name: 'base_system',
          from_label: 'v1',
          to_label: 'v2',
          diff: '-TUESDAY\n+WEDNESDAY\n',
        })
      }
      if (path === `${BASE}/history/1/revert` && method === 'POST') {
        return json({
          package: 'arcagent',
          name: 'base_system',
          reverted_from: 1,
          new_version: 3,
          signer_did: 'did:arc:op',
          sha256: 'sha256:aa',
          message: 'Reverted to version 1 as new version 3.',
        })
      }
      if (path === `${BASE}/history`) return json(HISTORY)
      if (path === BASE) return json(DETAIL)
      return new Response('{}', { status: 404 })
    }),
  )
  return calls
}

function renderDrawer() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <PromptDrawer
        agentId="olivia"
        prompt={{ package: 'arcagent', name: 'base_system' }}
        open
        onOpenChange={() => {}}
      />
    </QueryClientProvider>,
  )
}

describe('PromptDrawer history tab', () => {
  it('lists signed versions, marks the live one, and shows the diff', async () => {
    stubServer()
    renderDrawer()
    await userEvent.click(await screen.findByRole('button', { name: 'history' }))
    expect((await screen.findAllByText(/^v2/)).length).toBeGreaterThan(0)
    expect(screen.getByText('current')).toBeTruthy()
    expect(await screen.findByText(/WEDNESDAY/)).toBeTruthy()
  })

  it('hides Revert from a viewer', async () => {
    stubServer()
    renderDrawer()
    await userEvent.click(await screen.findByRole('button', { name: 'history' }))
    await screen.findAllByText(/^v2/)
    expect(screen.queryByRole('button', { name: /revert to v1/i })).toBeNull()
  })

  it('operator Revert posts to the version and reports the new version', async () => {
    localStorage.setItem('arcui_operator_mode', '1')
    const calls = stubServer()
    renderDrawer()
    await userEvent.click(await screen.findByRole('button', { name: 'history' }))
    await userEvent.click(await screen.findByRole('button', { name: /revert to v1/i }))
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.path === `${BASE}/history/1/revert`)).toBe(true),
    )
    expect(await screen.findByText(/new version 3/i)).toBeTruthy()
    // The live version has no Revert button.
    expect(screen.queryByRole('button', { name: /revert to v2/i })).toBeNull()
  })
})
