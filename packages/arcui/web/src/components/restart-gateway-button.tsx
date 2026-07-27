import { useState } from 'react'
import { RotateCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { apiPost, ApiError } from '@/lib/api'

/** Operator-only "Restart gateway" control (shared by the Agents page + Connect tab).
 *  Restarting applies config the gateway only reads at startup (e.g. a freshly connected
 *  Telegram token). It restarts the whole fleet, so it is a two-click confirm; the server
 *  enforces operator role regardless of this gate. */
export function RestartGatewayButton({ size = 'sm' }: { size?: 'sm' | 'default' }) {
  const [operatorMode] = useOperatorMode()
  const [armed, setArmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  if (!operatorMode) return null

  const restart = async () => {
    setBusy(true)
    setMsg(null)
    try {
      await apiPost('/api/gateway/restart', {})
      setMsg('Restarting — the page will reconnect shortly.')
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : 'Restart failed')
      setBusy(false)
      setArmed(false)
    }
  }

  return (
    <div className="flex items-center gap-2">
      {msg && <span className="text-xs text-muted-foreground">{msg}</span>}
      <Button
        type="button"
        size={size}
        variant={armed ? 'destructive' : 'outline'}
        disabled={busy}
        onClick={armed ? restart : () => setArmed(true)}
        onBlur={() => setArmed(false)}
        title="Restart the gateway to apply new config (e.g. a connected Telegram bot)."
      >
        <RotateCw className={busy ? 'animate-spin' : undefined} />
        {busy ? 'Restarting…' : armed ? 'Confirm restart' : 'Restart gateway'}
      </Button>
    </div>
  )
}
