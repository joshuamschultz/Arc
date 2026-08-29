import { useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Plus, UserMinus, UserPlus } from 'lucide-react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { apiDelete, apiPost, ApiError } from '@/lib/api'
import { useMe, useRoster } from '@/lib/queries'
import type { Agent, Channel } from '@/lib/types'

/** All the keys a channel member ref could arrive as for this agent — DID,
 *  agent id, name, and the DID's trailing hex — so membership can be checked
 *  against whichever form the server happens to have stored. */
function refKeysFor(agent: Agent): string[] {
  const tail = agent.did ? (agent.did.split('/').pop()?.split(':').pop() ?? '') : ''
  return [agent.did, agent.agent_id, agent.name, tail].filter((k): k is string => Boolean(k))
}

/** One selectable identity in the add-member dropdown: a DID to submit and
 *  the friendly name + type a person recognizes (H-019) — never a raw DID. */
interface MemberOption {
  did: string
  label: string
  type: string
}

/** Resolve a channel member ref (usually a DID) to the name a person would use:
 *  the roster display name, "Operator" for the human, else the DID's short tail. */
function useMemberName(): (ref: string) => string {
  const roster = useRoster()
  const agents = roster.data?.agents ?? []
  const byKey = new Map<string, string>()
  for (const a of agents) {
    const readable = a.display_name || a.name || a.agent_id
    if (!readable) continue
    const tail = a.did ? (a.did.split('/').pop()?.split(':').pop() ?? '') : ''
    for (const k of [a.did, a.agent_id, a.name, tail]) if (k) byKey.set(k, readable)
  }
  return (ref: string) => {
    const hit = byKey.get(ref)
    if (hit) return hit
    if (ref.startsWith('did:')) {
      const tail = ref.split('/').pop()?.split(':').pop() ?? ref
      return byKey.get(tail) ?? (ref.includes('operator') ? 'Operator' : tail)
    }
    return byKey.get(ref) ?? ref
  }
}

const CHANNELS_KEY = ['team', 'channels']

/** Operator-only create-channel form (COMP-006). Duplicate names (409) and
 *  unknown member refs (400) surface with the server's exact message. */
