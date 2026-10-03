import { TriangleAlert } from 'lucide-react'
import { HostRequirementLine } from '@/components/host-setup-panel'
import type { HostRequirement } from '@/lib/types'

function listNames(requirements: HostRequirement[]): string {
  const names = requirements.map((r) => r.name)
  if (names.length < 2) return names[0] ?? 'A helper program'
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

/**
 * The honest state of a host prerequisite Arc can never place itself (an npm
 * package, a program with no pinned build). An Install button there would run, be
 * refused and say nothing useful, so none is drawn: the page says what is missing
 * and who has to supply it, in plain words and with no command to copy.
 *
 * Met prerequisites fall through to the quiet "already on this computer" line.
 */
export function HostNeedsNote({ requirements }: { requirements: HostRequirement[] }) {
  const pending = requirements.filter((r) => r.satisfied !== true)
  if (pending.length === 0) return <HostRequirementLine requirements={requirements} />
  return (
    <p
      data-host-needs-note
      className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 text-[11px] text-amber-800 dark:text-amber-300"
    >
      <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
      <span>
        {listNames(pending)} has to be installed on the computer that runs Arc, and Arc cannot
        install it from here yet. Ask whoever looks after that computer to add it.
      </span>
    </p>
  )
}
