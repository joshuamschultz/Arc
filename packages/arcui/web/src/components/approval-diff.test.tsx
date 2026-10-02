import { afterEach, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ApprovalDiff } from '@/components/approval-diff'

afterEach(cleanup)

it('renders node groups and per-file diffs', () => {
  render(
    <ApprovalDiff
      diff={{
        nodes: { added: ['triage'], removed: ['old'], changed: ['send'] },
        files: [{ path: 'prompts/send.md', status: 'modified', diff: '-hi\n+hello' }],
      }}
    />,
  )
  expect(screen.getByText('What changes')).toBeTruthy()
  expect(screen.getByText('triage')).toBeTruthy()
  expect(screen.getByText('old')).toBeTruthy()
  expect(screen.getByText('send')).toBeTruthy()
  expect(screen.getByText(/prompts\/send\.md/)).toBeTruthy()
  expect(screen.getByText(/\+hello/)).toBeTruthy()
})

it('renders nothing for an empty diff', () => {
  const { container } = render(<ApprovalDiff diff={{ nodes: {}, files: [] }} />)
  expect(container.textContent).toBe('')
})
