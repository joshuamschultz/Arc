import type { PolicyBullet } from './types'

export type BulletSort = 'score' | 'uses' | 'created'

export function filterBullets(
  bullets: PolicyBullet[],
  { text, hideRetired }: { text: string; hideRetired: boolean },
): PolicyBullet[] {
  const q = text.trim().toLowerCase()
  return bullets.filter((b) => {
    if (hideRetired && b.retired) return false
    if (q && !(b.text || '').toLowerCase().includes(q)) return false
    return true
  })
}

export function sortBullets(bullets: PolicyBullet[], key: BulletSort): PolicyBullet[] {
  const get = (b: PolicyBullet): number => {
    if (key === 'created') return b.created ? Date.parse(b.created) : 0
    const v = b[key]
    return typeof v === 'number' ? v : 0
  }
  return [...bullets].sort((a, b) => get(b) - get(a))
}
