import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { CapabilityImportPanel } from './capability-import-panel'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

const review = {
  import_id: 'a'.repeat(64),
  status: 'review_ready',
  target_agent_did: 'did:arc:agent:olivia',
  archive_sha256: 'b'.repeat(64),
  review_digest: 'c'.repeat(64),
  tools: [],
  skills: ['create-skill'],
  findings: [
    'builtin_name_collision: create-skill',
    'missing_section: skills/create-skill/SKILL.md: missing ## Contract',
  ],
  files: [
    { path: 'skills/create-skill/SKILL.md', sha256: 'd'.repeat(64), size: 10 },
    { path: 'skills/create-skill/references/guide.md', sha256: 'e'.repeat(64), size: 5 },
    { path: 'skills/create-skill/scripts/run.py', sha256: 'f'.repeat(64), size: 7 },
  ],
  supplier_metadata_keys: [],
  activation: 'review_only',
}

type Call = { url: string; init?: RequestInit }

function stubFetch(imports: unknown[]): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: RequestInfo | URL, init?: RequestInit) => {
      const url = String(path)
      calls.push({ url, init })
      if (url.startsWith('/api/team/roster')) {
        return new Response(JSON.stringify({ agents: [{ agent_id: 'olivia', name: 'olivia' }] }))
      }
      if (init?.method === 'POST') return new Response(JSON.stringify(review), { status: 201 })
      return new Response(JSON.stringify({ imports }))
    }),
  )
  return calls
}

function mount() {
  localStorage.setItem('arcui_operator_mode', '1')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <CapabilityImportPanel />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

async function chooseAgent() {
  const select = await screen.findByLabelText(/Target agent/)
  await waitFor(() => expect(screen.getByRole('option', { name: 'olivia' })).toBeTruthy())
  await userEvent.selectOptions(select, 'olivia')
}

it('renders review findings before the promote action', async () => {
  stubFetch([review])
  mount()
  await chooseAgent()

  const findings = await screen.findByRole('list', { name: /review findings/i })
  expect(findings.textContent).toContain('builtin_name_collision')
  expect(findings.textContent).toContain('create-skill')
  expect(findings.textContent).toContain('missing_section')
  const promote = screen.getByRole('button', { name: /promote and sign/i })
  // The findings sit above the action that would sign past them.
  expect(findings.compareDocumentPosition(promote) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
})

it('shows the reviewed files as a tree with script badges', async () => {
  stubFetch([review])
  mount()
  await chooseAgent()

  const tree = await screen.findByRole('tree', { name: /reviewed files/i })
  expect(tree.textContent).toContain('references/')
  const script = screen.getByRole('button', { name: /run\.py/ })
  expect(script.textContent).toMatch(/script/i)
  expect(screen.getByRole('button', { name: /guide\.md/ }).textContent).not.toMatch(/script/i)
})

function fileEntry(name: string, fullPath: string, content: string) {
  return {
    isFile: true,
    isDirectory: false,
    name,
    fullPath,
    file: (resolve: (file: File) => void) => resolve(new File([content], name)),
  }
}

function dirEntry(name: string, fullPath: string, children: unknown[]) {
  return {
    isFile: false,
    isDirectory: true,
    name,
    fullPath,
    createReader: () => {
      const batches = [children, []]
      return { readEntries: (resolve: (entries: unknown[]) => void) => resolve(batches.shift() ?? []) }
    },
  }
}

it('zips a dropped skill folder and uploads it as one archive', async () => {
  const calls = stubFetch([])
  mount()
  await chooseAgent()

  const folder = dirEntry('pdf-tools', '/pdf-tools', [
    fileEntry('SKILL.md', '/pdf-tools/SKILL.md', '---\nname: pdf-tools\n---\n'),
    fileEntry('.DS_Store', '/pdf-tools/.DS_Store', 'junk'),
    dirEntry('references', '/pdf-tools/references', [
      fileEntry('guide.md', '/pdf-tools/references/guide.md', 'guide'),
    ]),
  ])
  const zone = screen.getByRole('button', { name: /upload a capability/i })
  fireEvent.drop(zone, {
    dataTransfer: {
      files: [],
      items: [{ kind: 'file', webkitGetAsEntry: () => folder }],
    },
  })

  await waitFor(() => expect(calls.some((call) => call.init?.method === 'POST')).toBe(true))
  const post = calls.find((call) => call.init?.method === 'POST')
  const uploaded = (post?.init?.body as FormData).get('file') as File
  expect(uploaded.name).toBe('pdf-tools.zip')
  const text = new TextDecoder().decode(new Uint8Array(await uploaded.arrayBuffer()))
  expect(text).toContain('pdf-tools/SKILL.md')
  expect(text).toContain('pdf-tools/references/guide.md')
  expect(text).not.toContain('.DS_Store')
})

it('offers a folder picker beside the ZIP picker', async () => {
  stubFetch([])
  mount()
  const picker = await screen.findByLabelText(/choose a skill folder/i)
  expect(picker.getAttribute('webkitdirectory')).not.toBeNull()
})
