import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { AlertCircle, Hash, MessageSquare, RotateCcw, Send, Wrench } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { Markdown } from '@/components/markdown'
import { MentionComposer, type MentionHandle } from '@/components/mention-composer'
import { EmptyState } from '@/components/states'
import { StatusDot, StatusText } from '@/components/status-badge'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import {
  ChannelMembersSheet,
  CreateChannelSheet,
  MembersButton,
  NewChannelButton,
} from '@/components/channel-management'
import { useChatSession, type ChatMessage } from '@/hooks/use-chat'
import { useTeamStream, type TeamFrame } from '@/hooks/use-team-stream'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useRoster, useTeamChannels, useChannelMessages } from '@/lib/queries'
import { apiPost, ApiError } from '@/lib/api'
import { initials } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { Channel, Dict } from '@/lib/types'

type Selection =
  | { kind: 'agent'; id: string; label: string }
  | { kind: 'channel'; id: string; label: string }
  | null

function Bubble({ m }: { m: ChatMessage }) {
  if (m.role === 'tool_call') {
    return (
      <div className="inline-flex max-w-full items-center gap-1.5 self-center rounded-md border border-border bg-muted/40 px-2 py-1 text-[11px] text-muted-foreground">
        <Wrench className="size-3 shrink-0" /> {m.tool}
        {m.text && <span className="truncate font-mono text-muted-foreground/80">{m.text}</span>}
      </div>
    )
  }
  if (m.role === 'system') {
    return <div className="self-center text-[11px] italic text-muted-foreground">{m.text}</div>
  }
  const mine = m.role === 'user'
  return (
    <div className={cn('flex max-w-[75%] flex-col gap-1', mine ? 'items-end self-end' : 'items-start self-start')}>
      <div
        className={cn(
          'rounded-lg px-3 py-2 text-sm',
          mine
            ? 'bg-primary/15 text-foreground'
            : 'border border-border bg-card text-foreground',
        )}
      >
        {mine ? (
          <p className="whitespace-pre-wrap break-words">{m.text}</p>
        ) : (
          <Markdown>{m.text}</Markdown>
        )}
      </div>
      {m.time && <span className="px-0.5 text-[10px] tabular-nums text-muted-foreground/70">{m.time}</span>}
    </div>
  )
}

function ChatPanel({ agentId }: { agentId: string }) {
  const { messages, status, sendMessage, resetForNewSession } = useChatSession(agentId)
  const [text, setText] = useState('')
  const [resetting, setResetting] = useState(false)
  const [resetError, setResetError] = useState<string | null>(null)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const send = () => {
    if (!text.trim()) return
    sendMessage(text)
    setText('')
  }
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  const newSession = async () => {
    setResetting(true)
    setResetError(null)
    try {
      await apiPost(`/api/agents/${agentId}/sessions/new`, {})
      resetForNewSession()
    } catch (e) {
      setResetError(e instanceof ApiError ? e.message : 'Could not start a new session')
    } finally {
      setResetting(false)
    }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <span className="rounded-md border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-foreground">
          {agentId}
        </span>
        <div className="flex items-center gap-3">
          {resetError && <span className="text-xs text-destructive">{resetError}</span>}
          <StatusText value={status} />
          <Button
            variant="ghost"
            size="xs"
            onClick={newSession}
            disabled={status !== 'ready' || resetting}
            title="Start a fresh conversation"
          >
            <RotateCcw className="size-3" /> New session
          </Button>
        </div>
      </div>
      <div className="flex flex-1 flex-col gap-3 overflow-auto p-4">
        {messages.length === 0 ? (
          <EmptyState icon={<MessageSquare className="size-7" />} title="No messages yet" description="Say hello to start the conversation." />
        ) : (
          messages.map((m) => <Bubble key={m.id} m={m} />)
        )}
        <div ref={endRef} />
      </div>
      <div className="flex items-end gap-2 border-t border-border bg-card/30 p-3">
        <Textarea
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          placeholder={status === 'ready' ? 'Message…' : 'Connecting…'}
          disabled={status !== 'ready'}
        />
        <Button onClick={send} disabled={status !== 'ready' || !text.trim()} size="icon">
          <Send className="size-4" />
        </Button>
      </div>
    </div>
  )
}

interface ChannelRow {
  key: string
  seq: number
  from: string
  body: string
  mentions: string[]
  ts: string
}

function handleOf(ref: string): string {
  // Defensive: the server already renders handles, but a raw ref that slips
  // through (backfill payloads) is collapsed to its trailing segment so a DID
  // never renders in the UI.
  if (ref.startsWith('did:')) return ref.split('/').pop()?.split(':').pop() ?? ref
  if (ref.includes('://')) return ref.split('://')[1] ?? ref
  return ref.replace(/^@/, '')
}

