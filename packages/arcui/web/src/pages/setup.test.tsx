import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { SetupPage } from '@/pages/setup'
import { clearHostedClaim, hostedClaimSecret } from '@/lib/hosted-claim'
import { setToken } from '@/lib/auth'

vi.mock('@/lib/hosted-claim', () => ({
  hostedClaimSecret: vi.fn(() => 'customer-secret'),
  clearHostedClaim: vi.fn(),
}))
vi.mock('@/lib/auth', () => ({ setToken: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

function showSetup() {
  return render(<MemoryRouter initialEntries={['/setup']}>
    <Routes>
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/home" element={<p>Signed in</p>} />
    </Routes>
  </MemoryRouter>)
}

async function fillPasswords(password: string, confirmation: string) {
  await userEvent.type(document.getElementById('setup-password') as HTMLInputElement, password)
  await userEvent.type(document.getElementById('setup-repeat') as HTMLInputElement, confirmation)
}

it('shows server status and keeps a mismatched password off the claim endpoint', async () => {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify({ status: 'awaiting_setup' })))
  vi.stubGlobal('fetch', fetchMock)
  showSetup()
  expect(await screen.findByText(/Your server is ready for account setup/)).toBeTruthy()
  await fillPasswords('correct-horse-battery', 'correct-horse-other')
  await userEvent.click(screen.getByRole('button', { name: 'Create account' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent',
    'Use at least 12 characters and enter the same password twice.')
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it('claims with the tab secret and signs in after account creation', async () => {
  const fetchMock = vi.fn(async (path: RequestInfo | URL, options?: RequestInit) => {
    if (String(path) === '/api/setup/status') {
      return new Response(JSON.stringify({ status: 'awaiting_setup' }))
    }
    if (String(path) === '/api/setup/claim') {
      expect(JSON.parse(String(options?.body))).toEqual({
        customer_secret: 'customer-secret', password: 'correct-horse-battery',
      })
      return new Response(JSON.stringify({ status: 'setup_complete', email: 'user@example.com' }))
    }
    return new Response(JSON.stringify({ token: 'session-token' }))
  })
  vi.stubGlobal('fetch', fetchMock)
  showSetup()
  await screen.findByText(/Your server is ready for account setup/)
  await fillPasswords('correct-horse-battery', 'correct-horse-battery')
  await userEvent.click(screen.getByRole('button', { name: 'Create account' }))
  expect(await screen.findByText('Signed in')).toBeTruthy()
  expect(clearHostedClaim).toHaveBeenCalledOnce()
  expect(setToken).toHaveBeenCalledWith('session-token')
  expect(fetchMock).toHaveBeenCalledTimes(3)
})

it('treats an uncertain claim as pending and rechecks status', async () => {
  let ready = false
  vi.stubGlobal('fetch', vi.fn(async (path: RequestInfo | URL) => {
    if (String(path) === '/api/setup/claim') {
      return new Response(JSON.stringify({ status: 'claim_pending' }), { status: 202 })
    }
    return new Response(JSON.stringify({ status: ready ? 'setup_complete' : 'awaiting_setup' }))
  }))
  showSetup()
  await screen.findByText(/Your server is ready for account setup/)
  await fillPasswords('correct-horse-battery', 'correct-horse-battery')
  await userEvent.click(screen.getByRole('button', { name: 'Create account' }))
  expect(await screen.findByRole('button', { name: 'Check setup status' })).toBeTruthy()
  ready = true
  await userEvent.click(screen.getByRole('button', { name: 'Check setup status' }))
  await waitFor(() => expect(screen.getByText(/Your account is ready. Sign in/)).toBeTruthy())
  expect(clearHostedClaim).toHaveBeenCalledOnce()
  expect(screen.getByRole('link', { name: /Sign in/ }).getAttribute('href')).toBe('/')
})

it('refuses creation when the browser has no claim secret', async () => {
  vi.mocked(hostedClaimSecret).mockReturnValueOnce('')
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ status: 'awaiting_setup' }))))
  showSetup()
  await screen.findByText(/Your server is ready for account setup/)
  await fillPasswords('correct-horse-battery', 'correct-horse-battery')
  await userEvent.click(screen.getByRole('button', { name: 'Create account' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent',
    'Open the setup link from your verified order page again.')
  expect(fetch).toHaveBeenCalledTimes(1)
})
