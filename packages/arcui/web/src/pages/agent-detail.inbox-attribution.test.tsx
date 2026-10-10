// SPEC-087 T-1285 (REQ-570, COMP-009): the inbox thread shows who mail was sent
// for. The thread JSON carries `on_behalf_of` (person DID) and `via` (app DID)
// per message; the view renders 'from <person> via <app>', or just
// 'from <person>' when no app was involved. The signed transport sender (the
// operator mail identity) is unchanged and still shown as the sender.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { InboxTab } from '@/pages/agent-detail'

const PERSON = 'did:arc:default:user/ada-0f3c9e'
const APP = 'did:arc:default:app/reportbot-7a21d4'
const THREAD = {
  thread_id: 't1',
  subject: 'Q3 close',
  updated_at: '2026-10-01T00:00:00Z',
  unread_count: 0,
  participants: [{ participant_id: 'did:arc:olivia', role: 'agent' }],
}
const sender = { participant_id: 'did:arc:op', role: 'human' }

function stubThread(messages: Array<Record<string, unknown>>) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL) => {
      const path = String(request)
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
      if (path.endsWith('/inbox/t1')) return json({ messages, handoffs: [] })
      if (path.includes('/inbox')) return json({ threads: [THREAD] })
      if (path.includes('/tasks')) return json({ tasks: [] })
      if (path.includes('/approvals')) return json({ approvals: [] })
      return json({ agents: [] })
    }),
  )
}

function message(body: string, extra: Record<string, unknown>) {
  return { message_id: `m-${body}`, thread_id: 't1', body, created_at: '2026-10-01T00:00:00Z', sender, ...extra }
}

async function openThread() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TooltipProvider>
          <InboxTab agentId="olivia" />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  fireEvent.click(await screen.findByText('Q3 close'))
}

/** The message card containing `body`: the attribution line lives inside it. */
async function cardFor(body: string): Promise<HTMLElement> {
  const text = await screen.findByText(body)
  const card = text.parentElement
  if (!card) throw new Error(`no card for ${body}`)
  return card
}

/** A DID is shown by name/label; the full DID must still be reachable (text or tooltip). */
function shows(el: Element, did: string): boolean {
  const titles = [el, ...Array.from(el.querySelectorAll('[title]'))].map((node) => node.getAttribute('title') ?? '')
  return [el.textContent ?? '', ...titles].some((value) => value.includes(did))
}

/** The one element in the card whose own text reads 'from …'. */
function attributionLine(card: HTMLElement): HTMLElement {
  const lines = Array.from(card.querySelectorAll<HTMLElement>('*')).filter((el) =>
    /^\s*from\s+\S/i.test(el.textContent ?? '') && !Array.from(el.children).some((child) =>
      /^\s*from\s+\S/i.test(child.textContent ?? ''),
    ),
  )
  expect(lines).toHaveLength(1)
  return lines[0]
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('InboxTab thread attribution', () => {
  it("renders 'from <person> via <app>' when both are present", async () => {
    stubThread([message('Please own the Q3 close.', { on_behalf_of: PERSON, via: APP })])
    await openThread()

    const line = attributionLine(await cardFor('Please own the Q3 close.'))
    const text = (line.textContent ?? '').replace(/\s+/g, ' ').trim()

    expect(text).toMatch(/^from \S.* via \S.*$/)
    expect(shows(line, PERSON)).toBe(true)
    expect(shows(line, APP)).toBe(true)
    // Person before app: 'from <person> via <app>', never the reverse.
    const [personPart, appPart] = text.replace(/^from /, '').split(' via ')
    expect(PERSON.startsWith(personPart.replace(/…$/, '')) || personPart.includes(PERSON)).toBe(true)
    expect(APP.startsWith(appPart.replace(/…$/, '')) || appPart.includes(APP)).toBe(true)
  })

  it("renders just 'from <person>' when via is absent", async () => {
    stubThread([message('Signed-in note.', { on_behalf_of: PERSON, via: null })])
    await openThread()

    const line = attributionLine(await cardFor('Signed-in note.'))
    const text = (line.textContent ?? '').replace(/\s+/g, ' ').trim()

    expect(text).toMatch(/^from \S/)
    expect(text).not.toMatch(/\bvia\b/)
    expect(shows(line, PERSON)).toBe(true)
  })

  it('renders no attribution when the mail names no person', async () => {
    stubThread([message('Operator token note.', { on_behalf_of: null, via: null })])
    await openThread()

    const card = await cardFor('Operator token note.')

    expect(card.textContent ?? '').not.toMatch(/\bfrom\s+\S/i)
    expect(card.textContent ?? '').not.toMatch(/\bvia\b/)
  })
})
