// SPEC-087 T-1288 (COMP-013, REQ-573, REQ-574): Settings -> Apps.
//
// Contract pinned here for the green step:
// - `AppsPanel` is exported from `@/pages/apps` (mirrors `UsersPanel`).
// - Rows come from GET /api/principals ({principals: [...]}); agents resolve
//   through the roster (/api/team/roster), subjects through /api/users, the
//   deployment tier through /api/settings/public-address (`tier`), the web
//   app's existing source for it.
// - A "New app" button opens the form; the submit button's name starts "Issue".
// - The issue form uses one checkbox per (scope, roster agent) and one per
//   person; the expiry is a date input labelled "Expires".
// - A pending row carries `approval_id` and links to the approval card under
//   /approvals. NOTE: the GET list row must include `approval_id` for pending
//   principals (not yet asserted by test_principal_routes.py).
// - Revoke asks with window.confirm (the existing confirm pattern) before POST.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AppsPanel } from '@/pages/apps'

const OLIVIA = 'did:arc:default:agent/olivia'
const BOB = 'did:arc:default:agent/bob'
const ADA = 'did:arc:default:user/ada'
const SECRET = 'arcp_0123456789abcdef0123456789abcdef.SECRETSECRETSECRETSECRETSECRETSECRETSECRE'
const DIGEST = 'f00dfeedf00dfeedf00dfeedf00dfeedf00dfeedf00dfeedf00dfeedf00dfeed'

const ACTIVE_ROW = {
  id: 'p-active',
  name: 'billing-app',
  did: 'did:arc:default:app/billing',
  scopes: { 'inbox:send': [OLIVIA], 'runs:read': [BOB] },
  agents: [OLIVIA, BOB],
  subjects: [ADA],
  status: 'active',
  created_by: 'did:arc:default:user/op',
  approved_by: null,
  last_used_at: null,
  expires_at: '2027-01-15T00:00:00Z',
  // Never sent by the route; a defensive UI still must not render it.
  secret_digest: DIGEST,
}

const PENDING_ROW = {
  ...ACTIVE_ROW,
  id: 'p-pending',
  name: 'crm-app',
  did: 'did:arc:default:app/crm',
  status: 'pending',
  approval_id: 'appr-42',
  secret_digest: undefined,
}

interface Call {
  method: string
  path: string
  body?: Record<string, unknown>
}

interface StubOptions {
  tier?: 'personal' | 'enterprise' | 'federal'
  rows?: unknown[]
  listStatus?: number
  issueStatus?: number
  issueReply?: unknown
}

function stubFetch(opts: StubOptions = {}): Call[] {
  const { tier = 'personal', rows = [ACTIVE_ROW], listStatus = 200, issueStatus = 201 } = opts
  const issueReply = opts.issueReply ?? {
    principal: { ...ACTIVE_ROW, id: 'p-new', name: 'new-app', secret_digest: undefined },
    secret: SECRET,
    status: 'active',
  }
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const method = init?.method ?? 'GET'
      calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      const reply = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
      if (path === '/api/principals' && method === 'GET') {
        return listStatus === 200
          ? reply({ principals: rows })
          : reply({ error: 'operator_session_required' }, listStatus)
      }
      if (path === '/api/principals' && method === 'POST') return reply(issueReply, issueStatus)
      if (path.endsWith('/revoke')) return reply({ principal: { ...ACTIVE_ROW, status: 'revoked' } })
      if (path.startsWith('/api/team/roster')) {
        return reply({
          agents: [
            { agent_id: 'olivia', name: 'olivia', display_name: 'Olivia', did: OLIVIA },
            { agent_id: 'bob', name: 'bob', display_name: 'Bob', did: BOB },
          ],
        })
      }
      if (path === '/api/users') {
        return reply({
          users: [
            {
              id: 'u1',
              email: 'ada@example.com',
              did: ADA,
              handle: 'ada',
              display_name: 'Ada',
              roles: ['viewer'],
              pairings: [],
              disabled: false,
              created_at: 0,
            },
          ],
        })
      }
      if (path.startsWith('/api/settings/public-address')) {
        return reply({
          public_base_url: null,
          redirect_uri: 'http://127.0.0.1:8420/oauth/callback',
          tier,
          https_required: tier !== 'personal',
          suggestions: [],
        })
      }
      return reply({})
    }),
  )
  return calls
}

function renderPanel(client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AppsPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { client, ...view }
}

/** The row (list item or table row) that holds `text`. */
function rowFor(text: string): HTMLElement {
  const row = screen.getByText(text).closest('li, tr')
  if (!(row instanceof HTMLElement)) throw new Error(`no row for ${text}`)
  return row
}

