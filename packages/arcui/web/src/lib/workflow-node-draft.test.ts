import { expect, it } from 'vitest'
import { fromDraft, toDraft } from '@/lib/workflow-node-draft'
import type { WorkflowNode } from '@/lib/types'

const base = { id: 'a', kind: 'agent', needs: ['x', 'y'] } as WorkflowNode

it('on_failure defaults to fail_run and is omitted from the node', () => {
  const draft = toDraft(base)
  expect(draft.onFailure).toBe('fail_run')
  expect('on_failure' in fromDraft(draft)).toBe(false)
})

it('on_failure round-trips a non-default value', () => {
  const draft = toDraft({ ...base, on_failure: 'skip_dependents' })
  expect(draft.onFailure).toBe('skip_dependents')
  expect(fromDraft(draft).on_failure).toBe('skip_dependents')
})

it('an unknown on_failure falls back to fail_run', () => {
  const draft = toDraft({ ...base, on_failure: 'bogus' } as unknown as WorkflowNode)
  expect(draft.onFailure).toBe('fail_run')
})

it('no longer emits loop or join fields', () => {
  const node = fromDraft(toDraft(base))
  expect('join' in node || 'loop_back_to' in node || 'max_iterations' in node).toBe(false)
})
