import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FileTree } from '@/components/file-tree'
import { apiGet, apiGetText, apiPut } from '@/lib/api'

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  apiGet: vi.fn(),
  apiGetText: vi.fn(),
  apiPut: vi.fn(),
}))
vi.mock('@/hooks/use-operator-mode', () => ({ useOperatorMode: () => [true, vi.fn()] }))

const get = vi.mocked(apiGet)
const getText = vi.mocked(apiGetText)
const put = vi.mocked(apiPut)

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

function setup() {
  const sources: Record<string, string> = {
    'report.html': '<h1>Original</h1>',
    'other.html': '<h1>Other</h1>',
  }
  get.mockImplementation(async (path) => path.includes('/tree')
    ? { entries: ['report.html', 'other.html'].map((entry) => ({ path: entry, type: 'file', size: 20, mtime: 0 })) }
    : { path: path.includes('other.html') ? 'other.html' : 'report.html',
      content: sources[path.includes('other.html') ? 'other.html' : 'report.html'],
      content_type: 'text', mime: 'text/html', size: 20, mtime: 0 })
  put.mockImplementation(async (path, body) => {
    sources[path.includes('other.html') ? 'other.html' : 'report.html'] = (body as { content: string }).content
    return { message: 'Saved', signature_stale: false }
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><FileTree agentId="ada" /></QueryClientProvider>)
  return { client, setSource: (value: string) => { sources['report.html'] = value } }
}

async function selectReport() {
  await userEvent.click(await screen.findByRole('button', { name: 'report.html' }))
  await screen.findByRole('button', { name: 'View report' })
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('keeps the newest report when an aborted fetch resolves out of order', async () => {
  const first = deferred<string>()
  const second = deferred<string>()
  getText.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  setup()
  await selectReport()
  await userEvent.click(screen.getByRole('button', { name: 'View report' }))
  await userEvent.click(screen.getByRole('button', { name: 'View report' }))
  expect(getText.mock.calls[0]?.[1]?.aborted).toBe(true)
  second.resolve('<h1>Newest</h1>')
  await waitFor(() => expect(screen.getByTitle('Report preview: report.html').getAttribute('srcdoc')).toContain('Newest'))
  first.resolve('<h1>Stale</h1>')
  await waitFor(() => expect(screen.getByTitle('Report preview: report.html').getAttribute('srcdoc')).not.toContain('Stale'))
})

it('cancels preview on source toggle and fetches fresh content after save', async () => {
  const pending = deferred<string>()
  getText.mockReturnValueOnce(pending.promise).mockResolvedValueOnce('<h1>Latest preview</h1>')
  const { client } = setup()
  await selectReport()
  await userEvent.click(screen.getByRole('button', { name: 'View report' }))
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  expect(getText.mock.calls[0]?.[1]?.aborted).toBe(true)
  const draft = screen.getByRole('textbox') as HTMLTextAreaElement
  await userEvent.clear(draft)
  await userEvent.type(draft, '<h1>Latest source</h1>')
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(get.mock.calls.filter(([path]) => path.includes('/files/read')).length).toBeGreaterThan(1))
  await userEvent.click(screen.getByRole('button', { name: 'View report' }))
  await waitFor(() => expect(screen.getByTitle('Report preview: report.html').getAttribute('srcdoc')).toContain('Latest preview'))
  await userEvent.click(screen.getByRole('button', { name: 'Return to source' }))
  expect(screen.queryByTitle('Report preview: report.html')).toBeNull()
  expect(getText).toHaveBeenCalledTimes(2)
  pending.resolve('<h1>Old preview</h1>')
  client.clear()
})

it('resets an unsaved draft when selecting another file', async () => {
  setup()
  await selectReport()
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  await userEvent.clear(screen.getByRole('textbox'))
  await userEvent.type(screen.getByRole('textbox'), '<h1>Report draft</h1>')
  await userEvent.click(screen.getByRole('button', { name: 'other.html' }))
  await screen.findByRole('button', { name: 'Edit' })
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('<h1>Other</h1>')
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(put).toHaveBeenCalledWith(
    expect.stringContaining('path=other.html'), { content: '<h1>Other</h1>' },
  ))
})

it('keeps the new file untouched when an old save finishes after selection', async () => {
  const pending = deferred<{ message: string; signature_stale: boolean }>()
  setup()
  put.mockReturnValueOnce(pending.promise)
  await selectReport()
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  await userEvent.clear(screen.getByRole('textbox'))
  await userEvent.type(screen.getByRole('textbox'), '<h1>Old save</h1>')
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  await userEvent.click(screen.getByRole('button', { name: 'other.html' }))
  await screen.findByRole('button', { name: 'Edit' })
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  pending.resolve({ message: 'Saved old file', signature_stale: false })
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('<h1>Other</h1>')
  expect(screen.queryByText('Saved old file')).toBeNull()
  expect(put.mock.calls[0]?.[0]).toContain('path=report.html')
})
