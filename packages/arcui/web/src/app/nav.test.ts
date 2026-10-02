import { describe, expect, it } from 'vitest'
import { NAV_ITEMS } from '@/app/nav'

describe('NAV_ITEMS', () => {
  it('files Call queue under advanced, not watch', () => {
    const queue = NAV_ITEMS.find((i) => i.path === 'queue')
    expect(queue?.group).toBe('advanced')
  })
})
