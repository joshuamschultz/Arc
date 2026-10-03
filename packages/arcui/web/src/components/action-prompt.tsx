import { useState } from 'react'
import { KeyRound } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { CustodyRepairPanel } from '@/components/custody-repair-panel'
import { RestartStackButton } from '@/components/restart-stack-button'
import { ApiError } from '@/lib/api'
import { useCustody } from '@/lib/queries'

/**
 * What a refusal asks of the person, as a plain sentence and a button where this
 * dashboard has a path for it. The server sends a CODE, never prose naming a
 * command; this is the one place a code becomes words, so no page shows a
 * terminal instruction.
 *
 * `sign_bundle` has no button yet: signing an add-on from this page is a later
 * task, and a sentence that says so is the honest state.
 */
export function ActionPrompt({ code }: { code: string | undefined }) {
  if (code === 'restart_arc') return <RestartArcPrompt />
  if (code === 'reseal_credentials') return <ResealCredentialsPrompt />
  if (code === 'sign_bundle') {
    return (
      <p data-action-prompt="sign_bundle" className="text-xs text-muted-foreground">
        This add-on is not signed with your key yet, so Arc will not install it. Signing an add-on
        from this page is not available yet.
      </p>
    )
  }
  if (code === 'ask_administrator') {
    return (
      <p data-action-prompt="ask_administrator" className="text-xs text-muted-foreground">
        This deployment only installs programs its administrator has approved. Ask your
        administrator to approve this one.
      </p>
    )
  }
  return null
}

/** The refusal carried by a failed request: its message, then what to do about it. */
export function RefusalNotice({ error, className }: { error: Error; className?: string }) {
  const code = error instanceof ApiError ? error.body?.action : undefined
  return (
    <div className="space-y-1.5">
      <p className={className ?? 'text-[11px] text-destructive'}>{error.message}</p>
      <ActionPrompt code={typeof code === 'string' ? code : undefined} />
    </div>
  )
}

function RestartArcPrompt() {
  return (
    <div data-action-prompt="restart_arc" className="flex flex-wrap items-center gap-2 text-xs">
      <span className="text-muted-foreground">
        Arc has to restart before it can use the new program.
      </span>
      <RestartStackButton label="Restart Arc" offerDatabases={false} />
    </div>
  )
}

function ResealCredentialsPrompt() {
  const custody = useCustody()
  const [open, setOpen] = useState(false)
  const status = custody.data
  return (
    <div data-action-prompt="reseal_credentials" className="space-y-2 text-xs">
      <p className="text-muted-foreground">
        This deployment cannot hold this credential at the stricter level yet. Its stored
        credentials have to be moved into the vault first.
      </p>
      {status && status.state !== 'ok' && (
        <>
          <Button size="sm" variant="outline" onClick={() => setOpen(!open)}>
            <KeyRound /> {open ? 'Hide credential review' : 'Review credentials'}
          </Button>
          {open && <CustodyRepairPanel status={status} />}
        </>
      )}
    </div>
  )
}
