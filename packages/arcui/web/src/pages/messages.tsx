import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertCircle, Hash, MessageSquare, RotateCcw } from 'lucide-react'
import { ToolChip } from '@/components/ai'
import { PageHeader } from '@/components/page-header'
import { Button } from '@/components/ui/button'
import { Markdown } from '@/components/markdown'
import { MentionComposer, type CommandOption, type MentionHandle } from '@/components/mention-composer'
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
import { GateCard } from '@/components/gate-card'
import { ApprovalRequest } from '@/components/hitl'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useComposerDraft } from '@/hooks/use-composer-draft'
import {
  useApprovals,
  useRoster,
  useTeamChannels,
  useChannelMessages,
  useWorkflows,
} from '@/lib/queries'
import { apiPost, ApiError } from '@/lib/api'
import { initials } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { Agent, Channel, Dict } from '@/lib/types'

type Selection =
  | { kind: 'agent'; id: string; label: string }
  | { kind: 'channel'; id: string; label: string }
  | null

function Bubble({ m }: { m: ChatMessage }) {
  if (m.role === 'tool_call') {
    return (
      <div className="self-center">
        <ToolChip tool={m.tool ?? 'tool'} arg={m.text || undefined} />
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

function ChatPanel({ agentId, commands }: { agentId: string; commands: CommandOption[] }) {
  const { messages, status, sendMessage, resetForNewSession } = useChatSession(agentId)
  const [text, setText] = useComposerDraft(`agent:${agentId}`)
  const [resetting, setResetting] = useState(false)
  const [resetError, setResetError] = useState<string | null>(null)
  const endRef = useRef<HTMLDivElement>(null)

  // Inline HITL: if this agent is blocked on a trifecta approval, surface it
  // right in the conversation so the operator acts without leaving the chat.
  const roster = useRoster()
  const approvalsQ = useApprovals()
  const [operatorMode] = useOperatorMode()
  const did = (roster.data?.agents ?? []).find((a) => a.agent_id === agentId)?.did ?? ''
  const pending = (approvalsQ.data?.approvals ?? []).filter((a) => a.agent_did === did)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const send = () => {
    if (!text.trim()) return
    sendMessage(text)
    setText('')
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
      {pending.length > 0 && (
        <div className="space-y-2 border-t border-border bg-status-warning/5 px-3 py-3">
          <div className="text-[11px] font-semibold uppercase tracking-[0.08em] text-status-warning">
            Needs your approval to continue
          </div>
          {pending.map((a) => (
            <ApprovalRequest key={a.id} a={a} operatorMode={operatorMode} />
          ))}
        </div>
      )}
      <div className="flex items-end gap-2 border-t border-border bg-card/30 p-3">
        <MentionComposer
          value={text}
          onChange={setText}
          onSubmit={send}
          handles={[]}
          commands={commands}
          placeholder={status === 'ready' ? 'Message… (/ for commands)' : 'Connecting…'}
          disabled={status !== 'ready'}
        />
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
  // SPEC-061 ArcFlow (T-855): present when this row narrates a workflow gate
  // node waiting on a human decision — renders a GateCard instead of a
  // normal chat bubble.
  gate?: { task_id: string; node_id?: string }
}

/** Map every known identifier for an agent to the name a person would use. */
function buildNameIndex(agents: Agent[]): Map<string, string> {
  const index = new Map<string, string>()
  for (const agent of agents) {
    const readable = agent.display_name || agent.name || agent.agent_id
    if (!readable) continue
    for (const key of [agent.did, agent.agent_id, agent.name]) {
      if (key) index.set(key, readable)
    }
  }
  return index
}

/**
 * Render who is speaking, as a person would say it.
 *
 * A DID collapsed to its trailing segment is a hex suffix — `7e3e1a09` — which
 * identifies an agent to the system and to nobody else. Every reference is
 * resolved through the roster first, and the collapse is kept only as the
 * last resort for a ref no roster entry claims.
 */
function handleOf(ref: string, names?: Map<string, string>): string {
  const known = names?.get(ref)
  if (known) return known
  if (ref.startsWith('did:')) {
    const tail = ref.split('/').pop()?.split(':').pop() ?? ref
    return names?.get(tail) ?? tail
  }
  if (ref.includes('://')) {
    const target = ref.split('://')[1] ?? ref
    return names?.get(target) ?? target
  }
  const bare = ref.replace(/^@/, '')
  return names?.get(bare) ?? bare
}

function ChannelPanel({
  channel,
  onOpenMembers,
  mentionHandles,
  commands,
  names,
}: {
  channel: Channel
  onOpenMembers: () => void
  mentionHandles: MentionHandle[]
  commands: CommandOption[]
  names: Map<string, string>
}) {
  const name = channel.name
  const history = useChannelMessages(name)
  const { frames, status, post } = useTeamStream(name)
  const endRef = useRef<HTMLDivElement>(null)
  const [text, setText] = useComposerDraft(`channel:${name}`)
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState<string | null>(null)
  const [sendWarning, setSendWarning] = useState<string | null>(null)

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
      const gate = d.gate as { task_id?: string; node_id?: string } | undefined
      bySeq.set(seq, {
        key: `h${seq}`,
        seq,
        from: handleOf(String(d.sender ?? d.from ?? d.agent_id ?? 'agent'), names),
        body: String(d.body ?? d.text ?? d.content ?? ''),
        mentions: Array.isArray(d.mentions)
          ? (d.mentions as string[]).map((m) => handleOf(m, names))
          : [],
        ts: String(d.ts ?? d.timestamp ?? ''),
        gate: gate?.task_id ? { task_id: gate.task_id, node_id: gate.node_id } : undefined,
      })
    }
    for (const f of frames as TeamFrame[]) {
      bySeq.set(f.seq, {
        key: f.id || `l${f.seq}`,
        seq: f.seq,
        from: handleOf(f.from, names),
        body: f.body,
        mentions: (f.mentions ?? []).map((m) => handleOf(m, names)),
        ts: f.ts,
        gate: f.gate ? { task_id: f.gate.task_id, node_id: f.gate.node_id } : undefined,
      })
    }
    return [...bySeq.values()].sort((a, b) => a.seq - b.seq)
  }, [history.data, frames, names])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [rows.length])

  // The compose box clears only once the server acknowledges the post. Clearing
  // optimistically is how a message that never reached arcteam looked sent
  // (SPEC-068 F3); the text stays put so it can be retried.
  const send = async () => {
    if (!text.trim() || sending) return
    setSending(true)
    setSendError(null)
    setSendWarning(null)
    try {
      setSendWarning(await post(text))
      setText('')
    } catch (err) {
      setSendError(err instanceof Error ? err.message : 'Could not send message.')
    } finally {
      setSending(false)
    }
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
          rows.map((m) =>
            m.gate ? (
              <div key={m.key} className="-mx-1.5 px-1.5 py-2">
                <GateCard taskId={m.gate.task_id} nodeId={m.gate.node_id} body={m.body} />
              </div>
            ) : (
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
                    <span className="ml-auto shrink-0 text-[10px] tabular-nums text-muted-foreground">
                      {m.ts}
                    </span>
                  </div>
                  <Markdown>{m.body}</Markdown>
                </div>
              </div>
            ),
          )
        )}
        <div ref={endRef} />
      </div>
      <div className="flex flex-col gap-2 border-t border-border bg-card/30 p-3">
        {sendError ? (
          <p role="alert" className="text-[11px] text-destructive">
            {sendError}
          </p>
        ) : null}
        {sendWarning ? (
          <p role="status" className="text-[11px] text-muted-foreground">
            {sendWarning}
          </p>
        ) : null}
        <div className="flex items-center gap-2">
          <MentionComposer
            value={text}
            onChange={setText}
            onSubmit={send}
            handles={mentionHandles}
            commands={commands}
            placeholder={status === 'ready' ? `Message #${name}… (@ to mention, / for commands)` : 'Connecting…'}
            disabled={status !== 'ready' || sending}
          />
        </div>
      </div>
    </div>
  )
}