/** Local YYYY-MM-DD `days` from today, the value a date input holds. */
function dateInDays(days: number): string {
  const d = new Date()
  d.setDate(d.getDate() + days)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

const issuePosts = (calls: Call[]) =>
  calls.filter((c) => c.method === 'POST' && c.path === '/api/principals')

async function openIssueForm(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByText('billing-app')
  await user.click(screen.getByRole('button', { name: /new app/i }))
  await user.type(await screen.findByLabelText(/app name/i), 'new-app')
  await user.type(screen.getByLabelText(/public key/i), 'ab'.repeat(32))
  await user.click(await screen.findByRole('checkbox', { name: /inbox:send.*Olivia/i }))
  await user.click(await screen.findByRole('checkbox', { name: /ada@example\.com/i }))
}

function setExpiry(days: number) {
  fireEvent.change(screen.getByLabelText(/expires/i), { target: { value: dateInDays(days) } })
}

const submit = (user: ReturnType<typeof userEvent.setup>) =>
  user.click(screen.getByRole('button', { name: /^issue/i }))

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('AppsPanel list', () => {
  it('shows name, DID, scopes by agent, subjects, status, last use and expiry', async () => {
    stubFetch()
    renderPanel()
    await screen.findByText('billing-app')
    const row = rowFor('billing-app')
    const text = row.textContent ?? ''
    expect(text).toContain('did:arc:default:app/billing')
    expect(text).toContain('inbox:send')
    expect(text).toContain('runs:read')
    await waitFor(() => expect(row.textContent).toMatch(/Olivia/))
    expect(row.textContent).toMatch(/Bob/)
    await waitFor(() => expect(row.textContent).toMatch(/ada@example\.com/))
    expect(text).toMatch(/active/i)
    expect(text).toMatch(/never/i) // last_used_at null
    expect(text).toMatch(/2027/) // expiry
  })

  it('never renders a secret or digest', async () => {
    stubFetch()
    renderPanel()
    await screen.findByText('billing-app')
    expect(document.body.textContent).not.toContain(DIGEST)
    expect(document.body.textContent).not.toMatch(/arcp_/)
  })

  it('links a pending row to its approval card', async () => {
    stubFetch({ tier: 'enterprise', rows: [PENDING_ROW] })
    renderPanel()
    await screen.findByText('crm-app')
    const row = rowFor('crm-app')
    expect(row.textContent).toMatch(/pending/i)
    const link = within(row).getByRole('link', { name: /approv/i })
    const href = link.getAttribute('href') ?? ''
    expect(href.startsWith('/approvals')).toBe(true)
    expect(href).toContain('appr-42')
  })

  it('tells a viewer that only an operator can manage apps', async () => {
    stubFetch({ listStatus: 403 })
    renderPanel()
    expect(await screen.findByText('Only an operator can manage apps.')).toBeTruthy()
  })
})

describe('AppsPanel issue form', () => {
  it('offers one scope checkbox per roster agent and one subject per person', async () => {
    stubFetch()
    renderPanel()
    const user = userEvent.setup()
    await screen.findByText('billing-app')
    await user.click(screen.getByRole('button', { name: /new app/i }))
    expect(await screen.findByRole('checkbox', { name: /inbox:send.*Olivia/i })).toBeTruthy()
    expect(screen.getByRole('checkbox', { name: /inbox:send.*Bob/i })).toBeTruthy()
    expect(screen.getByRole('checkbox', { name: /gates:resolve.*Olivia/i })).toBeTruthy()
    expect(screen.getByRole('checkbox', { name: /ada@example\.com/i })).toBeTruthy()
  })

  it('posts the scope x agent map, subject DIDs and expiry', async () => {
    const calls = stubFetch({ tier: 'enterprise' })
    renderPanel()
    const user = userEvent.setup()
    await openIssueForm(user)
    setExpiry(10)
    await submit(user)

    await waitFor(() => expect(issuePosts(calls)).toHaveLength(1))
    const body = issuePosts(calls)[0].body ?? {}
    expect(body.name).toBe('new-app')
    expect(body.public_key).toBe('ab'.repeat(32))
    expect(body.scopes).toEqual({ 'inbox:send': [OLIVIA] })
    expect(body.subjects).toEqual([ADA])
    const days = (Date.parse(String(body.expires_at)) - Date.now()) / 86_400_000
    expect(days).toBeGreaterThan(9)
    expect(days).toBeLessThan(11.01)
  })

  it.each([
    ['enterprise', 95],
    ['federal', 35],
  ] as const)('blocks a %s expiry %i days out before calling the server', async (tier, days) => {
    const calls = stubFetch({ tier })
    renderPanel()
    const user = userEvent.setup()
    await openIssueForm(user)
    setExpiry(days)
    await submit(user)

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(issuePosts(calls)).toHaveLength(0)
  })

  it.each([
    ['enterprise', 85],
    ['federal', 25],
  ] as const)('accepts a %s expiry %i days out', async (tier, days) => {
    const calls = stubFetch({ tier })
    renderPanel()
    const user = userEvent.setup()
    await openIssueForm(user)
    setExpiry(days)
    await submit(user)
    await waitFor(() => expect(issuePosts(calls)).toHaveLength(1))
  })

  it('requires an expiry above personal', async () => {
    const calls = stubFetch({ tier: 'federal' })
    renderPanel()
    const user = userEvent.setup()
    await openIssueForm(user)
    await submit(user)
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(issuePosts(calls)).toHaveLength(0)
  })

  it('lets personal issue with no expiry', async () => {
    const calls = stubFetch({ tier: 'personal' })
    renderPanel()
    const user = userEvent.setup()
    await openIssueForm(user)
    await submit(user)
    await waitFor(() => expect(issuePosts(calls)).toHaveLength(1))
    expect(issuePosts(calls)[0].body?.expires_at).toBeNull()
  })

  it('shows the server refusal and no secret when the issue is refused', async () => {
    stubFetch({ issueStatus: 400, issueReply: { error: 'expiry_too_long' } })
    renderPanel()
    const user = userEvent.setup()
    await openIssueForm(user)
    await submit(user)
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.queryByText(SECRET)).toBeNull()
  })
})

