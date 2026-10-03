import { afterEach, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { SearchInput } from '@/components/ui/search-input'

afterEach(cleanup)

it('keeps the icon inside the input box, vertically centred', () => {
  render(<SearchInput aria-label="Find" helpKey="shared_knowledge.search" helpRoute="shared-knowledge" />)
  const input = screen.getByLabelText('Find')
  const box = input.parentElement as HTMLElement
  expect(box.className).toContain('relative')
  const icon = box.querySelector('svg') as SVGElement
  expect(icon.getAttribute('class')).toContain('inset-y-0')
  expect(icon.getAttribute('class')).toContain('my-auto')
  expect(input.className).toContain('pl-8')
})

it('places the help icon beside the box, not inside it or under it', () => {
  render(<SearchInput aria-label="Find" helpKey="shared_knowledge.search" helpRoute="shared-knowledge" />)
  const input = screen.getByLabelText('Find')
  const row = input.parentElement?.parentElement as HTMLElement
  expect(row.className).toContain('flex')
  expect(row.className).not.toContain('flex-col')
  const help = screen.getByRole('button', { name: /Help for/ })
  expect(input.parentElement?.contains(help)).toBe(false)
  expect(row.contains(help)).toBe(true)
})
