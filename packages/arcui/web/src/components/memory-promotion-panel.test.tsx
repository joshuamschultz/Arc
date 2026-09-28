// SPEC-083 T-1222 (COMP-028; REQ-509, REQ-510) — the "Memory sharing" panel.
//
// Contract assumed by these tests:
//   import { MemoryPromotionPanel } from '@/components/memory-promotion-panel'
//   <MemoryPromotionPanel agentId="olivia" operatorMode={true|false} />
//
//   GET  /api/agents/{id}/memory/promotion
//        -> { enabled, confidence_threshold, classifier_model, tier, federal_locked, key_set }
//   PUT  /api/agents/{id}/memory/promotion   body: settings only, never a key
//   PUT  /api/keys/TYPESAFE_API_KEY          body: { value }  — the ONLY place a key goes
//
// Accessible names pinned:
//   heading "Memory sharing"; toggle (switch or checkbox) /enable memory sharing/i;
//   inputs labelled /confidence threshold/i, /classifier model/i, /jev api key/i;
//   buttons /save settings/i and /save key/i; key badge text "Key set" / "Key not set".
//
// The server is stubbed at the network boundary (global fetch), as in
// remote-sign-in-panel.test.tsx, so the panel is free to use any lib/queries hook.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { MemoryPromotionPanel } from '@/components/memory-promotion-panel'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const SENTINEL = 'ts-zzz-panel-jev-key-sentinel-5150'
const SETTINGS_PATH = '/api/agents/olivia/memory/promotion'
const KEY_PATH = '/api/keys/TYPESAFE_API_KEY'

type Call = { path: string; method: string; rawBody: string; body: unknown }

type Settings = {
  enabled: boolean
  confidence_threshold: number
  classifier_model: string
  tier: string
  federal_locked: boolean
  key_set: boolean
}

const personal = (over: Partial<Settings> = {}): Settings => ({
  enabled: false,
  confidence_threshold: 0.95,
  classifier_model: 'jev-1.13',
  tier: 'personal',
  federal_locked: false,
  key_set: false,
  ...over,
})

function stubServer({
  settings = personal(),
  putStatus = 200,
  putError = 'confidence_threshold must be at least 0.90',
}: { settings?: Settings; putStatus?: number; putError?: string } = {}) {
  const calls: Call[] = []
  let current = { ...settings }
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
    const path = String(request)
    const method = init?.method ?? 'GET'
    const rawBody = init?.body ? String(init.body) : ''
    calls.push({ path, method, rawBody, body: rawBody ? JSON.parse(rawBody) : undefined })
    const json = (status: number, body: unknown) =>
      new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

    if (path.startsWith(SETTINGS_PATH) && method === 'GET') return json(200, current)
    if (path.startsWith(SETTINGS_PATH) && method === 'PUT') {
      if (putStatus !== 200) return json(putStatus, { error: putError })
      current = { ...current, ...(JSON.parse(rawBody) as Partial<Settings>) }
      return json(200, current)
    }
    if (path.startsWith(KEY_PATH) && method === 'PUT') {
      current = { ...current, key_set: true }
      return json(200, { env_var: 'TYPESAFE_API_KEY', present: true })
    }
    if (path.startsWith(KEY_PATH) && method === 'DELETE') {
      current = { ...current, key_set: false }
      return json(200, { env_var: 'TYPESAFE_API_KEY', present: false, removed: true })
    }
    if (path.startsWith('/api/keys') && method === 'GET') {
      return json(200, {
        keys: [{ provider: 'jev', env_var: 'TYPESAFE_API_KEY', required: false, present: current.key_set }],
      })
    }
    return json(404, { error: 'not found' })
  }))
  return calls
}

