import { ContextNote } from '@/components/hitl'
import { BlueprintsSection } from '@/components/maintenance/blueprints-section'
import { ModulesSection } from '@/components/maintenance/modules-section'
import { PromptHistorySection } from '@/components/maintenance/prompt-history-section'
import { TeamSection } from '@/components/maintenance/team-section'
import { UpdatesSection } from '@/components/maintenance/updates-section'

/** Settings -> Maintenance: the upkeep jobs that used to need a terminal.
 *
 *  Everyone signed in can read it. Changing anything needs operator mode here, and an
 *  operator token on the server, which refuses a viewer whatever this page shows. */
export function MaintenancePanel({ editable }: { editable: boolean }) {
  return (
    <div className="mx-auto max-w-4xl space-y-4">
      {!editable && (
        <ContextNote tone="info">
          You are looking at this page read-only. Turn on operator mode at the top of the page to
          make changes.
        </ContextNote>
      )}
      <UpdatesSection editable={editable} />
      <ModulesSection editable={editable} />
      <PromptHistorySection />
      <BlueprintsSection editable={editable} />
      <TeamSection editable={editable} />
    </div>
  )
}
