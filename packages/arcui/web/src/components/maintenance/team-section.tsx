import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ContextNote } from '@/components/hitl'
import { QueryState } from '@/components/states'
import { ConfirmButton, SectionCard } from '@/components/maintenance/section-card'
import {
  useAddTeamMember,
  useRegisterAgentWithTeam,
  useRoster,
  useSetMemberStatus,
  useTeamMembers,
} from '@/lib/queries'
import { errorText } from '@/lib/maintenance'
import type { TeamMember } from '@/lib/types'

const STATUS_LABEL: Record<TeamMember['status'], string> = {
  active: 'Active',
  suspended: 'Switched off',
  revoked: 'Removed',
}

function MemberRow({ member, editable }: { member: TeamMember; editable: boolean }) {
  const setStatus = useSetMemberStatus()
  const change = (status: TeamMember['status']) => setStatus.mutate({ did: member.did, status })
  const canChange = editable && !member.protected && member.status !== 'revoked'

  return (
    <li className="space-y-1 px-3 py-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-foreground">{member.name}</div>
          <div className="truncate text-xs text-muted-foreground">
            @{member.handle} · {member.type === 'agent' ? 'Agent' : 'Person'}
            {member.roles.length > 0 && ` · ${member.roles.join(', ')}`}
          </div>
        </div>
        <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground">
          {STATUS_LABEL[member.status]}
        </span>
        {canChange && (
          <span className="flex flex-wrap items-center gap-2">
            {member.status === 'active' ? (
              <Button
                size="sm"
                variant="outline"
                disabled={setStatus.isPending}
                onClick={() => change('suspended')}
                aria-label={`Switch off ${member.name}`}
              >
                Switch off
              </Button>
            ) : (
              <Button
                size="sm"
                variant="outline"
                disabled={setStatus.isPending}
                onClick={() => change('active')}
                aria-label={`Switch on ${member.name}`}
              >
                Switch on
              </Button>
            )}
            <ConfirmButton
              label={`Remove ${member.name}`}
              question={`Remove ${member.name} from the team? This cannot be undone here.`}
              confirmLabel="Remove"
              destructive
              busy={setStatus.isPending}
              onConfirm={() => change('revoked')}
            />
          </span>
        )}
      </div>
      {setStatus.isError && (
        <p role="alert" className="text-xs text-destructive">
          {errorText(setStatus.error)}
        </p>
      )}
    </li>
  )
}

function AddPersonForm() {
  const add = useAddTeamMember()
  const [handle, setHandle] = useState('')
  const [name, setName] = useState('')
  const [roles, setRoles] = useState('')

  const submit = () =>
    add.mutate(
      {
        handle: handle.trim(),
        name: name.trim(),
        roles: roles
          .split(',')
          .map((r) => r.trim())
          .filter(Boolean),
      },
      {
        onSuccess: () => {
          setHandle('')
          setName('')
          setRoles('')
        },
      },
    )

  return (
    <div className="space-y-2 rounded-lg border border-border bg-background p-3">
      <h3 className="text-sm font-semibold text-foreground">Add a person</h3>
      <div className="flex flex-wrap gap-2">
        <Input
          className="h-8 w-40"
          placeholder="name"
          aria-label="Name"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <Input
          className="h-8 w-40"
          placeholder="handle, like dana"
          aria-label="Handle"
          value={handle}
          onChange={(e) => setHandle(e.target.value)}
        />
        <Input
          className="h-8 w-48"
          placeholder="roles (optional)"
          aria-label="Roles"
          value={roles}
          onChange={(e) => setRoles(e.target.value)}
        />
        <Button size="sm" disabled={add.isPending || !handle.trim() || !name.trim()} onClick={submit}>
          {add.isPending ? 'Adding…' : 'Add person'}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        The handle is what people type after @ in team chat: lowercase letters, digits, - or _.
      </p>
      {add.isError && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(add.error)}
        </p>
      )}
    </div>
  )
}

/** Agents that exist on this computer but are missing from the team list can be added back. */
function MissingAgents({ members }: { members: TeamMember[] }) {
  const roster = useRoster()
  const register = useRegisterAgentWithTeam()
  const known = new Set(members.map((m) => m.did))
  const missing = (roster.data?.agents ?? []).filter(
    (a) => !a.hidden && a.agent_id && (a.harness ?? 'arcagent') === 'arcagent' && a.did && !known.has(a.did),
  )
  if (missing.length === 0) return null
  return (
    <ContextNote tone="warning">
      <span>These agents are not on the team yet, so they cannot be reached in team chat.</span>
      <span className="mt-2 flex flex-wrap gap-2">
        {missing.map((a) => (
          <Button
            key={a.agent_id}
            size="sm"
            variant="outline"
            disabled={register.isPending}
            onClick={() => register.mutate(a.agent_id as string)}
            aria-label={`Add ${a.display_name || a.name || a.agent_id} to the team`}
          >
            Add {a.display_name || a.name || a.agent_id} to the team
          </Button>
        ))}
      </span>
      {register.isError && (
        <span role="alert" className="mt-1 block text-destructive">
          {errorText(register.error)}
        </span>
      )}
    </ContextNote>
  )
}

/** The team list: agents and people. Add a person, switch a member off or on, remove one,
 *  or add an agent that fell off the list. */
export function TeamSection({ editable }: { editable: boolean }) {
  const members = useTeamMembers()
  return (
    <SectionCard
      title="Team members"
      description="Everyone who can be reached in team chat: your agents and the people you work with."
    >
      <QueryState query={members}>
        {(data) => (
          <div className="space-y-3">
            <ul className="divide-y divide-border rounded-md border border-border">
              {data.members.map((m) => (
                <MemberRow key={m.did} member={m} editable={editable} />
              ))}
            </ul>
            {editable && <MissingAgents members={data.members} />}
            {editable && <AddPersonForm />}
          </div>
        )}
      </QueryState>
    </SectionCard>
  )
}
