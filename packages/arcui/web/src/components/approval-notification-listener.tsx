import { useEffect, useState } from 'react'
import { Bell } from 'lucide-react'
import { apiGet, apiPost } from '@/lib/api'

const ENABLED_KEY = 'arcui_approval_notifications_enabled'
const SEEN_KEY = 'arcui_approval_notifications_seen'
const REFRESH_EVENT = 'arcui:approval-notifications-refresh'

type ApprovalEvent = {
  event_id: string
  approval_id: string
  status: string
  tool?: string | null
}

function readSeen(): Set<string> {
  try {
    const raw = JSON.parse(localStorage.getItem(SEEN_KEY) || '[]')
    return new Set(Array.isArray(raw) ? raw.filter((v): v is string => typeof v === 'string') : [])
  } catch {
    return new Set()
  }
}

function writeSeen(seen: Set<string>): void {
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify([...seen].slice(-256)))
  } catch {
    /* Notification delivery remains server-backed when storage is unavailable. */
  }
}

/**
 * App-wide approval notification consumer. It lives in AppShell so changing
 * routes cannot stop polling. Events are acknowledged on the server only
 * after Notification construction succeeds; localStorage is an additional
 * cross-tab replay guard, never the source of truth.
 */
export function ApprovalNotificationListener() {
  const [permission, setPermission] = useState<NotificationPermission | 'unsupported'>(() =>
    typeof Notification === 'undefined' ? 'unsupported' : Notification.permission,
  )
  const [enabled, setEnabled] = useState(() => {
    try {
      return localStorage.getItem(ENABLED_KEY) === '1'
    } catch {
      return false
    }
  })

  useEffect(() => {
    const refresh = () => {
      try {
        setEnabled(localStorage.getItem(ENABLED_KEY) === '1')
      } catch {
        /* ignore */
      }
    }
    window.addEventListener(REFRESH_EVENT, refresh)
    window.addEventListener('storage', refresh)
    return () => {
      window.removeEventListener(REFRESH_EVENT, refresh)
      window.removeEventListener('storage', refresh)
    }
  }, [])

  const enable = async () => {
    if (typeof Notification === 'undefined') return
    const next = await Notification.requestPermission()
    setPermission(next)
    if (next === 'granted') {
      try {
        localStorage.setItem(ENABLED_KEY, '1')
      } catch {
        /* permission is still valid for this tab */
      }
      setEnabled(true)
      window.dispatchEvent(new Event(REFRESH_EVENT))
    }
  }

  useEffect(() => {
    if (!enabled || permission !== 'granted') return
    let stopped = false
    const poll = async () => {
      let data: { events?: ApprovalEvent[] }
      try {
        data = await apiGet<{ events?: ApprovalEvent[] }>('/api/approvals/notifications')
      } catch {
        return
      }
      const seen = readSeen()
      for (const event of data.events ?? []) {
        if (stopped || seen.has(event.event_id)) continue
        try {
          const notice = new Notification(`Approval ${event.status}`, {
            body: event.tool ?? 'Agent approval required',
          })
          notice.onclick = () => {
            window.location.assign(`/approvals#${encodeURIComponent(event.approval_id)}`)
          }
          // Acknowledgement is durable and replay-safe. If it fails, leave the
          // event unacknowledged so the next poll can retry it.
          await apiPost(`/api/approvals/notifications/${encodeURIComponent(event.event_id)}/ack`)
          seen.add(event.event_id)
        } catch {
          break
        }
      }
      writeSeen(seen)
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 5000)
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [enabled, permission])

  if (permission === 'unsupported' || permission === 'granted' || permission === 'denied') return null
  return (
    <button
      type="button"
      onClick={() => void enable()}
      className="fixed right-4 top-16 z-50 inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs font-medium shadow-sm"
      aria-label="Enable approval notifications"
    >
      <Bell className="size-3.5" /> Enable notifications
    </button>
  )
}
