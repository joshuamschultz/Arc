import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConnectorBundleSheet } from './connector-bundle-upload'
import { ActionPrompt } from './action-prompt'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const baseReview = {
  name: 'acme',
  display_name: 'Acme CRM',
  version: '1.2.0',
  description: 'Reads and updates Acme records.',
  attachment: 'native',
  tier_floor: 'personal',
  publisher: { status: 'unknown', signer_did: '' },
  tools: [
    {
      name: 'acme_search',
      description: 'Search records',
      classification: 'read_only',
      capability_tags: ['crm'],
      network: true,
    },
  ],
  secrets: [{ name: 'ACME_TOKEN', prompt: 'Your Acme API token', sensitive: true, required: true }],
  host_programs: [],
  egress_hosts: ['api.acme.test'],
  skills: [],
  files: [
    { path: 'extension.toml', size: 512, executes: false },
    { path: 'code/main.py', size: 2048, executes: true },
  ],
  executes_code: true,
  needs_network: true,
  flags: ['This package runs code on your computer.'],
  digest: 'd'.repeat(64),
  confirm_required: true,
  update: null,
}

function staged(over: Record<string, unknown> = {}) {
  return { staging_id: 's1', expires_in: 600, review: { ...baseReview, ...over } }
}

type Call = { url: string; init?: RequestInit }

function stubFetch(handler: (url: string, init?: RequestInit) => Response | undefined): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: RequestInfo | URL, init?: RequestInit) => {
      const url = String(path)
      calls.push({ url, init })
      const made = handler(url, init)
      if (made) return made
      if (url === '/api/connector-bundles') {
        return new Response(JSON.stringify({ installed: [], unsigned_local: [] }))
      }
      return new Response('{}')
    }),
  )
  return calls
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

