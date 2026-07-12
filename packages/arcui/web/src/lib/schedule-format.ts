import type { Dict } from '@/lib/types'

// Human-readable rendering for scheduler `ScheduleEntry` dicts. Kept out of the
// drawer component so both the Schedules table and the detail drawer share one
// source of truth (and so the component file only exports components).

const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

function ordinal(n: number): string {
  const s = ['th', 'st', 'nd', 'rd']
  const v = n % 100
  return `${n}${s[(v - 20) % 10] ?? s[v] ?? s[0]}`
}

/** Turn a 5-field cron expression into plain English for the common cases,
 *  falling back to the raw expression for anything unrecognized. */
export function cronToProse(expr: string): string {
  const parts = expr.trim().split(/\s+/)
  if (parts.length !== 5) return expr
  const [min, hour, dom, mon, dow] = parts
  const raw = () => `Cron: ${expr}`

  // Every N minutes / hours.
  const stepMin = /^\*\/(\d+)$/.exec(min)
  if (stepMin && hour === '*' && dom === '*' && mon === '*' && dow === '*') {
    return `Every ${stepMin[1]} minutes`
  }
  const stepHour = /^\*\/(\d+)$/.exec(hour)
  if (stepHour && dom === '*' && mon === '*' && dow === '*') {
    const m = /^\d+$/.test(min) ? Number(min) : 0
    return `Every ${stepHour[1]} hours at :${pad2(m)}`
  }

  // Fixed time-of-day variants require numeric minute + hour.
  if (!/^\d+$/.test(min) || !/^\d+$/.test(hour)) return raw()
  const at = `${pad2(Number(hour))}:${pad2(Number(min))}`

  if (dom === '*' && mon === '*' && dow === '*') return `Daily at ${at}`
  if (/^[0-6]$/.test(dow) && dom === '*' && mon === '*') {
    return `Weekly on ${DAYS[Number(dow)]} at ${at}`
  }
  if (dow === '7' && dom === '*' && mon === '*') return `Weekly on ${DAYS[0]} at ${at}`
  if (/^\d+$/.test(dom) && mon === '*' && dow === '*') {
    return `Monthly on the ${ordinal(Number(dom))} at ${at}`
  }
  return raw()
}

/** Turn an interval in seconds into "Every N minutes/hours/days". */
export function humanizeInterval(seconds: number): string {
  const unit = (n: number, label: string) => `Every ${n} ${label}${n === 1 ? '' : 's'}`
  if (seconds % 86400 === 0) return unit(seconds / 86400, 'day')
  if (seconds % 3600 === 0) return unit(seconds / 3600, 'hour')
  if (seconds % 60 === 0) return unit(seconds / 60, 'minute')
  return unit(seconds, 'second')
}

function formatAt(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

/** Plain-English timing for a schedule — cron prose, interval cadence, or a
 *  formatted one-time datetime. Shared by the table column and the drawer. */
export function scheduleTiming(schedule: Dict): string {
  const type = String(schedule.type ?? '')
  if (type === 'cron' && schedule.expression) return cronToProse(String(schedule.expression))
  if (type === 'interval' && schedule.every_seconds != null) {
    return humanizeInterval(Number(schedule.every_seconds))
  }
  if (type === 'once' && schedule.at) return `Once, ${formatAt(String(schedule.at))}`
  return '—'
}

/** A readable title for a schedule — the first line of its prompt (what it
 *  does), never the opaque `sched_xxxx` id. Falls back to the id if empty. */
export function scheduleTitle(schedule: Dict): string {
  const prompt = String(schedule.prompt ?? '').trim()
  const firstLine = prompt.split('\n')[0]?.replace(/^#+\s*/, '').trim() ?? ''
  if (firstLine) return firstLine.length > 70 ? `${firstLine.slice(0, 69)}…` : firstLine
  return String(schedule.id ?? 'schedule')
}
