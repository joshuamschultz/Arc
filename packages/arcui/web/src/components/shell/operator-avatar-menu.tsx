import { Lock, LockOpen, LogOut } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useMe } from '@/lib/queries'
import { clearToken } from '@/lib/auth'
import { apiPost } from '@/lib/api'
import { initials } from '@/lib/format'

/**
 * H-035: operator identity + the view/operator-mode toggle live behind this
 * avatar, fixed at the top-right of every screen — not as a row buried in
 * the sidebar's SYSTEM section. The toggle still owns no privilege itself
 * (see `useOperatorMode`); every mutation is still enforced server-side.
 */
export function OperatorAvatarMenu() {
  const [operatorOn, setOperatorOn] = useOperatorMode()
  const me = useMe()

  const name = me.data?.display_name || me.data?.email || null
  const label = name || (me.data?.anonymous ? 'Operator' : me.data?.role || 'Operator')
  const avatarInitials = initials(name) !== '??' ? initials(name) : 'JS'

  const signOut = async () => {
    try {
      await apiPost('/api/auth/logout')
    } catch {
      /* the token is cleared client-side regardless — the server session
         expires on its own if the revoke call can't reach it. */
    }
    clearToken()
    window.location.reload()
  }

  return (
    <div className="fixed right-4 top-4 z-40">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label="Operator menu"
            className="flex size-9 items-center justify-center rounded-[10px] bg-secondary text-[11px] font-bold text-secondary-foreground shadow-sm outline-none transition-colors hover:bg-secondary/80 focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            {avatarInitials}
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-64">
          <DropdownMenuLabel>
            <div className="flex flex-col gap-0.5">
              <span className="truncate text-sm font-semibold text-foreground">{label}</span>
              <span className="truncate text-xs text-muted-foreground">
                {me.data?.anonymous ? 'Signed in with a token' : me.data?.email || 'Not signed in'}
              </span>
              {me.data?.did && (
                <span className="mt-1 truncate rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                  {me.data.did}
                </span>
              )}
            </div>
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuCheckboxItem
            checked={operatorOn}
            onCheckedChange={(v) => setOperatorOn(Boolean(v))}
            title="arcui can't read your role from the server yet — toggle this on if you hold the operator token. Every save/edit/delete is still enforced server-side."
          >
            {operatorOn ? <LockOpen /> : <Lock />}
            {operatorOn ? 'Operator controls on' : 'Operator controls off'}
          </DropdownMenuCheckboxItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem variant="destructive" onClick={() => void signOut()}>
            <LogOut />
            Sign out
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}