function mount(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

async function pickFile() {
  const input = (await screen.findByLabelText('Connector package file')) as HTMLInputElement
  const file = new File(['zip'], 'acme.zip', { type: 'application/zip' })
  await userEvent.upload(input, file)
}

it('uploads a file as field "file" and shows the review', async () => {
  const calls = stubFetch((url) =>
    url === '/api/connector-bundles/upload' ? json(staged()) : undefined,
  )
  mount(<ConnectorBundleSheet open onOpenChange={() => {}} />)
  await pickFile()

  expect(await screen.findByText('Acme CRM')).toBeTruthy()
  const post = calls.find((c) => c.url === '/api/connector-bundles/upload')!
  expect(post.init?.method).toBe('POST')
  expect((post.init?.body as FormData).get('file')).toBeInstanceOf(File)
  expect(screen.getByText('acme_search')).toBeTruthy()
  expect(screen.getByText('network')).toBeTruthy()
  expect(screen.getByText('ACME_TOKEN')).toBeTruthy()
  expect(screen.getByText('This package runs code on your computer.')).toBeTruthy()
  expect(screen.getByText('code/main.py')).toBeTruthy()
  expect(screen.getByText('2.0 KB')).toBeTruthy()
  expect(screen.getByText('runs code')).toBeTruthy()
  expect(screen.getByText('Unknown publisher')).toBeTruthy()
})

it('keeps Approve disabled until the typed name matches, then posts confirm_name', async () => {
  const calls = stubFetch((url) => {
    if (url === '/api/connector-bundles/upload') return json(staged())
    if (url === '/api/connector-bundles/s1/approve') {
      return json({
        installed: { name: 'acme', display_name: 'Acme CRM', version: '1.2.0', signer_did: 'did:x', used_by: [] },
      })
    }
    return undefined
  })
  mount(<ConnectorBundleSheet open onOpenChange={() => {}} />)
  await pickFile()

  const approve = (await screen.findByRole('button', { name: 'Approve and sign' })) as HTMLButtonElement
  expect(approve.disabled).toBe(true)
  const input = screen.getByLabelText('Type acme to approve')
  await userEvent.type(input, 'acm')
  expect(approve.disabled).toBe(true)
  await userEvent.type(input, 'e')
  expect(approve.disabled).toBe(false)
  await userEvent.click(approve)

  expect(await screen.findByText(/Installed\. Find Acme CRM under Available/)).toBeTruthy()
  const post = calls.find((c) => c.url === '/api/connector-bundles/s1/approve')!
  expect(JSON.parse(String(post.init?.body))).toEqual({ confirm_name: 'acme' })
})

it('verified publisher shows the badge and needs no typing', async () => {
  stubFetch((url) =>
    url === '/api/connector-bundles/upload'
      ? json(
          staged({
            publisher: { status: 'verified', signer_did: 'did:arc:pub:1' },
            confirm_required: false,
          }),
        )
      : undefined,
  )
  mount(<ConnectorBundleSheet open onOpenChange={() => {}} />)
  await pickFile()

  expect(await screen.findByText('Verified publisher')).toBeTruthy()
  expect(screen.getByText(/did:arc:pub:1/)).toBeTruthy()
  expect(screen.queryByLabelText(/Type acme/)).toBeNull()
  expect((screen.getByRole('button', { name: 'Approve and sign' }) as HTMLButtonElement).disabled).toBe(false)
})

it('renders the update diff', async () => {
  stubFetch((url) =>
    url === '/api/connector-bundles/upload'
      ? json(
          staged({
            update: {
              installed_version: '1.0.0',
              tools_added: ['acme_new'],
              tools_removed: [],
              tools_changed: ['acme_search'],
              new_secrets: [],
              new_egress: ['evil.example.test'],
            },
          }),
        )
      : undefined,
  )
  mount(<ConnectorBundleSheet open onOpenChange={() => {}} />)
  await pickFile()

  expect(await screen.findByText('Update from 1.0.0 to 1.2.0')).toBeTruthy()
  expect(screen.getByText(/Tools changed:/).parentElement?.textContent).toContain('acme_search')
  expect(screen.getByText(/New network hosts:/).parentElement?.textContent).toContain('evil.example.test')
  expect(screen.getByText(/Changed tools stop working until you approve them/)).toBeTruthy()
})

it('shows the server refusal text', async () => {
  stubFetch((url) =>
    url === '/api/connector-bundles/upload'
      ? json({ error: 'That package has no manifest.', reason: 'no_manifest' }, 400)
      : undefined,
  )
  mount(<ConnectorBundleSheet open onOpenChange={() => {}} />)
  await pickFile()
  expect(await screen.findByText('That package has no manifest.')).toBeTruthy()
})

it('shows which connections block a remove', async () => {
  stubFetch((url, init) => {
    if (url === '/api/connector-bundles' && !init?.method) {
      return json({
        installed: [
          { name: 'acme', display_name: 'Acme CRM', version: '1.2.0', signer_did: 'did:x', used_by: ['acme-main'] },
        ],
        unsigned_local: [],
      })
    }
    if (init?.method === 'DELETE') {
      return json({ error: 'This package is in use.', reason: 'in_use', used_by: ['acme-main', 'acme-two'] }, 409)
    }
    return undefined
  })
  mount(<ConnectorBundleSheet open onOpenChange={() => {}} />)
  await userEvent.click(await screen.findByRole('button', { name: 'Remove' }))
  await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('acme-main, acme-two'))
})

it('stages a local package straight into the review when given localName', async () => {
  const calls = stubFetch((url) =>
    url === '/api/connector-bundles/stage-local' ? json(staged()) : undefined,
  )
  mount(<ConnectorBundleSheet open onOpenChange={() => {}} localName="acme" />)
  expect(await screen.findByText('Acme CRM')).toBeTruthy()
  const post = calls.find((c) => c.url === '/api/connector-bundles/stage-local')!
  expect(JSON.parse(String(post.init?.body))).toEqual({ name: 'acme' })
})

it('the sign_bundle prompt offers Review and sign, which opens the sheet', async () => {
  stubFetch(() => undefined)
  mount(<ActionPrompt code="sign_bundle" />)
  expect(screen.getByText(/Arc will not run it until you review and sign it/)).toBeTruthy()
  await userEvent.click(screen.getByRole('button', { name: 'Review and sign' }))
  expect(await screen.findByText('Add connector package')).toBeTruthy()
})
