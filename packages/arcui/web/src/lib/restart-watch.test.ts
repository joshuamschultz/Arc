import { describe, expect, it } from 'vitest'
import { waitForArcBack } from '@/lib/restart-watch'

const fast = { intervalMs: 1, graceMs: 30, maxMs: 500 }

describe('waitForArcBack', () => {
  it('waits through the outage and resolves once Arc answers again', async () => {
    const answers = [true, false, false, true]
    const probe = async () => answers.shift() ?? true
    expect(await waitForArcBack(fast, undefined, probe)).toBe(true)
    expect(answers).toEqual([])
  })

  it('does not believe an answer from before the restart began', async () => {
    let calls = 0
    const probe = async () => {
      calls += 1
      return true
    }
    // Arc never goes down here, so it is only believed after the grace period.
    expect(await waitForArcBack({ ...fast, graceMs: 40 }, undefined, probe)).toBe(true)
    expect(calls).toBeGreaterThan(3)
  })

  it('gives up when Arc never comes back', async () => {
    const probe = async () => false
    expect(await waitForArcBack({ intervalMs: 1, graceMs: 5, maxMs: 30 }, undefined, probe)).toBe(false)
  })

  it('stops when the caller aborts', async () => {
    const controller = new AbortController()
    controller.abort()
    const probe = async () => true
    expect(await waitForArcBack(fast, controller.signal, probe)).toBe(false)
  })
})
