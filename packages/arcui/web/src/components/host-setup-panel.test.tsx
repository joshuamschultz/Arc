// J1-3 — the host-setup panel finishes the journey without a terminal.
//
// The server is stubbed at the network boundary (global fetch). Pinned:
//   - the install button posts to the bundle's host-setup route;
//   - while it runs the panel shows what Arc is doing;
//   - a failure is one plain sentence, never shell text or a "someone with a
//     terminal" hand-off;
//   - a missing Node.js is named in plain words with a link, not a command.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { HostSetupPanel } from '@/components/host-setup-panel'
import type { HostRequirement } from '@/lib/types'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const requirements = (names: string[]): HostRequirement[] =>
  names.map((name) => ({
    name,
    instruction: `npm install -g ${name}`,
    satisfied: false,
  }))

function stubHostSetup(body: Record<string, unknown>) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPanel(names: string[]) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <HostSetupPanel
        extension="readwise_reader"
        requirements={requirements(names)}
        operatorMode
        blocking
      />
    </QueryClientProvider>,
  )
}

describe('HostSetupPanel', () => {
  it('installs from the button and shows a plain receipt', async () => {
    const fetchMock = stubHostSetup({
      installed: true,
      detail: 'Installed readwise.',
      manual_steps: 'npm install -g @readwise/cli',
    })
    renderPanel(['readwise'])

    await userEvent.click(screen.getByRole('button', { name: /install on this computer/i }))

    await waitFor(() => expect(screen.getByText(/this computer is ready/i)).toBeTruthy())
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toContain('/api/connections/readwise_reader/host-setup')
    expect(init.method).toBe('POST')
  })

  it('shows a refusal in plain words and never a command to copy', async () => {
    stubHostSetup({
      installed: false,
      detail: 'readwise downloaded from the registry is not the approved build — nothing was installed',
      manual_steps: 'npm pack @readwise/cli@0.5.9\nnpm install -g ./cli.tgz',
    })
    renderPanel(['readwise'])

    await userEvent.click(screen.getByRole('button', { name: /install on this computer/i }))

    await waitFor(() => expect(screen.getByText(/not the approved build/i)).toBeTruthy())
    expect(screen.queryByText(/npm /)).toBeNull()
    expect(screen.queryByText(/terminal/i)).toBeNull()
    expect(screen.queryByText(/do it myself/i)).toBeNull()
  })

  it('says in plain words that Node.js is missing, with a link and no command', () => {
    renderPanel(['node', 'readwise'])

    expect(screen.getByText(/runs on node\.js/i)).toBeTruthy()
    const link = screen.getByRole('link', { name: /nodejs\.org/i }) as HTMLAnchorElement
    expect(link.href).toContain('nodejs.org')
    expect(screen.queryByText(/npm /)).toBeNull()
  })

  it('shows what Arc is doing while the install runs', async () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})))
    renderPanel(['readwise'])

    await userEvent.click(screen.getByRole('button', { name: /install on this computer/i }))

    await waitFor(() => expect(screen.getByRole('status')).toBeTruthy())
    expect(screen.getByText(/checking it is exactly what was approved/i)).toBeTruthy()
  })
})
