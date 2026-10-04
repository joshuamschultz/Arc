import { useEffect, useRef, useState } from 'react'
import { RotateCw } from 'lucide-react'
import { ContextNote } from '@/components/hitl'
import { QueryState, EmptyState } from '@/components/states'
import { ConfirmButton, SectionCard } from '@/components/maintenance/section-card'
import { useActivateRuntime, useMaintenanceRuntime } from '@/lib/queries'
import { errorText, switchLabel } from '@/lib/maintenance'
import { waitForArcBack, type WatchOptions } from '@/lib/restart-watch'

type Phase = 'idle' | 'restarting' | 'stalled'

/** Which Arc version is in use, and a switch or roll back to another installed one.
 *
 *  Switching restarts Arc, so the page does not just refresh: it says "Restarting…",
 *  waits for Arc to answer again, and reloads itself. Fetching a new version from
 *  elsewhere is out of scope; the page says so when nothing newer is installed. */
export function UpdatesSection({
  editable,
  watch,
  onBack = () => window.location.reload(),
}: {
  editable: boolean
  /** Polling cadence; tests shorten it. */
  watch?: WatchOptions
  /** Called once Arc answers again. */
  onBack?: () => void
}) {
  const runtime = useMaintenanceRuntime()
  const activate = useActivateRuntime()
  const [phase, setPhase] = useState<Phase>('idle')
  const [target, setTarget] = useState<string | null>(null)
  const controller = useRef<AbortController | null>(null)

  useEffect(() => () => controller.current?.abort(), [])

  const switchTo = (version: string) =>
    activate.mutate(version, {
      onSuccess: () => {
        setTarget(version)
        setPhase('restarting')
        controller.current = new AbortController()
        void waitForArcBack(watch, controller.current.signal).then((back) => {
          if (controller.current?.signal.aborted) return
          if (back) onBack()
          else setPhase('stalled')
        })
      },
    })

  if (phase !== 'idle') {
    return (
      <SectionCard
        title="Updates"
        description="Which version of Arc this computer runs, and a way to go back."
      >
        {phase === 'restarting' ? (
          <div role="status" className="flex items-center gap-2 text-sm text-foreground">
            <RotateCw className="size-4 animate-spin" />
            Restarting… Arc is starting version {target}. This page reloads when it is back.
          </div>
        ) : (
          <ContextNote tone="warning">
            Arc has not answered for a while. Give it a minute, then reload this page. If it does
            not come back, the computer that runs Arc needs a look.
          </ContextNote>
        )}
      </SectionCard>
    )
  }

  return (
    <SectionCard
      title="Updates"
      description="Which version of Arc this computer runs. You can roll back to an earlier install, or switch to one that is already installed."
    >
      <QueryState
        query={runtime}
        isEmpty={(data) => data.versions.length === 0}
        empty={
          <EmptyState
            title="No versions found"
            description="Arc did not find any installed versions to switch between."
          />
        }
      >
        {(data) => (
          <div className="space-y-3">
            <p className="text-sm text-foreground">
              In use:{' '}
              <span className="font-mono text-[13px]">{data.active ?? 'unknown'}</span>
            </p>
            <ul className="divide-y divide-border rounded-md border border-border">
              {[...data.versions].reverse().map((v) => (
                <li key={v.version} className="flex flex-wrap items-center gap-x-4 gap-y-2 px-3 py-2">
                  <span className="font-mono text-[13px] text-foreground">{v.version}</span>
                  <span className="text-xs text-muted-foreground">
                    Installed {new Date(v.installed_at).toLocaleString()}
                  </span>
                  <span className="ml-auto">
                    {v.active ? (
                      <span className="rounded-sm border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-400">
                        In use
                      </span>
                    ) : (
                      editable && (
                        <ConfirmButton
                          label={switchLabel(v.relation, v.version)}
                          question={`This restarts Arc for everyone. ${switchLabel(v.relation, v.version)}?`}
                          confirmLabel="Restart and switch"
                          busy={activate.isPending}
                          onConfirm={() => switchTo(v.version)}
                        />
                      )
                    )}
                  </span>
                </li>
              ))}
            </ul>
            {activate.isError && (
              <p role="alert" className="text-sm text-destructive">
                {errorText(activate.error)}
              </p>
            )}
            <ContextNote tone="info">{data.note}</ContextNote>
          </div>
        )}
      </QueryState>
    </SectionCard>
  )
}
