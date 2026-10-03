import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { OAuthCallbackPage } from '@/pages/oauth-callback'

const messages: unknown[] = []

class FakeChannel {
  name: string
  constructor(name: string) {
    this.name = name
  }
  postMessage(message: unknown) {
    messages.push({ channel: this.name, message })
  }
  close() {}
}

let landed = ''

beforeEach(() => {
  messages.length = 0
  vi.stubGlobal('BroadcastChannel', FakeChannel)
  window.history.replaceState(null, '', '/oauth/callback?code=secret-code&state=s1')
  landed = window.location.href
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

function stubComplete(response: Response) {
  const fetchMock = vi.fn(async () => response)
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('OAuthCallbackPage', () => {
  it('posts the landed address once, even under StrictMode, and strips the query', async () => {
    localStorage.setItem('arcui_viewer_token', 'tok')
    const fetchMock = stubComplete(new Response(JSON.stringify({ instance: 'x' })))
    render(
      <StrictMode>
        <OAuthCallbackPage />
      </StrictMode>,
    )
    await screen.findByText('Connected. You can close this tab.')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/oauth/complete')
    expect(JSON.parse(String(init.body))).toEqual({ redirect_url: expect.stringContaining('code=secret-code') })
    expect(window.location.search).toBe('')
  })

  it('broadcasts on success so the opener refetches', async () => {
    localStorage.setItem('arcui_viewer_token', 'tok')
    stubComplete(new Response(JSON.stringify({ instance: 'x' })))
    render(<OAuthCallbackPage />)
    await screen.findByText('Connected. You can close this tab.')
    expect(messages).toEqual([{ channel: 'arc-connections', message: { type: 'connected' } }])
  })

  it('shows the server error as plain text and does not broadcast', async () => {
    localStorage.setItem('arcui_viewer_token', 'tok')
    stubComplete(new Response(JSON.stringify({ error: '<b>state expired</b>' }), { status: 400 }))
    render(<OAuthCallbackPage />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toBe('<b>state expired</b>')
    expect(alert.querySelector('b')).toBeNull()
    expect(messages).toEqual([])
  })

  it('shows the paste fallback without a session and posts nothing', async () => {
    const fetchMock = stubComplete(new Response('{}'))
    render(<OAuthCallbackPage />)
    expect(
      await screen.findByText("Paste this page's address into the Arc tab that started the sign-in."),
    ).toBeTruthy()
    const box = screen.getByLabelText("This page's address") as HTMLInputElement
    await waitFor(() => expect(box.value).toBe(landed))
    expect(box.readOnly).toBe(true)
    expect(screen.getByRole('button', { name: /Copy/ })).toBeTruthy()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(window.location.search).toBe('')
  })
})
