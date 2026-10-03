import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { PageHeader } from '@/components/page-header'

vi.mock('@/components/shell/operator-avatar-menu', () => ({
  OperatorAvatarMenu: () => <button type="button">Avatar</button>,
}))

afterEach(cleanup)

function show() {
  return render(
    <MemoryRouter initialEntries={['/agents']}>
      <PageHeader title="Fleet" description="All agents" actions={<button type="button">New agent</button>} />
    </MemoryRouter>,
  )
}

it('wraps the title block and the action cluster instead of overflowing at 375px', () => {
  const { container } = show()
  const bar = container.firstElementChild as HTMLElement
  expect(bar.className).toContain('flex-wrap')
  expect(bar.className).toContain('px-4')
  const actions = screen.getByText('New agent').parentElement as HTMLElement
  expect(actions.className).toContain('flex-wrap')
  // The cluster must be allowed to shrink/wrap rather than being pinned wide.
  expect(actions.className).not.toContain('shrink-0')
})

it('gives the title block the first line of room', () => {
  show()
  const title = screen.getByRole('heading', { name: 'Fleet' }).parentElement as HTMLElement
  expect(title.className).toContain('min-w-0')
  expect(title.className).toContain('flex-1')
})
