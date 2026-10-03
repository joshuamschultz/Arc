// The Guides tab: one card per connection the agent is granted, holding the signed
// navigation guide (all kinds) and, for a database, what its tables mean.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { GuidesSection } from '@/components/connection-guides'

const operator = vi.hoisted(() => ({ on: true }))
vi.mock('@/hooks/use-operator-mode', () => ({ useOperatorMode: () => [operator.on, vi.fn()] }))

const LAYER = `classification = "internal"

[table.invoices]
entity = "invoice"
description = ""
hidden = false

[table.invoices.column.amt]
label = "amt"
description = ""
samples = ["19.99"]
`

interface World {
  guides: Record<string, { content: string; signed: boolean; signer: string | null; updated_at: string | null; version: number; tampered: boolean }>
  layer: string
  history: Array<{ version: number; signer: string; updated_at: string; digest: string }>
  starter: string
  calls: Array<{ method: string; path: string; body: unknown }>
}

let world: World

function connection(instance: string, extension: string, display = '') {
  return { instance, extension, extension_display_name: display, agents: ['olivia'] }
}

function freshWorld(): World {
  return {
    guides: {
      drive: { content: '# Drive\nFiles live in folders.', signed: true, signer: 'did:arc:operator', updated_at: '2026-10-01', version: 2, tampered: false },
      salesdb: { content: '', signed: false, signer: null, updated_at: null, version: 0, tampered: false },
    },
    layer: LAYER,
    history: [
      { version: 2, signer: 'did:arc:operator', updated_at: '2026-10-01', digest: 'b' },
      { version: 1, signer: 'did:arc:operator', updated_at: '2026-09-28', digest: 'a' },
    ],
    starter: '# Sales database\n\nWhat each table means.',
    calls: [],
  }
}

function stubServer() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const method = (init?.method ?? 'GET').toUpperCase()
      const body = init?.body ? JSON.parse(String(init.body)) : undefined
      world.calls.push({ method, path, body })
      const json = (value: unknown) =>
        new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })
      if (path === '/api/agents/olivia/connectors')
        return json({
          instances: [connection('drive', 'google_drive', 'Google Drive'), connection('salesdb', 'postgres_database', 'Sales database')],
          extensions_roots: [],
        })
      if (path === '/api/agents/olivia/knowledge/connected-sources') return json({ items: [] })
      const match = /^\/api\/connections\/([^/]+)\/(guide|semantic-layer)(\/\w+)?$/.exec(path)
      if (!match) return json({})
      const [, instance, kind, verb] = match
      if (kind === 'semantic-layer') {
        if (method === 'PUT') {
          world.layer = (body as { content: string }).content
          return json({ connection_id: instance, signer_did: 'did:arc:operator', sha256: 'x', message: 'saved' })
        }
        return json({ connection_id: instance, exists: true, content: world.layer, classification: 'internal', signed: true })
      }
      if (verb === '/history') return json({ versions: world.history })
      if (verb === '/starter') return json({ content: world.starter })
      if (verb === '/restore') {
        const target = (body as { version: number }).version
        world.guides[instance] = { ...world.guides[instance], content: `# Restored v${target}`, version: 3, signed: true, tampered: false }
        return json(world.guides[instance])
      }
      if (method === 'PUT') {
        const g = world.guides[instance]
        world.guides[instance] = { ...g, content: (body as { content: string }).content, signed: true, signer: 'did:arc:operator', version: g.version + 1, tampered: false }
        return json(world.guides[instance])
      }
      return json(world.guides[instance])
    }),
  )
}

function renderGuides() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <GuidesSection agentId="olivia" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const card = (instance: string) => screen.findByTestId(`guide-card-${instance}`)
const sent = (method: string, suffix: string) =>
  world.calls.filter((c) => c.method === method && c.path.endsWith(suffix))

