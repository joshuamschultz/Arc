// SPEC-083 T-1222 (COMP-028; REQ-509, REQ-510) — the "Memory sharing" panel.
//
// Contract assumed by these tests:
//   import { MemoryPromotionPanel } from '@/components/memory-promotion-panel'
//   <MemoryPromotionPanel agentId="olivia" operatorMode={true|false} />
//
//   GET  /api/agents/{id}/memory/promotion
//        -> { enabled, confidence_threshold, classifier_model, tier, federal_locked, key_set }
//   PUT  /api/agents/{id}/memory/promotion   body: settings only, never a key
//   GET  /api/classifiers/jev/models         -> { classifier, models: string[] }
//   The key is NOT edited here (Settings -> Keys owns it): the panel shows a badge + link only.
//
// Accessible names pinned:
//   heading "Memory sharing"; toggle (switch or checkbox) /enable memory sharing/i;
//   input /confidence threshold/i; combobox /classifier model/i (models from the server,
//   plus "Other…" which reveals a free-text /pinned model version/i input);
//   button /save settings/i; key badge text "Key set" / "Key not set"; link to Settings.
//
// The server is stubbed at the network boundary (global fetch), as in
// remote-sign-in-panel.test.tsx, so the panel is free to use any lib/queries hook.
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { MemoryPromotionPanel } from '@/components/memory-promotion-panel'

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

const SETTINGS_PATH = '/api/agents/olivia/memory/promotion'
const MODELS_PATH = '/api/classifiers/jev/models'
const RUN_PATH = '/api/agents/olivia/memory/promotion/run'
const RUN_RESULT = {
  status: 'completed',
  evaluated: 12,
  promoted: 3,
  kept_private: 7,
  blocked_secret: 1,
  too_large: 1,
  deferred: 40,
}

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
  classifier_model: 'jev-1.13.0',
  tier: 'personal',
  federal_locked: false,
  key_set: false,
  ...over,
})

