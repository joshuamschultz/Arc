import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ScheduleStatus } from '@/components/workflows-view/schedule-status'
import type { WorkflowSchedule } from '@/lib/types'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const schedule = (extra: Partial<WorkflowSchedule>): WorkflowSchedule => ({
  agent_id: 'olivia',
  schedule_id: 'wf:nightly',
  enabled: false,
  ...extra,
})

const renderStatus = (s: WorkflowSchedule) =>
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ScheduleStatus workflowId="nightly" schedule={s} />
    </QueryClientProvider>,
  )

describe('ScheduleStatus', () => {
  it('names an unapproved legacy schedule and offers Approve', () => {
    renderStatus(schedule({ disabled_reason: 'unapproved' }))
    expect(screen.getByText('Needs approval — re-create or approve')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Approve' })).toBeTruthy()
  })

  it('Approve posts to the approve route, never the PATCH enable route', async () => {
    const fetchMock = vi.fn(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    renderStatus(schedule({ disabled_reason: 'unapproved' }))
    await userEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(String(url)).toContain('/api/agents/olivia/schedules/wf%3Anightly/approve')
    expect(init.method).toBe('POST')
  })

  it('an operator-disabled schedule still offers Re-enable', () => {
    renderStatus(schedule({ disabled_reason: 'operator' }))
    expect(screen.getByRole('button', { name: 'Re-enable' })).toBeTruthy()
  })
})
