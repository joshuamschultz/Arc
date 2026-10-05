import { describe, expect, it } from 'vitest'
import { describeAction, mergeTimeline, type RunItem, type ToolItem } from './run-timeline'

describe('describeAction for strategy selection', () => {
  it('says which strategy the selection call chose', () => {
    const item: RunItem = {
      kind: 'run',
      name: 'strategy.selection.complete',
      extra: { selected: 'react' },
    }
    expect(describeAction(item).description).toBe('Selected: react')
  })

  it('falls back to a plain description without a name', () => {
    const item: RunItem = { kind: 'run', name: 'strategy.selection.complete' }
    expect(describeAction(item).description).toBe('Chose a strategy')
  })
})

describe('a tool result that was saved whole instead of shown', () => {
  const start = { kind: 'tool_event', phase: 'start', tool_name: 'read', ts: 't1', extra: {} }
  const end = (extra: Record<string, unknown>) => ({
    kind: 'tool_event',
    phase: 'end',
    tool_name: 'read',
    outcome: 'ok',
    ts: 't2',
    extra,
  })

  it('says the full output was saved, with its size, and never says truncated', () => {
    const [item] = mergeTimeline(
      [start, end({ spilled: true, spill_tokens: 30000, spill_handle: 'spill_ab' })] as never,
      false,
    ) as ToolItem[]
    expect(item.spilledTokens).toBe(30000)
    const { description } = describeAction(item)
    expect(description).toContain('saved full output (30000 tokens)')
    expect(description.toLowerCase()).not.toContain('truncated')
  })

  it('adds nothing for a result that was shown whole', () => {
    const [item] = mergeTimeline([start, end({})] as never, false) as ToolItem[]
    expect(item.spilledTokens).toBeNull()
    expect(describeAction(item).description).not.toContain('saved full output')
  })
})
