import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'

afterEach(cleanup)

const NAMES = ['Overview', 'Identity', 'Inbox', 'Workspace', 'Files', 'Connect']

function bar(value: string, listClass?: string) {
  return (
    <Tabs value={value}>
      <TabsList className={listClass}>
        {NAMES.map((n) => <TabsTrigger key={n} value={n}>{n}</TabsTrigger>)}
      </TabsList>
    </Tabs>
  )
}

it('lays every tab out in one scrollable row, never wrapping', () => {
  render(bar('Overview', 'my-2 h-auto flex-wrap'))
  const list = screen.getByRole('tablist')
  expect(list.className).not.toMatch(/(^|\s)flex-wrap(\s|$)/)
  expect(list.className).toContain('flex-nowrap')
  const scroller = list.parentElement as HTMLElement
  expect(scroller.className).toContain('overflow-x-auto')
  expect(scroller.className).toContain('snap-x')
  expect(screen.getByRole('tab', { name: 'Connect' }).className).toContain('shrink-0')
})

it('scrolls the active tab into view when it changes', async () => {
  const scrollIntoView = vi.fn()
  Element.prototype.scrollIntoView = scrollIntoView
  const { rerender } = render(bar('Overview'))
  scrollIntoView.mockClear()
  rerender(bar('Connect'))
  await waitFor(() => expect(scrollIntoView).toHaveBeenCalled())
  expect(scrollIntoView.mock.contexts.at(-1)).toBe(screen.getByRole('tab', { name: 'Connect' }))
})
