import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { CustodyBanner } from './custody-repair-panel'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const NEEDS_REVIEW = {
  state: 'needs_review',
  keys: [
    { key: 'JIRA_API_TOKEN', reason: 'undeclared', kept: false },
    { key: 'OLD_NOTE', reason: 'undeclared', kept: false },
  ],
  targets: [{ connection: 'work_slack', field: 'user_token' }],
  affected_connections: ['work_slack'],
}

function stubApi(posts: unknown[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        posts.push(JSON.parse(String(init.body)))
        return new Response(JSON.stringify({ state: 'ok', keys: [], targets: [], affected_connections: [] }))
      }
      return new Response(JSON.stringify(NEEDS_REVIEW))
    }),
  )
}

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CustodyBanner />
    </QueryClientProvider>,
  )
}

it('says credentials need review and names the affected connection', async () => {
  stubApi([])
  show()
  expect(await screen.findByText(/Credentials need review\./)).toBeTruthy()
  expect(screen.getByText(/work_slack/)).toBeTruthy()
})

it('shows nothing when custody is healthy', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ state: 'ok', keys: [], targets: [], affected_connections: [] }))),
  )
  show()
  await waitFor(() => expect(fetch).toHaveBeenCalled())
  expect(screen.queryByText(/Credentials need review/)).toBeNull()
})

it('needs every key answered, and a drop needs its name typed back', async () => {
  const posts: unknown[] = []
  stubApi(posts)
  show()
  await userEvent.click(await screen.findByRole('button', { name: 'Review' }))
  const apply = screen.getByRole('button', { name: 'Apply answers' }) as HTMLButtonElement
  expect(apply.disabled).toBe(true)

  await userEvent.selectOptions(screen.getByLabelText('What to do with OLD_NOTE'), 'keep')
  await userEvent.selectOptions(screen.getByLabelText('What to do with JIRA_API_TOKEN'), 'drop')
  expect(apply.disabled).toBe(true)
  await userEvent.type(screen.getByLabelText('Type JIRA_API_TOKEN to confirm the drop'), 'JIRA_API_TOKEN')
  expect(apply.disabled).toBe(false)

  await userEvent.click(apply)
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0]).toEqual({
    decisions: [
      { key: 'JIRA_API_TOKEN', action: 'drop', confirm: 'JIRA_API_TOKEN' },
      { key: 'OLD_NOTE', action: 'keep' },
    ],
  })
})

it('maps a key onto a connection field with no value anywhere on screen', async () => {
  const posts: unknown[] = []
  stubApi(posts)
  show()
  await userEvent.click(await screen.findByRole('button', { name: 'Review' }))
  await userEvent.selectOptions(screen.getByLabelText('What to do with JIRA_API_TOKEN'), 'map')
  await userEvent.selectOptions(screen.getByLabelText('Field for JIRA_API_TOKEN'), 'work_slack/user_token')
  await userEvent.selectOptions(screen.getByLabelText('What to do with OLD_NOTE'), 'keep')
  await userEvent.click(screen.getByRole('button', { name: 'Apply answers' }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect((posts[0] as { decisions: unknown[] }).decisions[0]).toEqual({
    key: 'JIRA_API_TOKEN',
    action: 'map',
    connection: 'work_slack',
    field: 'user_token',
  })
  expect(screen.queryByRole('textbox', { name: /value/i })).toBeNull()
})
