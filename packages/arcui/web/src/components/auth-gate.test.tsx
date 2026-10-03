import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AuthGate } from '@/components/auth-gate'
import { getToken } from '@/lib/auth'

interface Post {
  path: string
  body: Record<string, unknown>
}

function stubFetch(
  mode: { login_available: boolean; setup_available: boolean },
  handlers: Record<string, { status: number; body: unknown }> = {},
): Post[] {
  const posts: Post[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const reply = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
      if (path === '/api/auth/mode') return reply(mode)
      posts.push({ path, body: JSON.parse(String(init?.body ?? '{}')) })
      const handler = handlers[path]
      return handler ? reply(handler.body, handler.status) : reply({}, 404)
    }),
  )
  return posts
}

const PASSWORD = 'correct horse battery'

beforeEach(() => {
  localStorage.clear()
  window.location.hash = ''
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('AuthGate first run', () => {
  it('posts the setup code, email and password, then stores the token', async () => {
    const posts = stubFetch(
      { login_available: false, setup_available: true },
      { '/api/auth/setup': { status: 200, body: { token: 'tok-1', email: 'a@b.co', role: 'operator' } } },
    )
    render(
      <AuthGate>
        <p>inside the app</p>
      </AuthGate>,
    )
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Setup code'), 'CODE-123')
    await user.type(screen.getByLabelText('Email'), 'a@b.co')
    await user.type(screen.getByLabelText('Password'), PASSWORD)
    await user.type(screen.getByLabelText('Repeat password'), PASSWORD)
    await user.click(screen.getByRole('button', { name: 'Create account' }))

    await screen.findByText('inside the app')
    expect(posts[0].path).toBe('/api/auth/setup')
    expect(posts[0].body).toMatchObject({ setup_code: 'CODE-123', email: 'a@b.co', password: PASSWORD })
    expect(getToken()).toBe('tok-1')
  })

  it('tells the person where the setup code is, in plain words', async () => {
    stubFetch({ login_available: false, setup_available: true })
    render(<AuthGate>x</AuthGate>)
    expect(await screen.findByText(/ARC FIRST-RUN SETUP CODE/)).toBeTruthy()
  })

  it('refuses a short password before calling the server', async () => {
    const posts = stubFetch({ login_available: false, setup_available: true })
    render(<AuthGate>x</AuthGate>)
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Setup code'), 'C')
    await user.type(screen.getByLabelText('Email'), 'a@b.co')
    await user.type(screen.getByLabelText('Password'), 'short')
    await user.click(screen.getByRole('button', { name: 'Create account' }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/at least 12/i)
    expect(posts).toHaveLength(0)
  })
})

describe('AuthGate wording', () => {
  it.each([
    ['first run', { login_available: false, setup_available: true }],
    ['sign in', { login_available: true, setup_available: false }],
    ['token only', { login_available: false, setup_available: false }],
  ])('never shows a command on the %s screen', async (_name, mode) => {
    stubFetch(mode)
    const { container } = render(<AuthGate>x</AuthGate>)
    await waitFor(() => expect(container.querySelector('h1')).toBeTruthy())
    await new Promise((r) => setTimeout(r, 20))
    const text = container.textContent ?? ''
    for (const command of ['arc user', 'arc ui', 'arc team']) expect(text).not.toContain(command)
  })
})

describe('AuthGate invite link', () => {
  it('checks the link, then accepts it with a new password', async () => {
    window.location.hash = '#invite=abc123'
    const posts = stubFetch(
      { login_available: true, setup_available: false },
      {
        '/api/auth/invite/check': { status: 200, body: { email: 'new@b.co', kind: 'invite', role: 'viewer' } },
        '/api/auth/invite/accept': { status: 200, body: { token: 'tok-2', email: 'new@b.co', role: 'viewer' } },
      },
    )
    render(
      <AuthGate>
        <p>inside the app</p>
      </AuthGate>,
    )
    await screen.findByText('You were invited as new@b.co (viewer)')
    expect(posts[0]).toEqual({ path: '/api/auth/invite/check', body: { token: 'abc123' } })
    expect(window.location.hash).toBe('')

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Password'), PASSWORD)
    await user.type(screen.getByLabelText('Repeat password'), PASSWORD)
    await user.click(screen.getByRole('button', { name: 'Set password and sign in' }))

    await screen.findByText('inside the app')
    expect(posts[1].path).toBe('/api/auth/invite/accept')
    expect(posts[1].body).toMatchObject({ token: 'abc123', password: PASSWORD })
    expect(getToken()).toBe('tok-2')
  })

  it('shows a reset link as a password reset', async () => {
    window.location.hash = '#invite=r1'
    stubFetch(
      { login_available: true, setup_available: false },
      { '/api/auth/invite/check': { status: 200, body: { email: 'old@b.co', kind: 'reset', role: 'operator' } } },
    )
    render(<AuthGate>x</AuthGate>)
    expect(await screen.findByText('Reset the password for old@b.co')).toBeTruthy()
  })

  it('explains an expired link and offers a way back to sign in', async () => {
    window.location.hash = '#invite=old'
    stubFetch(
      { login_available: true, setup_available: false },
      { '/api/auth/invite/check': { status: 404, body: { error: 'That link has expired.' } } },
    )
    render(<AuthGate>x</AuthGate>)
    await screen.findByText('This link no longer works')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Back to sign in' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeTruthy()
  })
})
