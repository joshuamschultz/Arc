import { Lock, LockOpen } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useOperatorMode } from '@/hooks/use-operator-mode'

/** Session-local switch that reveals operator affordances (edit/delete/save).
 *  See `useOperatorMode` — this does not grant privilege, the server does. */
export function OperatorModeToggle() {
  const [on, setOn] = useOperatorMode()
  return (
    <Button
      type="button"
      variant={on ? 'default' : 'outline'}
      size="sm"
      onClick={() => setOn(!on)}
      title="arcui can't read your role from the server yet — toggle this on if you hold the operator token. Every save/edit/delete is still enforced server-side."
    >
      {on ? <LockOpen className="size-3.5" /> : <Lock className="size-3.5" />}
      {on ? 'Operator controls on' : 'Operator controls off'}
    </Button>
  )
}