// Built-in composer commands, ahead of the per-workflow ones. `new` resets the
// session; `help` shows guidance. Workflows contribute `/<id>` each.
const BUILTIN_COMMANDS: CommandOption[] = [
  { name: 'new', label: 'Start a fresh session' },
  { name: 'help', label: 'Show available commands' },
]

export function MessagesPage() {
  const roster = useRoster()
  const channels = useTeamChannels()
  const workflows = useWorkflows()
  const [sel, setSel] = useState<Selection>(null)
  const [operatorMode] = useOperatorMode()
  const [creating, setCreating] = useState(false)
  const [managingMembers, setManagingMembers] = useState(false)

  const agents = (roster.data?.agents ?? []).filter((a) => !a.hidden)
  // Who is speaking, as a person would say it. Without this a sender renders as
  // the trailing hex of its DID, which names the agent to the system and to
  // nobody else.
  const names = useMemo(() => buildNameIndex(agents), [agents])
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

  // Slash commands = built-ins + one `/<id>` per workflow (label falls back to
  // its id). Shared by direct-agent chat and channel composers alike.
  const commands = useMemo<CommandOption[]>(
    () => [
      ...BUILTIN_COMMANDS,
      ...(workflows.data?.workflows ?? []).map((w) => ({
        name: w.id,
        label: w.name || w.id,
      })),
    ],
    [workflows.data],
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
            <ChatPanel key={sel.id} agentId={sel.id} commands={commands} />
          ) : selectedChannel ? (
            <ChannelPanel
              key={sel.id}
              channel={selectedChannel}
              onOpenMembers={() => setManagingMembers(true)}
              mentionHandles={mentionHandles}
              commands={commands}
              names={names}
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
