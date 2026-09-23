import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { PageHeader } from '@/components/page-header'
import { CreateTaskSheet } from '@/components/create-task-sheet'
import { FieldHelp } from '@/components/help'
import content from '@/content/screen-help.json'

afterEach(cleanup)

describe('contextual help', () => {
  for (const [route, entry] of Object.entries(content)) {
    const path = route.replace(':id', 'example')
    it(`opens ${route} guidance from the page header`, async () => {
      render(<MemoryRouter initialEntries={[`/${path}`]}><PageHeader title={entry.title} /></MemoryRouter>)
      await userEvent.click(screen.getByRole('button', { name: `Help for ${entry.title}` }))
      expect(screen.getByText(entry.summary)).toBeTruthy()
      await userEvent.keyboard('{Escape}')
      expect(screen.queryByText(entry.summary)).toBeNull()
      expect(document.activeElement).toBe(screen.getByRole('button', { name: `Help for ${entry.title}` }))
    })
  }

  it('opens field help without toggling a neighboring checkbox', async () => {
    render(<MemoryRouter><label><input type="checkbox" aria-label="Require review" />Require review</label><FieldHelp helpKey="task.requires_review" route="tasks" /></MemoryRouter>)
    await userEvent.click(screen.getByRole('button', { name: 'Help for Require review' }))
    expect((screen.getByRole('checkbox', { name: 'Require review' }) as HTMLInputElement).checked).toBe(false)
    expect(screen.getByText(content.tasks.fields.find((field) => field.key === 'task.requires_review')!.description)).toBeTruthy()
  })

  it('exposes task form help while preserving a typed draft', async () => {
    const queryClient = new QueryClient()
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter><CreateTaskSheet open onOpenChange={() => {}} roster={[]} /></MemoryRouter>
      </QueryClientProvider>,
    )
    const title = screen.getByRole('textbox', { name: 'Title' }) as HTMLInputElement
    await userEvent.type(title, 'Investigate the outage')
    await userEvent.click(screen.getByRole('button', { name: 'Help for Title' }))
    expect(title.value).toBe('Investigate the outage')
    expect(screen.getByText(content.tasks.fields.find((field) => field.key === 'task.title')!.description)).toBeTruthy()
  })
})
