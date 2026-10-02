import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { GatedCapabilitiesPage } from './gated-capabilities'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

const row = {
  agent_id: 'olivia',
  agent_label: 'olivia',
  name: 'briefing',
  kind: 'skill',
  status: 'invalid',
  path: '/w/briefing/SKILL.md',
  hash: 'h1',
  detail: 'missing_section: missing required sections: [Validation]',
  signer_did: 'did:arc:agent:abc',
}

function mount() {
  localStorage.setItem('arcui_operator_mode', '1')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <GatedCapabilitiesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: RequestInfo | URL) => {
      const url = String(path)
      if (url.startsWith('/api/trust/gated')) return new Response(JSON.stringify({ gated: [row] }))
      if (url.includes('/source')) return new Response(JSON.stringify({ source: 'x', hash: 'h1' }))
      return new Response(JSON.stringify(row))
    }),
  )
}

it('labels an invalid row as failing validation, not as a bad signature', async () => {
  stubFetch()
  mount()
  expect(await screen.findByText('fails validation')).toBeTruthy()
  expect(screen.queryByText('invalid signature')).toBeNull()
  expect(screen.getByText(/missing required sections/)).toBeTruthy()
})

it('re-enables the buttons and shows why it is still held after a re-sign', async () => {
  stubFetch()
  mount()
  await userEvent.click(await screen.findByRole('button', { name: /Review source/ }))
  const resign = await screen.findByRole('button', { name: /Re-sign/ })
  await waitFor(() => expect((resign as HTMLButtonElement).disabled).toBe(false))
  await userEvent.click(resign)
  expect(await screen.findByText(/Signed, but still held back/)).toBeTruthy()
  expect((resign as HTMLButtonElement).disabled).toBe(false)
  expect(
    (screen.getByRole('button', { name: /Disapprove/ }) as HTMLButtonElement).disabled,
  ).toBe(false)
})
