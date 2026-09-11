import { useState } from 'react'
import { RotateCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { apiPost, ApiError } from '@/lib/api'

/** Operator-only "Restart stack" control (Settings header).
 *
 *  Restarts the whole node in order: arc.service (NATS + the fleet), then the
 *  companion services. "Include databases" adds the Postgres container bounce
 *  (store + memory index) — off by default, since a routine restart should not
 *  drop the data plane. Two-click confirm; the server enforces operator role
 *  regardless of this gate. */
export function RestartStackButton({ size = 'sm' }: { size?: 'sm' | 'default' }) {
  const [operatorMode] = useOperatorMode()
  const [armed, setArmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [withDb, setWithDb] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  if (!operatorMode) return null

  const restart = async () => {
    setBusy(true)
    setMsg(null)
    try {
      await apiPost('/api/stack/restart', { with_db: withDb })
      setMsg('Restarting — the dashboard will reconnect shortly.')
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : 'Restart failed')
      setBusy(false)
      setArmed(false)
    }
  }

  return (
    <div className="flex items-center gap-2">
      {msg && <span className="text-xs text-muted-foreground">{msg}</span>}
      <label className="flex items-center gap-1.5 text-xs text-muted-foreground select-none">
        <input
          type="checkbox"
          className="size-3.5 accent-current"
          checked={withDb}
          disabled={busy}
          onChange={(e) => setWithDb(e.target.checked)}
        />
        Include databases
      </label>
      <Button
        type="button"
        size={size}
        variant={armed ? 'destructive' : 'outline'}
        disabled={busy}
        onClick={armed ? restart : () => setArmed(true)}
        onBlur={() => setArmed(false)}
        title="Restart arc, NATS, the fleet, and companion services. Check 'Include databases' to also bounce Postgres."
      >
        <RotateCw className={busy ? 'animate-spin' : undefined} />
        {busy
          ? 'Restarting…'
          : armed
            ? withDb
              ? 'Confirm restart + DB'
              : 'Confirm restart'
            : 'Restart stack'}
      </Button>
    </div>
  )
}
