// Item 8 — the Knowledge overview no longer dumps the raw summary payload.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { KnowledgeOverview } from '@/components/knowledge-view/overview'
import type { KnowledgeResponse } from '@/lib/queries'

vi.mock('@/components/file-tree', () => ({ FileTree: () => <div>file tree</div> }))

afterEach(cleanup)

describe('KnowledgeOverview', () => {
  it('has no "Raw summary payload" block', () => {
    const data = { agent_id: 'olivia', memory: { episodic: 3 } } as unknown as KnowledgeResponse
    render(<KnowledgeOverview data={data} agentId="olivia" onNavigate={() => {}} />)

    expect(screen.getByText('Knowledge base')).toBeTruthy()
    expect(screen.queryByText(/raw summary payload/i)).toBeNull()
  })
})
