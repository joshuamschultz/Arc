import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { BundleCard, ConnectionsPage } from '@/pages/connections'
import type { CatalogBundle } from '@/lib/types'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

const bundle = (remote: boolean): CatalogBundle => ({
  name: 'google_workspace', display_name: 'Google Workspace', version: '1.0.0',
  description: 'Gmail, Calendar and Drive', attachment: 'cli', tier_floor: 'personal',
  approval_default: 'ask', knowledge_mode: 'source', knowledge_reason: '',
  secrets: [{ name: 'account', prompt: 'Google account', sensitive: false, value: '' }],
  host_requires: [{ name: 'gog', instruction: 'install gog', satisfied: true, remote_login: remote }],
  tools: [], root: '/ext',
})

function wrap(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>,
  )
}

describe('BundleCard', () => {
  it('offers "Add an account" for a remote-login bundle', () => {
    wrap(<BundleCard bundle={bundle(true)} connectedCount={1} operatorMode onConnect={() => {}} />)
    expect(screen.getByRole('button', { name: /Add an account/ })).toBeTruthy()
    expect(
      screen.getByText('Each account is its own connection. After adding it, click Sign in on its card.'),
    ).toBeTruthy()
  })

  it('keeps "Connect" for every other bundle', () => {
    wrap(<BundleCard bundle={bundle(false)} connectedCount={0} operatorMode onConnect={() => {}} />)
    expect(screen.getByRole('button', { name: /^Connect$/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Add an account/ })).toBeNull()
  })
})

describe('ConnectionCard for a remote-login connection', () => {
  it('shows the account, a Reconnect status, and opens the sign-in panel', async () => {
    localStorage.setItem('arcui_operator_mode', '1')
    vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL) => {
      const path = String(request)
      const json = (body: unknown) => new Response(JSON.stringify(body))
      if (path.includes('/api/team/roster')) return json({ agents: [] })
      if (path.includes('/api/connectors/catalog')) return json({ available: [bundle(true)], unreadable: [] })
      if (path.endsWith('/auth-status')) return json({ sign_in: 'expired', reachable: true, detail: 'token revoked' })
      if (path.endsWith('/auth')) {
        return json({
          instance: 'gmail-olivia', extension: 'google_workspace', extension_display_name: 'Google Workspace',
          credentials: [{ name: 'account', prompt: 'Google account', sensitive: false, value: 'olivia@example.com' }],
          reachable: true, detail: '', sign_in: 'expired', hosts: [{ name: 'gog', remote_login: true }],
        })
      }
      if (path.endsWith('/api/connections')) {
        return json({
          connections: [{
            instance: 'gmail-olivia', extension: 'google_workspace', extension_display_name: 'Google Workspace',
            knowledge_mode: 'source', knowledge_reason: '', approval: 'ask', agents: [],
          }],
          extensions_roots: [],
        })
      }
      return new Response(JSON.stringify({ error: 'nope' }), { status: 404 })
    }))
    wrap(<ConnectionsPage />)
    const chip = await screen.findByText('Reconnect needed', { selector: '[data-sign-in-chip]' })
    const card = chip.closest('[data-connection-card]') as HTMLElement
    expect(within(card).getAllByText('olivia@example.com').length).toBeGreaterThan(0)
    expect(within(card).getByRole('button', { name: /Edit details/ })).toBeTruthy()
    expect(within(card).getByRole('button', { name: /Hide sign-in/ })).toBeTruthy()
    expect(await within(card).findByText(/Google stopped accepting the saved sign-in/)).toBeTruthy()
  })
})
