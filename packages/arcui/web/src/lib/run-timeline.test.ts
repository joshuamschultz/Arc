import { describe, expect, it } from 'vitest'
import { describeAction, type RunItem } from './run-timeline'

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
