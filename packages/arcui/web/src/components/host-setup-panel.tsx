import { useState } from 'react'
import { CheckCircle2, Download, Terminal, TriangleAlert } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { InstructionBlock } from '@/components/instruction-block'
import { ApiError } from '@/lib/api'
import { useHostSetup } from '@/lib/queries'
import type { HostRequirement } from '@/lib/types'

function joinNames(requirements: HostRequirement[]): string {
  const names = requirements.map((r) => r.name)
  if (names.length < 2) return names[0] ?? 'a helper program'
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

/**
 * One quiet line about a bundle's host prerequisites, for the browse list —
 * enough to set expectations, never a task to read. The instructions and the
 * install button live in the connect panel, where the operator has already
 * chosen this one.
 */
export function HostRequirementLine({ requirements }: { requirements: HostRequirement[] }) {
  const pending = requirements.filter((r) => r.satisfied !== true)

  if (pending.length === 0) {
    return (
      <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <CheckCircle2 className="size-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
        {joinNames(requirements)} — already on this computer.
      </p>
    )
  }
  // `satisfied` absent means nobody checked, so this stays a statement of what
  // the bundle needs rather than a claim that the host lacks it.
  const known = pending.every((r) => r.satisfied === false)
  return (
    <p className="text-[11px] text-muted-foreground">
      Needs {joinNames(pending)} on this computer
      {known ? ' — Arc can install it when you connect.' : '.'}
    </p>
  )
}

/**
 * A connector's host prerequisites, with a button that installs them.
 *
 * The prerequisite is the point where a non-technical operator used to stop:
 * a wall of shell text and no way forward. Everything here exists so the
 * answer to "now what?" is a button, and — when the button cannot win — the
 * exact text to hand someone who does use a terminal.
 */
export function HostSetupPanel({
  agentId,
  extension,
  requirements,
  operatorMode,
  /** True when a connect attempt was just refused over these; false when the
   *  bundle merely declares them and we do not know if they are missing. */
  blocking,
}: {
  agentId: string
  extension: string
  requirements: HostRequirement[]
  operatorMode: boolean
  blocking: boolean
}) {
  const setup = useHostSetup(agentId, extension)
  const result = setup.data
  const [showManual, setShowManual] = useState(false)
  // Never ask for something this host already has. A refusal only carries the
  // requirements it tripped on, so there everything is pending by definition.
  const pending = requirements.filter((r) => r.satisfied !== true)

  const failedDetail = setup.isError
    ? setup.error instanceof ApiError && setup.error.status === 404
      ? 'This copy of Arc cannot install it for you.'
      : setup.error.message
    : result && !result.installed
      ? result.detail
      : null

  // Already there, and the server says so: nothing to warn about and nothing
  // to do. Say it once, quietly, and get out of the way.
  if (!blocking && !result && pending.length === 0) {
    return <HostRequirementLine requirements={requirements} />
  }

  // Once it is on the host the warning has no reader: the panel becomes a
  // receipt and the next step.
  if (result?.installed) {
    return (
      <p className="flex items-start gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 p-3 text-xs text-emerald-700 dark:text-emerald-400">
        <CheckCircle2 className="mt-0.5 size-4 shrink-0" />
        <span>
          {result.detail} This computer is ready — you can connect {extension} now.
        </span>
      </p>
    )
  }

  return (
    <div className="space-y-2.5 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-300">
      <p className="flex items-start gap-2 font-medium">
        <TriangleAlert className="mt-0.5 size-4 shrink-0" />
        <span>
          {blocking
            ? `Arc stopped: this computer does not have ${joinNames(pending)} yet.`
            : `This computer needs ${joinNames(pending)} before ${extension} can connect.`}
        </span>
      </p>
      <p className="text-amber-700/90 dark:text-amber-300/80">
        {extension} reaches your account through a small helper program. Arc can put it on this
        computer for you — you do not have to type anything.
      </p>

      <div className="flex flex-wrap items-center gap-2">
        {operatorMode ? (
          <Button size="sm" disabled={setup.isPending} onClick={() => setup.mutate()}>
            <Download /> {setup.isPending ? 'Installing…' : 'Install on this host'}
          </Button>
        ) : (
          <p className="italic text-amber-700/80 dark:text-amber-300/70">
            Turn on operator mode to install it from here.
          </p>
        )}
        <Button variant="ghost" size="xs" onClick={() => setShowManual(!showManual)}>
          <Terminal /> {showManual ? 'Hide the manual steps' : 'I’d rather do it myself'}
        </Button>
      </div>
      {setup.isPending && (
        <p className="text-amber-700/90 dark:text-amber-300/80">
          Downloading and installing. This can take a minute — leave this panel open.
        </p>
      )}

      {failedDetail && (
        <p className="rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-2 text-destructive">
          {failedDetail} Someone with terminal access can run the steps below instead.
        </p>
      )}

      {(showManual || failedDetail) && (
        <div className="space-y-2">
          {result?.manual_steps ? (
            <InstructionBlock text={result.manual_steps} />
          ) : (
            pending.map((h) => <InstructionBlock key={h.name} title={h.name} text={h.instruction} />)
          )}
        </div>
      )}
    </div>
  )
}
