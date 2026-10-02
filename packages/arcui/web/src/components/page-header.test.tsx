import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PageHeader } from './page-header'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('renders Help and the operator avatar side by side in one flow, not a fixed overlay', () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}')))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={['/connections']}>
      <QueryClientProvider client={client}>
        <PageHeader title="Connections" />
      </QueryClientProvider>
    </MemoryRouter>,
  )
  const avatar = screen.getByRole('button', { name: 'Operator menu' })
  const help = screen.getByRole('button', { name: /^Help for/ })
  const actions = help.parentElement as HTMLElement
  expect(actions.contains(avatar)).toBe(true)
  expect(help.compareDocumentPosition(avatar) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  for (let el: HTMLElement | null = avatar; el; el = el.parentElement) {
    expect(el.className).not.toMatch(/\b(fixed|absolute)\b/)
  }
})
