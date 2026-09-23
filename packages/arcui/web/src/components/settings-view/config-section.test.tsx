import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { focusManager, onlineManager, QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { ConfigSection } from '@/components/settings-view/config-section'
import { createQueryClient } from '@/lib/query-client'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  focusManager.setFocused(undefined)
  onlineManager.setOnline(true)
})

it('refreshes stale settings after focus or reconnect without replacing an unsaved draft', async () => {
  let serverModel = 'old/model'
  const fetchConfig = vi.fn(async () => ({ model: serverModel }))
  function SettingsHarness() {
    const query = useQuery({ queryKey: ['settings'], queryFn: fetchConfig, staleTime: 0 })
    return query.data ? (
      <ConfigSection endpoint="/api/system-config/arcllm" queryKey={['settings']} sectionKey="core" file="arcllm" value={query.data} editable />
    ) : null
  }
  const client = createQueryClient()
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter><SettingsHarness /></MemoryRouter>
    </QueryClientProvider>,
  )
  await screen.findByText('old/model')
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  const draft = screen.getByRole('textbox', { name: 'model' }) as HTMLInputElement
  await userEvent.clear(draft)
  await userEvent.type(draft, 'unsaved/model')
  serverModel = 'new/model'
  focusManager.setFocused(false)
  focusManager.setFocused(true)
  await waitFor(() => expect(fetchConfig.mock.calls.length).toBeGreaterThanOrEqual(2))
  expect(draft.value).toBe('unsaved/model')
  onlineManager.setOnline(false)
  onlineManager.setOnline(true)
  await waitFor(() => expect(fetchConfig.mock.calls.length).toBeGreaterThanOrEqual(3))
  expect(draft.value).toBe('unsaved/model')
  client.clear()
})

it('keeps numeric controls typed through clear and retype and blocks an empty value', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }))
  vi.stubGlobal('fetch', fetch)
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ConfigSection endpoint="/api/system-config/arcrun" queryKey={['config']} sectionKey="core" file="arcrun" value={{ timeout: 1.5 }} editable />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  const input = screen.getByRole('spinbutton', { name: 'timeout' }) as HTMLInputElement
  await userEvent.clear(input)
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  expect(screen.getByRole('alert').textContent).toMatch(/valid number/)
  expect(fetch).not.toHaveBeenCalled()
  await userEvent.type(input, '-2.75')
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ core: { timeout: -2.75 } })
})

it('shows schema-specific help in read and guided edit modes and saves a typed scalar', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }))
  vi.stubGlobal('fetch', fetch)
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ConfigSection endpoint="/api/agents/example/config/arcllm" queryKey={['config']} sectionKey="llm" file="arcllm" value={{ model: 'old/model' }} editable />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Help for Default model' }))
  expect(screen.getByText(/Provider\/model identifier used for this agent/)).toBeTruthy()
  await userEvent.keyboard('{Escape}')
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  const input = screen.getByRole('textbox', { name: 'model' }) as HTMLInputElement
  await userEvent.clear(input)
  await userEvent.type(input, 'new/model')
  await userEvent.click(screen.getByRole('button', { name: 'Help for Default model' }))
  expect(input.value).toBe('new/model')
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ llm: { model: 'new/model' } })
})

it('shows authored help for named entries in read and guided modes', async () => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ConfigSection endpoint="/api/agents/example/config/arcagent" queryKey={['config']} sectionKey="tools" file="arcagent"
          value={{ mcp_servers: { lookup: { command: 'lookup' } } }} editable />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Help for MCP command' }))
  expect(screen.getByText(/Executable used to start this named MCP server/)).toBeTruthy()
  await userEvent.keyboard('{Escape}')
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  await userEvent.click(screen.getByRole('button', { name: 'Help for MCP command' }))
  expect(screen.getByText(/Executable used to start this named MCP server/)).toBeTruthy()
})

it('shows authored help on a top-level ArcRun scalar section', async () => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ConfigSection endpoint="/api/agents/example/config/arcrun" queryKey={['config']} sectionKey="max_turns" file="arcrun" value={25} editable />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Help for Maximum turns' }))
  expect(screen.getByText(/Hard ceiling on agentic loop turns/)).toBeTruthy()
})

it('shows authored help for a top-level array edited in Advanced JSON', async () => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ConfigSection endpoint="/api/agents/example/config/arcrun" queryKey={['config']} sectionKey="allowed_strategies" file="arcrun" value={['react']} editable />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  await userEvent.click(screen.getAllByRole('button', { name: 'Help for Allowed run strategies' })[1])
  expect(screen.getByText(/Operator ceiling on strategy names/)).toBeTruthy()
})

it('shows route phrase help for a nested array in guided mode', async () => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ConfigSection endpoint="/api/agents/example/config/arcllm" queryKey={['config']} sectionKey="llm" file="arcllm"
          value={{ routes: { coding: { phrases: ['implement'] } } }} editable />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  expect(screen.getByText(/phrases: this value can be changed in Advanced JSON/)).toBeTruthy()
  await userEvent.click(screen.getByRole('button', { name: 'Help for Route phrases' }))
  expect(screen.getByText('Example phrases used by ArcLLM to select this named route.')).toBeTruthy()
})
