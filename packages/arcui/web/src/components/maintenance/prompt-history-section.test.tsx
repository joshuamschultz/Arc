import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen } from '@testing-library/react'
import { PromptHistorySection } from '@/components/maintenance/prompt-history-section'
import { renderWithClient, stubApi } from '@/components/maintenance/test-helpers'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('PromptHistorySection', () => {
  it('links each agent to the Prompts tab that already holds history, diff and restore', async () => {
    stubApi({
      'GET /api/team/roster': {
        agents: [
          { agent_id: 'olivia', name: 'olivia', display_name: 'Olivia' },
          { agent_id: 'ghost', name: 'ghost', hidden: true },
        ],
      },
    })
    renderWithClient(<PromptHistorySection />)

    const link = await screen.findByRole('link', { name: 'Olivia' })
    expect(link.getAttribute('href')).toBe('/agents/olivia/prompts')
    expect(screen.queryByRole('link', { name: 'ghost' })).toBeNull()
    expect(screen.getByText(/choose a prompt, then History/i)).toBeTruthy()
  })
})
