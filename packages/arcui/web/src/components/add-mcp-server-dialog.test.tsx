import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AddMcpServerDialog } from '@/components/add-mcp-server-dialog'
import { ApiError, apiPost } from '@/lib/api'
import type { Agent } from '@/lib/types'

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  apiPost: vi.fn(),
}))

const SECRET = 'dialog-s3cr3t-4242'
const agents: Agent[] = [{ agent_id: 'olivia', name: 'Olivia', workspace_path: '/team/olivia_agent' }]

const PREVIEW = {
  tools: [
    { name: 'search_tickets', description: 'Search tickets.', usable: true, reason: '' },
    { name: 'close_ticket', description: 'Close a ticket.', usable: true, reason: '' },
    { name: 'x'.repeat(60), description: 'Too long.', usable: false, reason: 'name cannot be exposed' },
  ],
  suggested_tags: ['network_egress', 'web'],
}

beforeEach(() => {
  vi.mocked(apiPost).mockReset()
})
afterEach(cleanup)

function renderDialog(onOpenChange = vi.fn()) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <AddMcpServerDialog agents={agents} open onOpenChange={onOpenChange} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return onOpenChange
}

async function fillHttp() {
  await userEvent.type(screen.getByLabelText('Name'), 'acme')
  await userEvent.type(screen.getByLabelText('Server URL'), 'https://mcp.acme.example/mcp')
  await userEvent.type(screen.getByLabelText('Credential'), SECRET)
}

it('hides the Discover button until a name and an address are given', async () => {
  renderDialog()
  const discover = screen.getByRole('button', { name: 'Discover tools' }) as HTMLButtonElement
  expect(discover.disabled).toBe(true)
  await userEvent.type(screen.getByLabelText('Name'), 'acme')
  expect(discover.disabled).toBe(true)
  await userEvent.type(screen.getByLabelText('Server URL'), 'https://mcp.acme.example/mcp')
  expect(discover.disabled).toBe(false)
})

it('masks the credential and sends it to the preview route only', async () => {
  vi.mocked(apiPost).mockResolvedValueOnce(PREVIEW)
  renderDialog()
  await fillHttp()
  expect((screen.getByLabelText('Credential') as HTMLInputElement).type).toBe('password')

  await userEvent.click(screen.getByRole('button', { name: 'Discover tools' }))

  await waitFor(() => expect(apiPost).toHaveBeenCalledTimes(1))
  const [path, body] = vi.mocked(apiPost).mock.calls[0] as [string, Record<string, unknown>]
  expect(path).toBe('/api/mcp-servers/preview')
  expect(body.transport).toBe('http')
  expect((body.secrets as Record<string, string>).credential).toBe(SECRET)
})

it('lists what the server offers, with nothing chosen by default, and flags what cannot be exposed', async () => {
  vi.mocked(apiPost).mockResolvedValueOnce(PREVIEW)
  renderDialog()
  await fillHttp()
  await userEvent.click(screen.getByRole('button', { name: 'Discover tools' }))

  const search = await screen.findByRole('checkbox', { name: /search_tickets/ })
  expect((search as HTMLInputElement).checked).toBe(false)
  expect(screen.getByText('name cannot be exposed')).toBeTruthy()
  expect((screen.getByRole('button', { name: 'Add server' }) as HTMLButtonElement).disabled).toBe(true)
})

it('adds only the chosen tools, as the operator classified them, to the chosen agents', async () => {
  vi.mocked(apiPost).mockResolvedValueOnce(PREVIEW).mockResolvedValueOnce({
    instance: 'acme', extension: 'acme', tools: ['acme__search_tickets'], detail: '', agents: ['olivia_agent'],
    spec_sha256: 'a'.repeat(64),
  })
  const onOpenChange = renderDialog()
  await fillHttp()
  await userEvent.click(screen.getByRole('button', { name: 'Discover tools' }))
  await userEvent.click(await screen.findByRole('checkbox', { name: /search_tickets/ }))
  await userEvent.selectOptions(screen.getByLabelText('search_tickets access'), 'read_only')
  await userEvent.click(screen.getByRole('button', { name: 'Olivia' }))

  await userEvent.click(screen.getByRole('button', { name: 'Add server' }))

  await waitFor(() => expect(apiPost).toHaveBeenCalledTimes(2))
  const [path, body] = vi.mocked(apiPost).mock.calls[1] as [string, Record<string, unknown>]
  expect(path).toBe('/api/mcp-servers')
  expect(body.tools).toEqual({
    search_tickets: { classification: 'read_only', capability_tags: ['network_egress', 'web'], description: 'Search tickets.' },
  })
  expect(body.agents).toEqual(['olivia_agent'])
  await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
})

it('defaults a chosen tool to the cautious classification', async () => {
  vi.mocked(apiPost).mockResolvedValueOnce(PREVIEW)
  renderDialog()
  await fillHttp()
  await userEvent.click(screen.getByRole('button', { name: 'Discover tools' }))
  await userEvent.click(await screen.findByRole('checkbox', { name: /close_ticket/ }))

  expect((screen.getByLabelText('close_ticket access') as HTMLSelectElement).value).toBe('state_modifying')
})

it('shows a refusal in words and never shows a credential', async () => {
  vi.mocked(apiPost).mockRejectedValueOnce(new ApiError(400, 'the url must be https'))
  renderDialog()
  await fillHttp()
  await userEvent.click(screen.getByRole('button', { name: 'Discover tools' }))

  expect(await screen.findByText('the url must be https')).toBeTruthy()
  expect(document.body.textContent).not.toContain(SECRET)
})

it('takes a local command and environment credentials for a stdio server', async () => {
  vi.mocked(apiPost).mockResolvedValueOnce(PREVIEW)
  renderDialog()
  await userEvent.type(screen.getByLabelText('Name'), 'localfs')
  await userEvent.selectOptions(screen.getByLabelText('Transport'), 'stdio')
  await userEvent.type(screen.getByLabelText('Command'), '/usr/local/bin/mcp-files /srv/docs')
  await userEvent.type(screen.getByLabelText('Credential name'), 'api_key')
  await userEvent.type(screen.getByLabelText('Environment variable'), 'FILES_API_KEY')
  await userEvent.type(screen.getByLabelText('Credential value'), SECRET)

  await userEvent.click(screen.getByRole('button', { name: 'Discover tools' }))

  await waitFor(() => expect(apiPost).toHaveBeenCalledTimes(1))
  const [, body] = vi.mocked(apiPost).mock.calls[0] as [string, Record<string, unknown>]
  expect(body.transport).toBe('stdio')
  expect(body.argv).toEqual(['/usr/local/bin/mcp-files', '/srv/docs'])
  expect(body.env_refs).toEqual({ api_key: 'FILES_API_KEY' })
  expect((body.secrets as Record<string, string>).api_key).toBe(SECRET)
})

it('forgets every typed value when closed', async () => {
  const onOpenChange = vi.fn()
  const { rerender } = render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <AddMcpServerDialog agents={agents} open onOpenChange={onOpenChange} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await userEvent.type(screen.getByLabelText('Credential'), SECRET)
  rerender(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <AddMcpServerDialog agents={agents} open={false} onOpenChange={onOpenChange} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  expect(document.body.textContent).not.toContain(SECRET)
})
