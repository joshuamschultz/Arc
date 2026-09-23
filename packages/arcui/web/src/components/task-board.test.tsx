import { act } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { TaskBoard } from '@/components/task-board'
import type { Task } from '@/lib/types'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

it('updates the seven-day window after idle while keeping unresolved work visible', () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-09-23T12:00:00Z'))
  const tasks = [
    { id: 'open', title: 'Old active task', status: 'in_progress', priority: 'medium', updated_at: '2020-01-01T00:00:00Z' },
    { id: 'done', title: 'Recently done', status: 'done', priority: 'medium', updated_at: '2026-09-17T12:00:00Z' },
  ] as Task[]
  render(<TaskBoard tasks={tasks} resolveOwner={() => null} onSelectTask={() => {}} />)
  act(() => { screen.getByRole('button', { name: '7 days' }).click() })
  expect(screen.getByRole('button', { name: /Old active task/ })).toBeTruthy()
  expect(screen.getByRole('button', { name: /Recently done/ })).toBeTruthy()
  act(() => {
    vi.setSystemTime(new Date('2026-09-25T12:00:00Z'))
    vi.advanceTimersByTime(60_000)
  })
  expect(screen.getByRole('button', { name: /Old active task/ })).toBeTruthy()
  expect(screen.queryByRole('button', { name: /Recently done/ })).toBeNull()
})