function ChannelPanel({
  channel,
  onOpenMembers,
  mentionHandles,
}: {
  channel: Channel
  onOpenMembers: () => void
  mentionHandles: MentionHandle[]
}) {
  const name = channel.name
  const history = useChannelMessages(name)
  const { frames, status, post } = useTeamStream(name)
  const endRef = useRef<HTMLDivElement>(null)
  const [text, setText] = useState('')

  // Sender colors reuse the same roster colors shown in the agent rail, so a
  // channel message row's avatar matches the sender's identity elsewhere in
  // the UI.
  const handleColor = useMemo(() => {
    const map = new Map<string, string>()
    for (const h of mentionHandles) if (h.color) map.set(h.handle, h.color)
    return map
  }, [mentionHandles])

  // History (one-shot backfill) + live frames, deduped by seq. The live stream
  // is authoritative for anything it has seen; history fills older gaps.
  const rows = useMemo<ChannelRow[]>(() => {
    const bySeq = new Map<number, ChannelRow>()
    for (const m of history.data?.messages ?? []) {
      const d = m as Dict
      const seq = Number(d.seq ?? 0)
      bySeq.set(seq, {
        key: `h${seq}`,
        seq,
        from: handleOf(String(d.sender ?? d.from ?? d.agent_id ?? 'agent')),
        body: String(d.body ?? d.text ?? d.content ?? ''),
        mentions: Array.isArray(d.mentions) ? (d.mentions as string[]).map(handleOf) : [],
        ts: String(d.ts ?? d.timestamp ?? ''),
      })
    }
    for (const f of frames as TeamFrame[]) {
      bySeq.set(f.seq, {
        key: f.id || `l${f.seq}`,
        seq: f.seq,
        from: handleOf(f.from),
        body: f.body,
        mentions: (f.mentions ?? []).map(handleOf),
        ts: f.ts,
      })
    }
    return [...bySeq.values()].sort((a, b) => a.seq - b.seq)
  }, [history.data, frames])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [rows.length])

  const send = () => {
    if (!text.trim()) return
    post(text)
    setText('')
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <span className="flex items-center gap-2">
          <Hash className="size-3.5 text-muted-foreground" />
          <span className="rounded-md border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-foreground">
            {name}
          </span>
          <MembersButton count={channel.members.length} onClick={onOpenMembers} />
        </span>
        <StatusText value={status} />
      </div>
      <div className="flex flex-1 flex-col gap-0.5 overflow-auto p-3">
        {rows.length === 0 ? (
          <EmptyState icon={<Hash className="size-7" />} title="No messages in this channel" />
        ) : (
          rows.map((m) => (
            <div
              key={m.key}
              className="-mx-1.5 flex items-start gap-3 rounded-lg px-1.5 py-2 transition-colors hover:bg-muted/40"
            >
              <span
                className="flex size-7 shrink-0 items-center justify-center rounded-md text-[11px] font-semibold text-primary-foreground"
                style={{ background: handleColor.get(m.from) || 'var(--primary)' }}
              >
                {initials(m.from)}
              </span>
              <div className="min-w-0 flex-1 text-sm">
                <div className="mb-0.5 flex flex-wrap items-center gap-2 text-xs">
                  <span className="font-semibold text-foreground">@{m.from}</span>
                  {m.mentions.map((h) => (
                    <span
                      key={h}
                      className="rounded-sm border border-primary/20 bg-primary/10 px-1.5 py-0.5 text-[11px] font-medium text-primary"
                    >
                      @{h}
                    </span>
                  ))}
                  <span className="ml-auto shrink-0 text-[10px] tabular-nums text-muted-foreground">{m.ts}</span>
                </div>
                <Markdown>{m.body}</Markdown>
              </div>
            </div>
          ))
        )}
        <div ref={endRef} />
      </div>
      <div className="flex items-center gap-2 border-t border-border bg-card/30 p-3">
        <MentionComposer
          value={text}
          onChange={setText}
          onSubmit={send}
          handles={mentionHandles}
          placeholder={status === 'ready' ? `Message #${name}… (@ to mention)` : 'Connecting…'}
          disabled={status !== 'ready'}
        />
      </div>
    </div>
  )
}