describe('AppsPanel one-time secret', () => {
  it('reveals the secret once with a copy button, then forgets it on close', async () => {
    stubFetch()
    const { client, unmount } = renderPanel()
    const user = userEvent.setup()
    await openIssueForm(user)
    await submit(user)

    expect(await screen.findByText(SECRET)).toBeTruthy()
    expect(screen.getByRole('button', { name: /copy/i })).toBeTruthy()

    await user.click(screen.getByRole('button', { name: /done|close|i saved it/i }))
    expect(screen.queryByText(SECRET)).toBeNull()

    // Nothing kept it: not the query cache, not browser storage, not a remount.
    const cached = JSON.stringify(client.getQueryCache().getAll().map((q) => q.state.data))
    expect(cached).not.toContain(SECRET)
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(SECRET)
    expect(JSON.stringify({ ...localStorage })).not.toContain(SECRET)
    unmount()
    renderPanel(client)
    await screen.findByText('billing-app')
    expect(document.body.textContent).not.toContain(SECRET)
  })

  it('links a pending issue to its approval card next to the secret', async () => {
    stubFetch({
      tier: 'enterprise',
      issueReply: {
        principal: { ...PENDING_ROW, approval_id: undefined },
        secret: SECRET,
        status: 'pending',
        approval_id: 'appr-77',
      },
    })
    renderPanel()
    const user = userEvent.setup()
    await openIssueForm(user)
    setExpiry(10)
    await submit(user)
    await screen.findByText(SECRET)
    const links = screen.getAllByRole('link', { name: /approv/i })
    expect(links.some((l) => (l.getAttribute('href') ?? '').includes('appr-77'))).toBe(true)
  })
})

describe('AppsPanel revoke', () => {
  it('does nothing when the confirmation is cancelled', async () => {
    const calls = stubFetch()
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderPanel()
    await screen.findByText('billing-app')
    await userEvent.setup().click(within(rowFor('billing-app')).getByRole('button', { name: /revoke/i }))
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(calls.some((c) => c.path.endsWith('/revoke'))).toBe(false)
  })

  it('posts the revoke route only after the confirmation', async () => {
    const calls = stubFetch()
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderPanel()
    await screen.findByText('billing-app')
    await userEvent.setup().click(within(rowFor('billing-app')).getByRole('button', { name: /revoke/i }))
    expect(confirm).toHaveBeenCalledTimes(1)
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.path === '/api/principals/p-active/revoke')).toBe(true),
    )
  })

  it('offers no revoke on an already revoked row', async () => {
    stubFetch({ rows: [{ ...ACTIVE_ROW, status: 'revoked' }] })
    renderPanel()
    await screen.findByText('billing-app')
    expect(within(rowFor('billing-app')).queryByRole('button', { name: /revoke/i })).toBeNull()
  })
})
