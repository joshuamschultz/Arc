/**
 * A canonical, lowercase UUIDv4.
 *
 * `crypto.randomUUID` exists only in a secure context (https or localhost). A
 * dashboard reached over plain http on a LAN or tailnet address has no
 * `randomUUID`, so calling it throws and the action silently never happens.
 * `crypto.getRandomValues` is available in every context, so the fallback
 * builds the same RFC 4122 version-4 shape from it.
 */
export function newRequestId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
