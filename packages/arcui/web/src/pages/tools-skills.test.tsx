import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { ToolsSkillsPage } from './tools-skills'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const payload = {
  tools: [
    { name: 'read', agents: ['mc'], source: 'builtin', classification: 'read_only' },
    { name: 'bash', agents: ['mc', 'ada'], source: 'builtin', classification: 'external_effect' },
    { name: 'crm', agents: ['ada'], source: 'extension', classification: 'external_effect' },
  ],
  skills: [
    { name: 'plan', agent_id: 'mc', source: 'agent', status: 'loaded' },
    { name: 'recall', agent_id: 'ada', source: 'module', status: 'loaded' },
  ],
}

function show() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: RequestInfo | URL) => {
      if (String(path).includes('tools-skills')) return new Response(JSON.stringify(payload))
      return new Response(JSON.stringify({ agents: [] }))
    }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ToolsSkillsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const count = (label: string) =>
  within(screen.getByText(label, { selector: 'span' }).closest('div')!.parentElement!).getByText(/^\d+$/).textContent

it('starts unfiltered with matching count cards', async () => {
  show()
  await screen.findByText('crm')
  expect(count('Tools')).toBe('3')
  expect(count('Skills')).toBe('2')
})

it('a source chip keeps only that source and the cards follow', async () => {
  show()
  await screen.findByText('crm')
  await userEvent.click(screen.getByRole('button', { name: /builtin/i }))
  expect(screen.queryByText('crm')).toBeNull()
  expect(count('Tools')).toBe('2')
  expect(count('Skills')).toBe('0')
  await userEvent.click(screen.getByRole('button', { name: /extension/i }))
  expect(count('Tools')).toBe('3')
})

it('the type filter hides the other kind and zeroes its card', async () => {
  show()
  await screen.findByText('crm')
  await userEvent.click(screen.getByRole('button', { name: 'skills' }))
  expect(count('Tools')).toBe('0')
  expect(count('Skills')).toBe('2')
})

it('clear filters restores everything', async () => {
  show()
  await screen.findByText('crm')
  await userEvent.click(screen.getByRole('button', { name: /module/i }))
  await userEvent.click(screen.getByRole('button', { name: 'tools' }))
  expect(count('Tools')).toBe('0')
  await userEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
  expect(count('Tools')).toBe('3')
  expect(count('Skills')).toBe('2')
})
