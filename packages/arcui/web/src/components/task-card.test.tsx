import { afterEach, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { TaskCard } from '@/components/task-card'
import type { Task } from '@/lib/types'

afterEach(cleanup)

it('reports the exact dependency count when the preview is clipped', () => {
  const task = { id: 'a', title: 'Blocked', status: 'todo', priority: 'medium',
    blocked_by: Array.from({ length: 10 }, (_, i) => `d${i}`), blocked_by_total: 20 } as Task
  render(<TaskCard task={task} ownerLabel={null} blocked onClick={() => {}} />)
  expect(screen.getByText('blocked 20')).toBeTruthy()
})
