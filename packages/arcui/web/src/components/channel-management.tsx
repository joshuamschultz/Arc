import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Plus, UserMinus, UserPlus } from 'lucide-react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { apiDelete, apiPost, ApiError } from '@/lib/api'
import type { Channel } from '@/lib/types'

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
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Name</label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="incident-response" />
          </div>
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Members (comma-separated agent refs, optional)</label>
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

/** Member list + operator-only add/remove (COMP-006). Refs are resolved
 *  server-side through the arcteam registry — the client sends whatever the
 *  operator types (agent id, DID, or handle) and surfaces an unresolvable
 *  ref's error verbatim. */
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
  const [newMember, setNewMember] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

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
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          {channel.members.length === 0 ? (
            <p className="text-xs text-muted-foreground">No members yet.</p>
          ) : (
            <ul className="space-y-1.5">
              {channel.members.map((m) => (
                <li key={m} className="flex items-center justify-between gap-2 rounded-lg border border-border bg-card px-3 py-2 text-sm">
                  <span className="truncate font-mono text-xs text-foreground">{m}</span>
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
              <Input
                value={newMember}
                onChange={(e) => setNewMember(e.target.value)}
                placeholder="agent ref…"
                className="h-8"
                onKeyDown={(e) => e.key === 'Enter' && add()}
              />
              <Button size="sm" disabled={!newMember.trim() || busy === newMember.trim()} onClick={add}>
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
      className="flex items-center gap-1 rounded-full border border-border bg-muted/40 px-2 py-0.5 text-xs text-muted-foreground hover:bg-muted/60"
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
      className="flex size-5 shrink-0 cursor-pointer items-center justify-center rounded text-muted-foreground hover:bg-muted/50 hover:text-foreground"
      title="New channel"
    >
      <Plus className="size-3.5" />
    </button>
  )
}