beforeEach(() => {
  operator.on = true
  world = freshWorld()
  stubServer()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('Guides tab: the card', () => {
  it('shows one card per granted connection with its display name and kind', async () => {
    renderGuides()
    const drive = await card('drive')
    expect(within(drive).getByText('Google Drive (drive)')).toBeTruthy()
    expect(within(drive).getByText('google drive')).toBeTruthy()
    const db = await card('salesdb')
    expect(within(db).getByText('Sales database (salesdb)')).toBeTruthy()
    expect(within(drive).getByText(/agents read this to find their way around this source\. only signed guides are used\./i)).toBeTruthy()
  })

  it('loads the guide into the editor and says who signed it', async () => {
    renderGuides()
    const drive = await card('drive')
    const editor = await within(drive).findByRole('textbox', { name: 'Navigation guide' })
    expect((editor as HTMLTextAreaElement).value).toBe('# Drive\nFiles live in folders.')
    expect(within(drive).getByText(/signed by did:arc:operator/i)).toBeTruthy()
  })

  it('edits and saves, signing through the guide route', async () => {
    renderGuides()
    const drive = await card('drive')
    const editor = await within(drive).findByRole('textbox', { name: 'Navigation guide' })
    const save = within(drive).getByRole('button', { name: /save and sign/i }) as HTMLButtonElement
    expect(save.disabled).toBe(true) // nothing changed yet

    fireEvent.change(editor, { target: { value: '# Drive\nStart in Shared.' } })
    fireEvent.click(save)

    await waitFor(() => expect(sent('PUT', '/api/connections/drive/guide')).toHaveLength(1))
    expect(sent('PUT', '/api/connections/drive/guide')[0].body).toEqual({ content: '# Drive\nStart in Shared.' })
    await waitFor(() => expect((within(drive).getByRole('textbox', { name: 'Navigation guide' }) as HTMLTextAreaElement).value).toBe('# Drive\nStart in Shared.'))
  })

  it('counts against the 16 KB limit and blocks a save over it', async () => {
    renderGuides()
    const drive = await card('drive')
    const editor = await within(drive).findByRole('textbox', { name: 'Navigation guide' })
    expect(within(drive).getByText(/0\.0 KB of 16 KB/)).toBeTruthy()

    fireEvent.change(editor, { target: { value: 'x'.repeat(17 * 1024) } })

    expect(within(drive).getByText(/17\.0 KB of 16 KB/)).toBeTruthy()
    expect((within(drive).getByRole('button', { name: /save and sign/i }) as HTMLButtonElement).disabled).toBe(true)
    expect(within(drive).getByText(/over 16 KB/i)).toBeTruthy()
  })

  it('shows a red notice when the guide was changed outside Arc', async () => {
    world.guides.drive = { ...world.guides.drive, tampered: true }
    renderGuides()
    const drive = await card('drive')
    const notice = await within(drive).findByRole('alert')
    expect(notice.textContent).toBe('This guide was changed outside Arc and is not being used. Save it again to re-sign.')
    expect(notice.className).toContain('destructive')
    expect(within(drive).queryByText(/signed by/i)).toBeNull()
  })

  it('fills an empty editor from the starter draft', async () => {
    renderGuides()
    const db = await card('salesdb')
    expect(within(await card('drive')).queryByRole('button', { name: /start from a draft/i })).toBeNull()

    fireEvent.click(await within(db).findByRole('button', { name: /start from a draft/i }))

    await waitFor(() =>
      expect((within(db).getByRole('textbox', { name: 'Navigation guide' }) as HTMLTextAreaElement).value).toBe(world.starter),
    )
    expect(within(db).queryByRole('button', { name: /start from a draft/i })).toBeNull()
    expect(sent('GET', '/api/connections/salesdb/guide/starter')).toHaveLength(1)
  })

  it('lists history and restores an older version', async () => {
    renderGuides()
    const drive = await card('drive')
    fireEvent.click(await within(drive).findByRole('button', { name: /history/i }))
    expect(await within(drive).findByText(/version 1/i)).toBeTruthy()

    fireEvent.click(within(drive).getByRole('button', { name: 'Restore version 1' }))

    await waitFor(() => expect(sent('POST', '/api/connections/drive/guide/restore')).toHaveLength(1))
    expect(sent('POST', '/api/connections/drive/guide/restore')[0].body).toEqual({ version: 1 })
    await waitFor(() => expect((within(drive).getByRole('textbox', { name: 'Navigation guide' }) as HTMLTextAreaElement).value).toBe('# Restored v1'))
  })

  it('previews the markdown', async () => {
    renderGuides()
    const drive = await card('drive')
    await within(drive).findByRole('textbox', { name: 'Navigation guide' })
    fireEvent.click(within(drive).getByRole('button', { name: /preview/i }))
    expect((await within(drive).findByTestId('guide-preview')).querySelector('h1')?.textContent).toBe('Drive')
  })

  it('hides Save and Restore from a viewer', async () => {
    operator.on = false
    renderGuides()
    const drive = await card('drive')
    await within(drive).findByRole('textbox', { name: 'Navigation guide' })
    expect(within(drive).queryByRole('button', { name: /save and sign/i })).toBeNull()
    fireEvent.click(within(drive).getByRole('button', { name: /history/i }))
    await within(drive).findByText(/version 1/i)
    expect(within(drive).queryByRole('button', { name: /restore version/i })).toBeNull()
  })

  it('stacks and wraps at 375 px', async () => {
    renderGuides()
    const drive = await card('drive')
    expect(drive.className).toContain('min-w-0')
    expect(drive.className).toContain('max-w-full')
    const editor = await within(drive).findByRole('textbox', { name: 'Navigation guide' })
    expect(editor.className).toContain('w-full')
    expect(within(drive).getByRole('button', { name: /history/i }).parentElement?.className).toContain('flex-wrap')
  })
})

describe('Guides tab: table meanings (databases only)', () => {
  it('appears for a database and not for a file source', async () => {
    renderGuides()
    expect(within(await card('salesdb')).getByText('Table meanings')).toBeTruthy()
    expect(within(await card('drive')).queryByText('Table meanings')).toBeNull()
  })

  it('edits a table and a column and saves the text back, keeping everything else', async () => {
    renderGuides()
    const db = await card('salesdb')
    const name = await within(db).findByRole('textbox', { name: 'Display name for table invoices' })
    expect((name as HTMLInputElement).value).toBe('invoice')

    fireEvent.change(name, { target: { value: 'sales invoice' } })
    fireEvent.change(within(db).getByRole('textbox', { name: 'Description for table invoices' }), { target: { value: 'One row per bill we send' } })
    fireEvent.click(within(db).getByRole('checkbox', { name: 'Hide table invoices' }))
    fireEvent.change(within(db).getByRole('textbox', { name: 'Display name for column invoices.amt' }), { target: { value: 'Amount' } })
    fireEvent.change(within(db).getByRole('textbox', { name: 'Description for column invoices.amt' }), { target: { value: 'US dollars' } })
    fireEvent.click(within(db).getByRole('button', { name: /save table meanings/i }))

    await waitFor(() => expect(sent('PUT', '/api/connections/salesdb/semantic-layer')).toHaveLength(1))
    const saved = (sent('PUT', '/api/connections/salesdb/semantic-layer')[0].body as { content: string }).content
    expect(saved).toContain('entity = "sales invoice"')
    expect(saved).toContain('description = "One row per bill we send"')
    expect(saved).toContain('hidden = true')
    expect(saved).toContain('label = "Amount"')
    expect(saved).toContain('description = "US dollars"')
    expect(saved).toContain('samples = ["19.99"]')
    expect(saved).toContain('classification = "internal"')
    // Round trip: the reloaded layer shows what was saved.
    await waitFor(() =>
      expect((within(db).getByRole('textbox', { name: 'Display name for table invoices' }) as HTMLInputElement).value).toBe('sales invoice'),
    )
  })

  it('offers the same text as raw TOML, and the two views stay in step', async () => {
    renderGuides()
    const db = await card('salesdb')
    await within(db).findByRole('textbox', { name: 'Display name for table invoices' })

    fireEvent.click(within(db).getByRole('checkbox', { name: /edit as raw toml/i }))
    const raw = (await within(db).findByRole('textbox', { name: 'Table meanings as TOML' })) as HTMLTextAreaElement
    expect(raw.value).toBe(LAYER)

    fireEvent.change(raw, { target: { value: LAYER.replace('entity = "invoice"', 'entity = "bill"') } })
    fireEvent.click(within(db).getByRole('checkbox', { name: /edit as raw toml/i }))

    expect((await within(db).findByRole('textbox', { name: 'Display name for table invoices' }) as HTMLInputElement).value).toBe('bill')
  })

  it('fits a phone: fields fill the width and the stacked grid has one column', async () => {
    renderGuides()
    const db = await card('salesdb')
    const name = await within(db).findByRole('textbox', { name: 'Display name for table invoices' })
    expect(name.className).toContain('w-full')
    expect(name.parentElement?.className).toContain('grid-cols-1')
  })
})
