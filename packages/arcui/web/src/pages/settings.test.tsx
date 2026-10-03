// Item 9 — "Memory sharing" lives in Settings, under agent scope only.
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { SettingsPage } from '@/pages/settings'

// Radix Select needs these browser APIs, which jsdom lacks.
beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => {}
  Element.prototype.releasePointerCapture ??= () => {}
  Element.prototype.scrollIntoView ??= () => {}
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL) => {
      const path = String(request)
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      if (path.includes('/api/team/roster')) {
        return json({ agents: [{ agent_id: 'olivia', name: 'olivia', display_name: 'Olivia' }] })
      }
      if (path.includes('/memory/promotion')) {
        return json({
          enabled: false,
          confidence_threshold: 0.95,
          classifier_model: 'jev-1.13.0',
          tier: 'personal',
          federal_locked: false,
          key_set: false,
        })
      }
      if (path.includes('/api/settings/public-address')) {
        return json({
          public_base_url: null,
          redirect_uri: 'http://127.0.0.1:8420/oauth/callback',
          tier: 'personal',
          https_required: false,
          suggestions: [],
        })
      }
      if (path.includes('/api/settings/tls')) {
        return json({ configured: false, active: false, required: false, subject: null, not_after: null, dns_names: [] })
      }
      if (path.includes('/api/classifiers/')) {
        return json({ classifier: 'jev', models: ['jev-1.13.0'] })
      }
      return json({})
    }),
  )
}

function renderSettings(initialEntry = '/settings') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('SettingsPage — Access tab', () => {
  it('opens on the Access tab when the link says ?tab=access', async () => {
    stubFetch()
    renderSettings('/settings?tab=access')
    const tab = await screen.findByRole('tab', { name: 'Access' })
    expect(tab.getAttribute('aria-selected')).toBe('true')
  })

  it('shows the Access tab in agent scope and System scope', async () => {
    stubFetch()
    renderSettings()
    expect(await screen.findByRole('tab', { name: 'Access' })).toBeTruthy()

    await userEvent.click(screen.getByRole('combobox'))
    await userEvent.click(await screen.findByRole('option', { name: /system/i }))
    expect(screen.getByRole('tab', { name: 'Access' })).toBeTruthy()
  })

  it('renders the public address panel when opened', async () => {
    stubFetch()
    renderSettings()
    await userEvent.click(await screen.findByRole('tab', { name: 'Access' }))
    expect(await screen.findByRole('heading', { name: 'Public address' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'HTTPS certificate' })).toBeTruthy()
  })
})

describe('SettingsPage — Memory sharing tab', () => {
  it('shows the tab in agent scope and renders the panel', async () => {
    stubFetch()
    renderSettings()

    const tab = await screen.findByRole('tab', { name: /memory sharing/i })
    await userEvent.click(tab)
    expect(await screen.findByRole('heading', { name: /memory sharing/i })).toBeTruthy()
  })

  it('hides the tab in System scope', async () => {
    stubFetch()
    renderSettings()
    await screen.findByRole('tab', { name: /memory sharing/i })

    await userEvent.click(screen.getByRole('combobox'))
    await userEvent.click(await screen.findByRole('option', { name: /system/i }))

    expect(screen.queryByRole('tab', { name: /memory sharing/i })).toBeNull()
    expect(screen.getByRole('tab', { name: /keys/i })).toBeTruthy()
  })
})