function renderPanel(operatorMode = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <MemoryPromotionPanel agentId="olivia" operatorMode={operatorMode} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const toggle = () =>
  (screen.queryByRole('switch', { name: /enable memory sharing/i }) ??
    screen.getByRole('checkbox', { name: /enable memory sharing/i })) as HTMLInputElement | HTMLButtonElement

const isOn = (el: HTMLElement) =>
  el instanceof HTMLInputElement ? el.checked : el.getAttribute('aria-checked') === 'true'

const isDisabled = (el: HTMLElement) =>
  (el as HTMLInputElement | HTMLButtonElement).disabled || el.getAttribute('aria-disabled') === 'true'

const threshold = () => screen.getByLabelText(/confidence threshold/i) as HTMLInputElement
const model = () => screen.getByLabelText(/classifier model/i) as HTMLInputElement
const keyField = () => screen.getByLabelText(/jev api key/i) as HTMLInputElement
const saveSettings = () => screen.getByRole('button', { name: /save settings/i }) as HTMLButtonElement
const saveKey = () => screen.getByRole('button', { name: /save key/i }) as HTMLButtonElement

async function loaded() {
  await screen.findByRole('heading', { name: /memory sharing/i })
  await waitFor(() => expect(model().value).not.toBe(''))
}

describe('MemoryPromotionPanel', () => {
  it('renders the current settings from the server', async () => {
    stubServer({ settings: personal({ enabled: true, confidence_threshold: 0.97, classifier_model: 'jev-1.14' }) })
    renderPanel()
    await loaded()

    expect(isOn(toggle())).toBe(true)
    expect(Number(threshold().value)).toBe(0.97)
    expect(model().value).toBe('jev-1.14')
  })

  it('saves changed settings to the promotion route and nowhere else', async () => {
    const calls = stubServer()
    renderPanel()
    await loaded()

    await userEvent.click(toggle())
    await userEvent.clear(threshold())
    await userEvent.type(threshold(), '0.96')
    await userEvent.click(saveSettings())

    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const puts = calls.filter((c) => c.method === 'PUT')
    expect(puts).toHaveLength(1)
    expect(puts[0].path).toBe(SETTINGS_PATH)
    expect(puts[0].body).toMatchObject({ enabled: true, confidence_threshold: 0.96 })
    expect(typeof (puts[0].body as Settings).confidence_threshold).toBe('number')
  })

  it('never puts a key field into the settings request', async () => {
    const calls = stubServer()
    renderPanel()
    await loaded()

    await userEvent.type(keyField(), SENTINEL)
    await userEvent.click(toggle())
    await userEvent.click(saveSettings())

    await waitFor(() => expect(calls.some((c) => c.path === SETTINGS_PATH && c.method === 'PUT')).toBe(true))
    const settingsPut = calls.find((c) => c.path === SETTINGS_PATH && c.method === 'PUT')!
    expect(settingsPut.rawBody).not.toContain(SENTINEL)
    for (const k of Object.keys(settingsPut.body as object)) {
      expect(['enabled', 'confidence_threshold', 'classifier_model']).toContain(k)
    }
  })

  it('shows the server refusal and keeps the form editable', async () => {
    stubServer({ putStatus: 422, putError: 'confidence_threshold must be at least 0.90' })
    renderPanel()
    await loaded()

    await userEvent.clear(threshold())
    await userEvent.type(threshold(), '0.85')
    await userEvent.click(saveSettings())

    expect(await screen.findByText(/at least 0\.90/)).toBeTruthy()
    expect(isDisabled(threshold())).toBe(false)
  })

  it('shows "Key not set" when no key is stored', async () => {
    stubServer()
    renderPanel()
    await loaded()
    expect(screen.getByText('Key not set')).toBeTruthy()
    expect(screen.queryByText('Key set')).toBeNull()
  })

  it('shows "Key set" without any value when a key is stored', async () => {
    stubServer({ settings: personal({ key_set: true }) })
    renderPanel()
    await loaded()

    expect(screen.getByText('Key set')).toBeTruthy()
    expect(keyField().value).toBe('')
    expect(keyField().type).toBe('password')
  })

  it('sends the key only to /api/keys, then shows "Key set" and clears the field', async () => {
    const calls = stubServer()
    renderPanel()
    await loaded()

    await userEvent.type(keyField(), SENTINEL)
    await userEvent.click(saveKey())

    expect(await screen.findByText('Key set')).toBeTruthy()
    const carrying = calls.filter((c) => c.rawBody.includes(SENTINEL) || c.path.includes(SENTINEL))
    expect(carrying).toHaveLength(1)
    expect(carrying[0].method).toBe('PUT')
    expect(carrying[0].path).toBe(KEY_PATH)
    expect(carrying[0].body).toEqual({ value: SENTINEL })
    expect(keyField().value).toBe('')
    expect(document.body.textContent ?? '').not.toContain(SENTINEL)
  })

  it('is disabled with a federal-tier reason and cannot write', async () => {
    const calls = stubServer({ settings: personal({ tier: 'federal', federal_locked: true }) })
    renderPanel()
    await loaded()

    expect(screen.getByText(/federal/i)).toBeTruthy()
    expect(isDisabled(toggle())).toBe(true)
    expect(isDisabled(threshold())).toBe(true)
    expect(isDisabled(model())).toBe(true)
    expect(isDisabled(saveSettings())).toBe(true)

    await userEvent.click(saveSettings())
    expect(calls.filter((c) => c.method !== 'GET')).toHaveLength(0)
  })

  it('is read-only for a viewer', async () => {
    const calls = stubServer()
    renderPanel(false)
    await loaded()

    expect(isDisabled(toggle())).toBe(true)
    const saveSettings = screen.queryByRole('button', { name: /save settings/i })
    expect(saveSettings === null || isDisabled(saveSettings)).toBe(true)
    expect(screen.queryByLabelText(/jev api key/i)).toBeNull()
    expect(calls.filter((c) => c.method !== 'GET')).toHaveLength(0)
  })
})
