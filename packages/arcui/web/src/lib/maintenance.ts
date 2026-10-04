import type { BlueprintCreates } from '@/lib/types'

/** Plain-words text for a failed Maintenance request. The server already words its errors. */
export const errorText = (e: unknown): string =>
  e instanceof Error ? e.message : 'Something went wrong.'

/** "Roll back to X" for an older install, "Switch to X" for a newer one. */
export const switchLabel = (relation: 'active' | 'older' | 'newer', version: string): string =>
  relation === 'older' ? `Roll back to ${version}` : `Switch to ${version}`

const count = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

/** The plain-words list of what an agent made from this blueprint starts with. */
export function describeCreates(c: BlueprintCreates): string {
  const parts: string[] = []
  if (c.persona) parts.push('a starting identity')
  if (c.prompts.length) parts.push(count(c.prompts.length, 'tuned prompt'))
  if (c.skills.length) parts.push(count(c.skills.length, 'skill'))
  if (c.capabilities.length) parts.push(count(c.capabilities.length, 'tool'))
  if (c.schedules) parts.push(count(c.schedules, 'scheduled task'))
  const list = parts.length ? parts.join(', ') : 'only its settings'
  return c.modules.length ? `${list}; turns on ${c.modules.join(', ')}` : list
}