export function CreateChannelSheet({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [members, setMembers] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reset = () => {
    setName('')
    setMembers('')
    setError(null)
  }

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await apiPost<{ name: string; members: string[] }>('/api/team/channels', {
        name: name.trim(),
        members: members
          .split(',')
          .map((m) => m.trim())
          .filter(Boolean),
      })
      await queryClient.invalidateQueries({ queryKey: CHANNELS_KEY })
      reset()
      onOpenChange(false)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to create channel')
    } finally {
      setBusy(false)
    }
  }

  const handleOpenChange = (o: boolean) => {
    onOpenChange(o)
    if (!o) reset()
  }

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-md">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="text-sm">New channel</SheetTitle>
          <SheetDescription>Creates a real arcteam channel — visible to `arc team channels`.</SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-4 overflow-auto p-5">
          {error && (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          <div className="space-y-1.5">
            <label className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">Name</label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="incident-response" />
          </div>
          <div className="space-y-1.5">
            <label className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
              Members (comma-separated agent refs, optional)
            </label>
            <Input value={members} onChange={(e) => setMembers(e.target.value)} placeholder="coder, marketer" />
          </div>
          <Button className="w-full" disabled={busy || !name.trim()} onClick={submit}>
            {busy ? 'Creating…' : 'Create channel'}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}

/** Member list + operator-only add/remove (COMP-006). The add picker offers
 *  every roster agent and the signed-in operator (H-019) — never free text —
 *  so the DID sent to the server always names a real, known identity. Refs
 *  are still resolved server-side through the arcteam registry; an
 *  unresolvable ref's error surfaces verbatim. */
export function ChannelMembersSheet({
  channel,
  open,
  onOpenChange,
  operatorMode,
}: {
  channel: Channel | null
  open: boolean
  onOpenChange: (o: boolean) => void
  operatorMode: boolean
}) {
  const queryClient = useQueryClient()
  const memberName = useMemberName()
  const roster = useRoster()
  const me = useMe(operatorMode)
  const [newMember, setNewMember] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const memberKeys = useMemo(() => new Set(channel?.members ?? []), [channel?.members])

  const agentOptions = useMemo<MemberOption[]>(() => {
    const agents = roster.data?.agents ?? []
    const opts: MemberOption[] = []
    for (const a of agents) {
      if (!a.did || a.hidden) continue
      if (refKeysFor(a).some((k) => memberKeys.has(k))) continue
      opts.push({
        did: a.did,
        label: a.identity?.name || a.display_name || a.name || a.agent_id || a.did,
        type: a.identity?.type || a.type || 'agent',
      })
    }
    return opts
  }, [roster.data, memberKeys])

  // Multi-operator support is coming; today the only operator identity this
  // client can name is whoever is signed in to the session (H-019). A
  // static-token session (no account) reports `did: null` — nothing to offer.
  const operatorOption = useMemo<MemberOption | null>(() => {
    const meDid = me.data?.did
    if (!meDid || memberKeys.has(meDid)) return null
    return { did: meDid, label: me.data?.display_name || 'Operator', type: 'operator' }
  }, [me.data, memberKeys])

  const options = operatorOption ? [...agentOptions, operatorOption] : agentOptions

  if (channel == null) return null

  const invalidate = () => queryClient.invalidateQueries({ queryKey: CHANNELS_KEY })

  const add = async () => {
    const ref = newMember.trim()
    if (!ref) return
    setBusy(ref)
    setError(null)
    try {
      await apiPost(`/api/team/channels/${encodeURIComponent(channel.name)}/members`, { member: ref })
      await invalidate()
      setNewMember('')
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to add member')
    } finally {
      setBusy(null)
    }
  }

  const remove = async (ref: string) => {
    setBusy(ref)
    setError(null)
    try {
      await apiDelete(`/api/team/channels/${encodeURIComponent(channel.name)}/members`, { member: ref })
      await invalidate()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to remove member')
    } finally {
      setBusy(null)
    }
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-md">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="text-sm">#{channel.name} — members</SheetTitle>
          <SheetDescription>{channel.members.length} member{channel.members.length === 1 ? '' : 's'}</SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-4 overflow-auto p-5">
          {error && (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          {channel.members.length === 0 ? (
            <p className="text-xs text-muted-foreground">No members yet.</p>
          ) : (
            <ul className="space-y-1.5">
              {channel.members.map((m) => (
                <li
                  key={m}
                  className="flex items-center justify-between gap-2 rounded-lg border border-border bg-card px-3 py-2 text-sm transition-colors hover:bg-muted/30"
                >
                  <span className="flex min-w-0 flex-col">
                    <span className="truncate font-medium text-foreground">{memberName(m)}</span>
                    <span className="truncate font-mono text-[10px] text-muted-foreground">{m}</span>
                  </span>
                  {operatorMode && (
                    <Button variant="ghost" size="icon-xs" disabled={busy === m} onClick={() => remove(m)} title="Remove member">
                      <UserMinus className="size-3.5" />
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
          {operatorMode && (
            <div className="flex items-center gap-2 border-t border-border pt-4">
              <Select value={newMember} onValueChange={setNewMember} disabled={options.length === 0}>
                <SelectTrigger className="h-8 flex-1">
                  <SelectValue
                    placeholder={options.length === 0 ? 'No available identities' : 'Add a member…'}
                  />
                </SelectTrigger>
                <SelectContent>
                  {agentOptions.length > 0 && (
                    <SelectGroup>
                      <SelectLabel>Agents</SelectLabel>
                      {agentOptions.map((o) => (
                        <SelectItem key={o.did} value={o.did}>
                          <span className="flex min-w-0 flex-col">
                            <span className="truncate">{o.label}</span>
                            <span className="truncate text-[10px] uppercase tracking-[0.06em] text-muted-foreground">
                              {o.type}
                            </span>
                          </span>
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  )}
                  {operatorOption && (
                    <SelectGroup>
                      <SelectLabel>Operators</SelectLabel>
                      <SelectItem value={operatorOption.did}>
                        <span className="flex min-w-0 flex-col">
                          <span className="truncate">{operatorOption.label}</span>
                          <span className="truncate text-[10px] uppercase tracking-[0.06em] text-muted-foreground">
                            {operatorOption.type}
                          </span>
                        </span>
                      </SelectItem>
                    </SelectGroup>
                  )}
                </SelectContent>
              </Select>
              <Button size="sm" disabled={!newMember || busy === newMember} onClick={add}>
                <UserPlus className="size-3.5" /> Add
              </Button>
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}

/** Small header button showing member count; opens the members sheet. */
export function MembersButton({ count, onClick }: { count: number; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center gap-1 rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      <UserPlus className="size-3" /> {count}
    </button>
  )
}

/** Sidebar "+ new channel" trigger. */
export function NewChannelButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex size-5 shrink-0 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      title="New channel"
    >
      <Plus className="size-3.5" />
    </button>
  )
}
