// Display formatters — TS port of the old `formatters.js`. Locale-aware,
// compact where it matters (tokens/cost) for dense telemetry tables.

const tokenFmt = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
})
const numberFmt = new Intl.NumberFormat('en-US')

/** Compact token counts: 1.2M, 45K. */
export function fmtTokens(n: number | null | undefined): string {
  if (n == null) return '—'
  return tokenFmt.format(n)
}

/** USD cost; 4 decimals under a cent, else 2. */
export function fmtCost(n: number | null | undefined): string {
  if (n == null) return '—'
  const decimals = n !== 0 && Math.abs(n) < 0.01 ? 4 : 2
  return `$${n.toFixed(decimals)}`
}

/** Latency in ms or s. */
export function fmtLatency(ms: number | null | undefined): string {
  if (ms == null) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

export function fmtNumber(n: number | null | undefined): string {
  if (n == null) return '—'
  return numberFmt.format(n)
}

export function fmtPercent(n: number | null | undefined): string {
  if (n == null) return '—'
  return `${(n * 100).toFixed(1)}%`
}

/** First 12 chars of a trace/span id. */
export function shortId(id: string | null | undefined, len = 12): string {
  if (!id) return '—'
  return id.length > len ? id.slice(0, len) : id
}

export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

/** "5m ago" from an ISO string or epoch (s or ms). */
export function relativeTime(ts: string | number | null | undefined): string {
  if (ts == null) return '—'
  let ms: number
  if (typeof ts === 'number') ms = ts < 1e12 ? ts * 1000 : ts
  else {
    const parsed = Date.parse(ts)
    if (Number.isNaN(parsed)) return String(ts)
    ms = parsed
  }
  const diff = Date.now() - ms
  const sec = Math.round(diff / 1000)
  if (sec < 5) return 'just now'
  if (sec < 60) return `${sec}s ago`
  const min = Math.round(sec / 60)
  if (min < 60) return `${min}m ago`
  const hr = Math.round(min / 60)
  if (hr < 24) return `${hr}h ago`
  const day = Math.round(hr / 24)
  return `${day}d ago`
}

/** Absolute local time for tooltips/detail. */
export function fmtTime(ts: string | number | null | undefined): string {
  if (ts == null) return '—'
  const ms =
    typeof ts === 'number' ? (ts < 1e12 ? ts * 1000 : ts) : Date.parse(ts)
  if (Number.isNaN(ms)) return String(ts)
  return new Date(ms).toLocaleString()
}

/** Two-letter initials for an agent avatar. */
export function initials(name: string | null | undefined): string {
  if (!name) return '??'
  const parts = name.trim().split(/[\s_-]+/).filter(Boolean)
  if (parts.length === 0) return '??'
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[1][0]).toUpperCase()
}

/**
 * XML pretty-printer (U11): tokenize into tags (`<…>`) and text runs, then
 * re-emit one node per line with depth-based indentation and a blank line
 * between sibling elements. Tolerant of XML-ish (not strictly well-formed)
 * content — unbalanced tags degrade to a flat list rather than throwing.
 */
export function formatXml(src: string): string {
  const tokens = src.match(/<[^>]+>|[^<]+/g) ?? []
  const lines: string[] = []
  let depth = 0
  const pad = (): string => '  '.repeat(Math.max(0, depth))
  for (const token of tokens) {
    if (token.startsWith('<')) {
      const isClose = token.startsWith('</')
      const isSelfContained =
        token.endsWith('/>') || token.startsWith('<?') || token.startsWith('<!')
      if (isClose) {
        depth = Math.max(0, depth - 1)
        lines.push(pad() + token.trim())
      } else if (isSelfContained) {
        lines.push(pad() + token.trim())
      } else {
        // A new element opening right after a sibling closed gets a blank line
        // so consecutive <skill>…</skill><skill>… reads as separated blocks.
        const prev = lines[lines.length - 1]?.trim() ?? ''
        if (/<\/[^>]+>$/.test(prev)) lines.push('')
        lines.push(pad() + token.trim())
        depth += 1
      }
    } else {
      const text = token.trim()
      if (text) lines.push(pad() + text)
    }
  }
  return lines.join('\n')
}