export function MessagesPage() {
  const roster = useRoster()
  const channels = useTeamChannels()
  const [sel, setSel] = useState<Selection>(null)
  const [operatorMode] = useOperatorMode()
  const [creating, setCreating] = useState(false)
  const [managingMembers, setManagingMembers] = useState(false)

  const agents = (roster.data?.agents ?? []).filter((a) => !a.hidden)
  const channelList = channels.data?.channels ?? []
  const selectedChannel = sel?.kind === 'channel' ? channelList.find((c) => c.name === sel.id) ?? null : null

  // Mention candidates are the arcteam handles (== agent name/id), so an
  // inserted @handle resolves in apply_mentions on the backend.
  const mentionHandles = useMemo<MentionHandle[]>(
    () =>
      agents
        .map((a) => ({
          handle: String(a.name || a.agent_id || ''),
          label: String(a.display_name || a.name || a.agent_id || ''),
          color: typeof a.color === 'string' ? a.color : undefined,
        }))
        .filter((h) => h.handle),
    [agents],
  )

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Messages" description="Direct agent chat and team channels." actions={<OperatorModeToggle />} />
      <div className="grid flex-1 grid-cols-[260px_1fr] overflow-hidden">
        <aside className="overflow-auto border-r border-border p-2">
          <div className="px-2 py-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            Agents
          </div>
          {agents.length === 0 && <div className="px-2 py-1 text-xs text-muted-foreground">No agents</div>}
          {agents.map((a) => {
            const label = String(a.display_name || a.name || a.agent_id)
            const selected = sel?.kind === 'agent' && sel.id === a.agent_id
            return (
              <button
                key={a.agent_id}
                type="button"
                onClick={() => setSel({ kind: 'agent', id: a.agent_id ?? '', label })}
                className={cn(
                  'group relative flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm text-muted-foreground transition-colors duration-150 hover:bg-muted/50 hover:text-foreground',
                  'before:absolute before:left-0 before:top-1/2 before:h-5 before:w-[2px] before:-translate-y-1/2 before:rounded-full before:bg-primary before:opacity-0 before:transition-opacity before:duration-150',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:ring-offset-2 focus-visible:ring-offset-background',
                  selected && 'bg-primary/8 text-foreground before:opacity-100',
                )}
              >
                <span
                  className="flex size-7 shrink-0 items-center justify-center rounded-md text-xs font-semibold text-primary-foreground"
                  style={{ background: (a.color as string) || 'var(--primary)' }}
                >
                  {initials(label)}
                </span>
                <span className="min-w-0 flex-1 truncate text-foreground">{label}</span>
                <StatusDot online={a.online} className="[&>span:last-child]:hidden" />
              </button>
            )
          })}

          <div className="mt-4 flex items-center justify-between px-2 py-1.5">
            <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Channels
            </span>
            {operatorMode && <NewChannelButton onClick={() => setCreating(true)} />}
          </div>
          {channels.isLoading && <div className="px-2 py-1 text-xs text-muted-foreground">Loading…</div>}
          {channels.isError && (
            <div className="mx-2 my-1 flex items-start gap-1.5 rounded-md border border-destructive/30 bg-destructive/10 px-2 py-1.5 text-xs text-destructive">
              <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
              <span>{channels.error instanceof ApiError ? channels.error.message : 'Channel service unavailable'}</span>
            </div>
          )}
          {!channels.isLoading && !channels.isError && channelList.length === 0 && (
            <div className="px-2 py-1 text-xs text-muted-foreground">No channels yet</div>
          )}
          {channelList.map((c) => {
            const selected = sel?.kind === 'channel' && sel.id === c.name
            return (
              <button
                key={c.name}
                type="button"
                onClick={() => setSel({ kind: 'channel', id: c.name, label: c.name })}
                className={cn(
                  'group relative flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm text-foreground transition-colors duration-150 hover:bg-muted/50',
                  'before:absolute before:left-0 before:top-1/2 before:h-5 before:w-[2px] before:-translate-y-1/2 before:rounded-full before:bg-primary before:opacity-0 before:transition-opacity before:duration-150',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:ring-offset-2 focus-visible:ring-offset-background',
                  selected && 'bg-primary/8 before:opacity-100',
                )}
              >
                <Hash className="size-4 shrink-0 text-muted-foreground" />
                <span className="truncate">{c.name}</span>
                <span className="ml-auto shrink-0 text-[11px] tabular-nums text-muted-foreground">{c.members.length}</span>
              </button>
            )
          })}
        </aside>

        <main className="overflow-hidden">
          {!sel ? (
            <div className="flex h-full items-center justify-center">
              <EmptyState icon={<MessageSquare className="size-7" />} title="Select a conversation" description="Pick an agent to chat, or a channel to follow." />
            </div>
          ) : sel.kind === 'agent' ? (
            <ChatPanel key={sel.id} agentId={sel.id} />
          ) : selectedChannel ? (
            <ChannelPanel
              key={sel.id}
              channel={selectedChannel}
              onOpenMembers={() => setManagingMembers(true)}
              mentionHandles={mentionHandles}
            />
          ) : (
            <div className="flex h-full items-center justify-center">
              <EmptyState icon={<Hash className="size-7" />} title="Channel not found" description="It may have been removed." />
            </div>
          )}
        </main>
      </div>

      <CreateChannelSheet open={creating} onOpenChange={setCreating} />
      <ChannelMembersSheet
        channel={selectedChannel}
        open={managingMembers}
        onOpenChange={setManagingMembers}
        operatorMode={operatorMode}
      />
    </div>
  )
}