function stubServer({
  settings = personal(),
  putStatus = 200,
  putError = 'confidence_threshold must be at least 0.90',
  runStatus = 200,
  runBody = RUN_RESULT as unknown,
  runGate,
}: {
  settings?: Settings
  putStatus?: number
  putError?: string
  runStatus?: number
  runBody?: unknown
  runGate?: Promise<void>
} = {}) {
  const calls: Call[] = []
  let current = { ...settings }
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
    const path = String(request)
    const method = init?.method ?? 'GET'
    const rawBody = init?.body ? String(init.body) : ''
    calls.push({ path, method, rawBody, body: rawBody ? JSON.parse(rawBody) : undefined })
    const json = (status: number, body: unknown) =>
      new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

    // T-1224 "Run now" — matched before SETTINGS_PATH, which is its prefix.
    if (path.startsWith(RUN_PATH)) {
      if (method !== 'POST') return json(405, { error: 'method not allowed' })
      if (runGate) await runGate
      return json(runStatus, runBody)
    }
    if (path.startsWith(SETTINGS_PATH) && method === 'GET') return json(200, current)
    if (path.startsWith(SETTINGS_PATH) && method === 'PUT') {
      if (putStatus !== 200) return json(putStatus, { error: putError })
      current = { ...current, ...(JSON.parse(rawBody) as Partial<Settings>) }
      return json(200, current)
    }
    if (path.startsWith(MODELS_PATH) && method === 'GET') {
      return json(200, { classifier: 'jev', models: ['jev-1.13.0', 'jev-1.14.0'] })
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
const model = () => screen.getByRole('combobox', { name: /classifier model/i }) as HTMLButtonElement
const customModel = () => screen.getByLabelText(/pinned model version/i) as HTMLInputElement
const saveSettings = () => screen.getByRole('button', { name: /save settings/i }) as HTMLButtonElement

async function loaded() {
  await screen.findByRole('heading', { name: /memory sharing/i })
  await screen.findByRole('combobox', { name: /classifier model/i })
}

describe('MemoryPromotionPanel', () => {
  it('renders the current settings from the server', async () => {
    stubServer({ settings: personal({ enabled: true, confidence_threshold: 0.97, classifier_model: 'jev-1.14.0' }) })
    renderPanel()
    await loaded()

    expect(isOn(toggle())).toBe(true)
    expect(Number(threshold().value)).toBe(0.97)
    expect(model().textContent).toContain('jev-1.14.0')
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

  it('has no key input: the key is edited in Settings -> Keys', async () => {
    const calls = stubServer()
    renderPanel()
    await loaded()

    expect(screen.queryByLabelText(/jev api key/i)).toBeNull()
    expect(screen.queryByRole('button', { name: /save key/i })).toBeNull()
    const link = screen.getByRole('link', { name: /settings.*keys/i })
    expect(link.getAttribute('href')).toBe('/settings')
    expect(calls.filter((c) => c.path.startsWith('/api/keys') && c.method !== 'GET')).toHaveLength(0)
  })

  it('offers the models from the server in a dropdown, with Other…', async () => {
    const calls = stubServer()
    renderPanel()
    await loaded()

    expect(calls.some((c) => c.path === MODELS_PATH)).toBe(true)
    expect(model().textContent).toContain('jev-1.13.0')
    await userEvent.click(model())
    expect(await screen.findByRole('option', { name: 'jev-1.14.0' })).toBeTruthy()
    expect(screen.getByRole('option', { name: /other/i })).toBeTruthy()
  })

  it('saves a model picked from the dropdown', async () => {
    const calls = stubServer()
    renderPanel()
    await loaded()

    await userEvent.click(model())
    await userEvent.click(await screen.findByRole('option', { name: 'jev-1.14.0' }))
    await userEvent.click(saveSettings())

    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body).toMatchObject({
      classifier_model: 'jev-1.14.0',
    })
  })

  it('keeps an unlisted pinned version editable as free text via Other…', async () => {
    const calls = stubServer({ settings: personal({ classifier_model: 'jev-9.9.9' }) })
    renderPanel()
    await loaded()

    expect(customModel().value).toBe('jev-9.9.9')
    await userEvent.clear(customModel())
    await userEvent.type(customModel(), 'jev-2.0.0')
    await userEvent.click(saveSettings())

    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body).toMatchObject({
      classifier_model: 'jev-2.0.0',
    })
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

  it('shows "Key set" when a key is stored', async () => {
    stubServer({ settings: personal({ key_set: true }) })
    renderPanel()
    await loaded()
    expect(screen.getByText('Key set')).toBeTruthy()
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
    expect(calls.filter((c) => c.method !== 'GET')).toHaveLength(0)
  })
})

// SPEC-083 T-1224 (COMP-029, REQ-512) — "Run now" on the Memory sharing panel.
//
// Contract assumed:
//   button /run now/i — POST /api/agents/{id}/memory/promotion/run  body {} (no cap from the panel)
//     -> { status, evaluated, promoted, kept_private, blocked_secret, too_large, deferred }
//   Enabled only for an operator, when sharing is ON in the SAVED settings and the
//   agent is not federal-locked. Disabled while a run is in flight (no double-send).
//   After a run the panel shows the last result: its status and counts.
//   A refusal (e.g. 503 "agent is not running") is shown and the button re-enables.
describe('MemoryPromotionPanel — Run now', () => {
  const runNow = () => screen.getByRole('button', { name: /run now/i }) as HTMLButtonElement
  const runCalls = (calls: Call[]) => calls.filter((c) => c.path.startsWith(RUN_PATH))
  const text = () => document.body.textContent ?? ''

  it('runs promotion on the running agent and shows the last result counts', async () => {
    const calls = stubServer({ settings: personal({ enabled: true, key_set: true }) })
    renderPanel()
    await loaded()

    expect(isDisabled(runNow())).toBe(false)
    await userEvent.click(runNow())

    await waitFor(() => expect(text()).toMatch(/evaluated\D{0,40}12\b/i))
    expect(text()).toMatch(/promoted\D{0,40}3\b/i)
    expect(text()).toMatch(/kept private\D{0,40}7\b/i)
    expect(text()).toMatch(/deferred\D{0,40}40\b/i)
    expect(text()).toMatch(/completed/i)

    const runs = runCalls(calls)
    expect(runs).toHaveLength(1)
    expect(runs[0].method).toBe('POST')
    expect(runs[0].body ?? {}).toEqual({})
    // Running never writes settings or keys.
    expect(calls.filter((c) => c.method !== 'GET' && !c.path.startsWith(RUN_PATH))).toHaveLength(0)
  })

  it('is disabled when sharing is off and cannot start a run', async () => {
    const calls = stubServer({ settings: personal({ enabled: false }) })
    renderPanel()
    await loaded()

    expect(isDisabled(runNow())).toBe(true)
    await userEvent.click(runNow())
    expect(runCalls(calls)).toHaveLength(0)
  })

  it('stays disabled when sharing is switched on but not yet saved', async () => {
    const calls = stubServer({ settings: personal({ enabled: false }) })
    renderPanel()
    await loaded()

    await userEvent.click(toggle())
    expect(isOn(toggle())).toBe(true)

    expect(isDisabled(runNow())).toBe(true)
    await userEvent.click(runNow())
    expect(runCalls(calls)).toHaveLength(0)
  })

  it('is disabled on a federal agent even if the file says enabled', async () => {
    const calls = stubServer({
      settings: personal({ enabled: true, tier: 'federal', federal_locked: true }),
    })
    renderPanel()
    await loaded()

    expect(isDisabled(runNow())).toBe(true)
    await userEvent.click(runNow())
    expect(runCalls(calls)).toHaveLength(0)
  })

  it('cannot be triggered by a viewer', async () => {
    const calls = stubServer({ settings: personal({ enabled: true }) })
    renderPanel(false)
    await loaded()

    const button = screen.queryByRole('button', { name: /run now/i })
    expect(button === null || isDisabled(button)).toBe(true)
    if (button) await userEvent.click(button)
    expect(runCalls(calls)).toHaveLength(0)
  })

  it('sends one request while a run is in flight (no double-send)', async () => {
    let release: () => void = () => {}
    const runGate = new Promise<void>((resolve) => {
      release = resolve
    })
    const calls = stubServer({ settings: personal({ enabled: true }), runGate })
    renderPanel()
    await loaded()

    await userEvent.click(runNow())
    await waitFor(() => expect(isDisabled(runNow())).toBe(true))
    await userEvent.click(runNow())
    expect(runCalls(calls)).toHaveLength(1)

    release()
    await waitFor(() => expect(text()).toMatch(/evaluated\D{0,40}12\b/i))
    expect(runCalls(calls)).toHaveLength(1)
    await waitFor(() => expect(isDisabled(runNow())).toBe(false))
  })

  it('shows the server refusal and re-enables the button', async () => {
    stubServer({
      settings: personal({ enabled: true }),
      runStatus: 503,
      runBody: { error: 'agent is not running' },
    })
    renderPanel()
    await loaded()

    await userEvent.click(runNow())

    expect(await screen.findByText(/agent is not running/i)).toBeTruthy()
    await waitFor(() => expect(isDisabled(runNow())).toBe(false))
    expect(text()).not.toMatch(/evaluated\D{0,40}12\b/i)
  })
})
