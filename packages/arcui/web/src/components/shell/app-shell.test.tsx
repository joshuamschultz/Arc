import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { AppShell } from '@/components/shell/app-shell'

vi.mock('@/components/approval-notification-listener', () => ({ ApprovalNotificationListener: () => null }))
vi.mock('@/components/command-palette', () => ({ CommandPalette: () => null }))
vi.mock('@/components/custody-repair-panel', () => ({ CustodyBanner: () => null }))

afterEach(cleanup)

function showShell() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={['/home']}>
          <Routes>
            <Route element={<AppShell />}>
              <Route path="home" element={<p>Home body</p>} />
              <Route path="agents" element={<p>Agents body</p>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

it('keeps the mobile menu button in a top bar that only shows below md', () => {
  showShell()
  const button = screen.getByRole('button', { name: 'Open navigation menu' })
  expect(button.closest('header')?.className).toContain('md:hidden')
  expect(button.className).toContain('min-h-11')
})

it('opens the nav drawer, and closes it on Escape', async () => {
  showShell()
  expect(screen.queryByRole('dialog')).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: 'Open navigation menu' }))
  const drawer = screen.getByRole('dialog')
  expect(within(drawer).getByRole('link', { name: 'Fleet' })).toBeTruthy()
  await userEvent.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).toBeNull()
})

it('closes the drawer after a navigation link is chosen', async () => {
  showShell()
  await userEvent.click(screen.getByRole('button', { name: 'Open navigation menu' }))
  await userEvent.click(within(screen.getByRole('dialog')).getByRole('link', { name: 'Fleet' }))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.getByText('Agents body')).toBeTruthy()
})

it('badges the Needs you nav item with everything waiting on the operator', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL) => {
      const body =
        String(request) === '/api/home/needs' ? { total: 3 } : { connections: [], extensions_roots: [] }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
  showShell()
  const badges = await screen.findAllByTestId('needs-you-badge')
  expect(badges[0].textContent).toBe('3')
  vi.unstubAllGlobals()
})

it('hides the desktop rail below md and never fixes a width past the viewport', () => {
  const { container } = showShell()
  const rail = container.querySelector('nav[aria-label="Primary"]')
  expect(rail?.className).toContain('max-md:hidden')
  const fixed = [...container.querySelectorAll<HTMLElement>('[class*="w-["]')]
    .flatMap((el) => [...el.className.matchAll(/(?<![:\w-])w-\[(\d+)px\]/g)].map((m) => Number(m[1])))
  // Unprefixed fixed widths apply at 375px, so none may exceed it.
  expect(fixed.filter((w) => w > 375)).toEqual([])
})
