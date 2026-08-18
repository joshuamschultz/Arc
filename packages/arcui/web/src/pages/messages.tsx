import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { AlertCircle, Hash, MessageSquare, RotateCcw, ScrollText, ShieldAlert } from 'lucide-react'
import { ThinkingTrace, ToolChip } from '@/components/ai'
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
import { initials, fmtTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { Agent, Channel, Dict } from '@/lib/types'

type Selection =
  | { kind: 'agent'; id: string; label: string }
  | { kind: 'channel'; id: string; label: string }
  | null

/** A tiny uppercase tag next to a sender's name: YOU for the operator, AGENT
 *  for an agent sender. Reads at a glance who authored a row without color. */
function RoleChip({ mine }: { mine: boolean }) {
  return (
    <span
      className={cn(
        'rounded px-1 py-px text-[9px] font-bold uppercase tracking-[0.06em]',
        mine ? 'bg-primary/12 text-primary' : 'bg-muted text-muted-foreground',
      )}
    >
      {mine ? 'You' : 'Agent'}
    </span>
  )
}

/** A subtle centered date divider ("Today", "Yesterday", "March 3"). */
function DateDivider({ label }: { label: string }) {
  return (
    <div className="my-2 flex items-center gap-3 px-1.5">
      <span className="h-px flex-1 bg-border" />
      <span className="rounded-full border border-border bg-card px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
        {label}
      </span>
      <span className="h-px flex-1 bg-border" />
    </div>
  )
}

/** Stable per-calendar-day key for divider grouping; null when unparseable. */
function dayKeyOf(ts: string): string | null {
  const ms = Date.parse(ts)
  if (Number.isNaN(ms)) return null
  const d = new Date(ms)
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
}

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}

/** Human day label: "Today" / "Yesterday" / a written date. */
function dayLabelOf(ts: string): string {
  const ms = Date.parse(ts)
  if (Number.isNaN(ms)) return ''
  const d = new Date(ms)
  const today = new Date()
  const yesterday = new Date()
  yesterday.setDate(today.getDate() - 1)
  if (sameDay(d, today)) return 'Today'
  if (sameDay(d, yesterday)) return 'Yesterday'
  return d.toLocaleDateString(undefined, {
    month: 'long',
    day: 'numeric',
    year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric',
  })
}

/**
 * Embedded activity card for an agent chat turn.
 *
 * The `/ws/chat` stream delivers each of a turn's tool invocations as its own
 * `tool_call` frame ahead of the agent's final message; this folds that run
 * into a compact card: a collapsible "N steps" pill (ThinkingTrace) revealing
 * one ToolChip per step, plus a link into the full signed trace on the Activity
 * page. The stream carries no run_id or reasoning duration, so the pill reads
 * "steps" (not "Thought for Xs") and the link lands on the Activity index
 * rather than a single run — see the page summary for the data gap.
 */
function AgentActivity({ steps }: { steps: ChatMessage[] }) {
  const n = steps.length
  const chips: ReactNode[] = steps.map((s) => (
    <ToolChip key={s.id} tool={s.tool ?? 'tool'} arg={s.text || undefined} />
  ))
  return (
    <div className="mb-1.5 flex flex-col gap-1.5">
      <ThinkingTrace summary={`Worked · ${n} step${n === 1 ? '' : 's'}`} steps={chips} />
      <Link
        to="/arcrun"
        className="inline-flex w-fit items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 text-[11px] font-medium text-muted-foreground transition-colors hover:border-foreground/20 hover:text-foreground"
      >
        <ScrollText className="size-3 text-signed" />
        Full signed trace in Activity
      </Link>
    </div>
  )
}

/** One rendered row of a direct-agent thread — a message (optionally carrying
 *  the turn's tool activity) or a centered system note. */
type DmRow =
  | { kind: 'msg'; id: string; mine: boolean; text: string; time: string; activity?: ChatMessage[] }
  | { kind: 'system'; id: string; text: string }

/**
 * Fold the flat chat stream into rows. Consecutive `tool_call` frames buffer
 * until the agent message they precede, which then carries them as `activity`.
 * A trailing buffer (tools still running, no final message yet) surfaces as an
 * activity-only row so the operator sees work in flight.
 */
function foldDmRows(messages: ChatMessage[]): DmRow[] {
  const rows: DmRow[] = []
  let buffer: ChatMessage[] = []
  const flush = () => {
    if (buffer.length) {
      rows.push({ kind: 'msg', id: `act-${buffer[0].id}`, mine: false, text: '', time: '', activity: buffer })
      buffer = []
    }
  }
  for (const m of messages) {
    if (m.role === 'tool_call') {
      buffer.push(m)
      continue
    }
    if (m.role === 'system') {
      flush()
      rows.push({ kind: 'system', id: m.id, text: m.text })
      continue
    }
    if (m.role === 'user') {
      flush()
      rows.push({ kind: 'msg', id: m.id, mine: true, text: m.text, time: m.time })
      continue
    }
    // agent message — attach any buffered tool activity to it
    rows.push({ kind: 'msg', id: m.id, mine: false, text: m.text, time: m.time, activity: buffer.length ? buffer : undefined })
    buffer = []
  }
  flush()
  return rows
}

function ChatPanel({
  agentId,
  agentLabel,
  agentColor,
  commands,
}: {
  agentId: string
  agentLabel: string
  agentColor: string
  commands: CommandOption[]
}) {
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
  const agent = (roster.data?.agents ?? []).find((a) => a.agent_id === agentId)
  const did = agent?.did ?? ''
  const pending = (approvalsQ.data?.approvals ?? []).filter((a) => a.agent_did === did)

  const rows = useMemo(() => foldDmRows(messages), [messages])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [rows])

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
        <span className="flex items-center gap-2.5">
          <span
            className="flex size-8 items-center justify-center rounded-md text-xs font-semibold text-primary-foreground"
            style={{ background: agentColor }}
          >
            {initials(agentLabel)}
          </span>
          <span className="flex flex-col leading-tight">
            <span className="text-[15px] font-bold text-foreground">{agentLabel}</span>
            <StatusDot online={agent?.online} className="[&>span:last-child]:text-[11px]" />
          </span>
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
      <div className="flex flex-1 flex-col gap-0.5 overflow-auto p-3">
        {rows.length === 0 ? (
          <EmptyState icon={<MessageSquare className="size-7" />} title="No messages yet" description="Say hello to start the conversation." />
        ) : (
          <>
            <DateDivider label="Today" />
            {rows.map((row, i) => {
              if (row.kind === 'system') {
                return (
                  <div key={row.id} className="self-center py-1 text-[11px] italic text-muted-foreground">
                    {row.text}
                  </div>
                )
              }
              const prev = rows[i - 1]
              const cont = prev?.kind === 'msg' && prev.mine === row.mine && !row.activity
              const label = row.mine ? 'You' : agentLabel
              const color = row.mine ? 'var(--primary)' : agentColor
              return (
                <div
                  key={row.id}
                  className={cn(
                    'group/msg -mx-1.5 flex items-start gap-3 rounded-lg px-1.5 transition-colors hover:bg-muted/40',
                    cont ? 'py-0.5' : 'mt-1.5 py-1',
                  )}
                >
                  {cont ? (
                    <span className="w-7 shrink-0 pt-0.5 text-right text-[9px] tabular-nums text-transparent group-hover/msg:text-muted-foreground">
                      {fmtTime(row.time)}
                    </span>
                  ) : (
                    <span
                      className="flex size-7 shrink-0 items-center justify-center rounded-md text-[11px] font-semibold text-primary-foreground"
                      style={{ background: color }}
                    >
                      {initials(label)}
                    </span>
                  )}
                  <div className="min-w-0 flex-1 text-sm">
                    {!cont && (row.text || row.time) && (
                      <div className="mb-0.5 flex flex-wrap items-center gap-2 text-xs">
                        <span className="font-semibold text-foreground">{label}</span>
                        <RoleChip mine={row.mine} />
                        {row.time && (
                          <span className="ml-auto shrink-0 text-[10px] tabular-nums text-muted-foreground">
                            {fmtTime(row.time)}
                          </span>
                        )}
                      </div>
                    )}
                    {row.activity && <AgentActivity steps={row.activity} />}
                    {row.text &&
                      (row.mine ? (
                        <p className="whitespace-pre-wrap break-words text-foreground">{row.text}</p>
                      ) : (
                        <Markdown>{row.text}</Markdown>
                      ))}
                  </div>
                </div>
              )
            })}
          </>
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
    // Also index the DID's trailing hex (e.g. `7e3e1a09`) — a mention or sender
    // often arrives already collapsed to that tail, which must still resolve.
    const tail = agent.did ? (agent.did.split('/').pop()?.split(':').pop() ?? '') : ''
    for (const key of [agent.did, agent.agent_id, agent.name, tail]) {
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
    const byTail = names?.get(tail)
    if (byTail) return byTail
    // The operator (the person) is not a roster agent — name them, don't show a hex.
    if (ref.includes(':operator') || ref.includes('/operator')) return 'Operator'
    return tail
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
  agentNames,
}: {
  channel: Channel
  onOpenMembers: () => void
  mentionHandles: MentionHandle[]
  commands: CommandOption[]
  names: Map<string, string>
  agentNames: Set<string>
}) {
  const name = channel.name
  const topic = channel.description?.trim()
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

  // Walk the rows once, emitting a date divider whenever the calendar day turns
  // over so the thread reads as "Today / Yesterday / March 3" like Slack.
  const rendered = useMemo<ReactNode[]>(() => {
    const out: ReactNode[] = []
    let prevDay: string | null = null
    rows.forEach((m, i) => {
      const dk = dayKeyOf(m.ts)
      if (dk && dk !== prevDay) {
        out.push(<DateDivider key={`day-${dk}-${m.key}`} label={dayLabelOf(m.ts)} />)
        prevDay = dk
      }
      if (m.gate) {
        out.push(
          <div key={m.key} className="-mx-1.5 px-1.5 py-2">
            <GateCard taskId={m.gate.task_id} nodeId={m.gate.node_id} body={m.body} />
          </div>,
        )
        return
      }
      // Slack-style grouping: a run of messages from one sender shows the
      // avatar and name once, then continuations are body-only — but a new
      // calendar day always restarts the group so a header follows the divider.
      const prev = rows[i - 1]
      const cont =
        prev && !prev.gate && prev.from === m.from && dayKeyOf(prev.ts) === dk
      const isAgent = agentNames.has(m.from)
      out.push(
        <div
          key={m.key}
          className={cn(
            'group/msg -mx-1.5 flex items-start gap-3 rounded-lg px-1.5 transition-colors hover:bg-muted/40',
            cont ? 'py-0.5' : 'mt-1.5 py-1',
          )}
        >
          {cont ? (
            <span className="w-7 shrink-0 pt-0.5 text-right text-[9px] tabular-nums text-transparent group-hover/msg:text-muted-foreground">
              {fmtTime(m.ts)}
            </span>
          ) : (
            <span
              className="flex size-7 shrink-0 items-center justify-center rounded-md text-[11px] font-semibold text-primary-foreground"
              style={{ background: handleColor.get(m.from) || 'var(--primary)' }}
            >
              {initials(m.from)}
            </span>
          )}
          <div className="min-w-0 flex-1 text-sm">
            {!cont && (
              <div className="mb-0.5 flex flex-wrap items-center gap-2 text-xs">
                <span className="font-semibold text-foreground">{m.from}</span>
                {isAgent && <RoleChip mine={false} />}
                {m.mentions.map((h) => (
                  <span
                    key={h}
                    className="rounded-sm border border-primary/20 bg-primary/10 px-1.5 py-0.5 text-[11px] font-medium text-primary"
                  >
                    @{h}
                  </span>
                ))}
                <span className="ml-auto shrink-0 text-[10px] tabular-nums text-muted-foreground">
                  {fmtTime(m.ts)}
                </span>
              </div>
            )}
            <Markdown>{m.body}</Markdown>
          </div>
        </div>,
      )
    })
    return out
  }, [rows, handleColor, agentNames])

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <span className="flex min-w-0 items-start gap-2">
          <Hash className="mt-0.5 size-5 shrink-0 text-muted-foreground" />
          <span className="flex min-w-0 flex-col leading-tight">
            <span className="flex items-center gap-2">
              <span className="text-[15px] font-bold text-foreground">{name}</span>
              <MembersButton count={channel.members.length} onClick={onOpenMembers} />
            </span>
            {topic && <span className="truncate text-xs text-muted-foreground">{topic}</span>}
          </span>
        </span>
        <StatusText value={status} />
      </div>
      <div className="flex flex-1 flex-col gap-0.5 overflow-auto p-3">
        {rows.length === 0 ? (
          <EmptyState icon={<Hash className="size-7" />} title="No messages in this channel" />
        ) : (
          rendered
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
  const approvalsQ = useApprovals()
  const [sel, setSel] = useState<Selection>(null)
  const [operatorMode] = useOperatorMode()
  const [creating, setCreating] = useState(false)
  const [managingMembers, setManagingMembers] = useState(false)

  const agents = (roster.data?.agents ?? []).filter((a) => !a.hidden)

  // Pending approvals per agent DID — an agent blocked on a human decision wears
  // a badge in the DM list so the operator sees it without opening every chat.
  const pendingByDid = useMemo(() => {
    const m = new Map<string, number>()
    for (const a of approvalsQ.data?.approvals ?? []) {
      if (a.agent_did) m.set(a.agent_did, (m.get(a.agent_did) ?? 0) + 1)
    }
    return m
  }, [approvalsQ.data])
  // Who is speaking, as a person would say it. Without this a sender renders as
  // the trailing hex of its DID, which names the agent to the system and to
  // nobody else.
  const names = useMemo(() => buildNameIndex(agents), [agents])
  // Readable names of every agent, so a channel row can tag agent senders with
  // an AGENT chip (operator posts, resolving to no roster entry, stay unchipped).
  const agentNames = useMemo(
    () => new Set(agents.map((a) => String(a.display_name || a.name || a.agent_id || ''))),
    [agents],
  )
  const channelList = channels.data?.channels ?? []
  const selectedChannel = sel?.kind === 'channel' ? channelList.find((c) => c.name === sel.id) ?? null : null
  const selectedAgent = sel?.kind === 'agent' ? agents.find((a) => a.agent_id === sel.id) ?? null : null

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
        <aside className="overflow-auto border-r border-border bg-sidebar/40 p-2">
          <div className="mb-1 flex items-center gap-2 border-b border-border px-2 pb-2.5 pt-1">
            <span className="grid size-6 place-items-center rounded-md bg-primary text-[11px] font-bold text-primary-foreground">
              A
            </span>
            <span className="text-sm font-bold tracking-tight text-foreground">Rooms</span>
          </div>

          {/* CHANNELS — on top, per the Slack layout. */}
          <div className="mt-1 flex items-center justify-between px-2 py-1.5">
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

          {/* AGENTS — direct messages, below channels. */}
          <div className="mt-4 px-2 py-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            Agents
          </div>
          {agents.length === 0 && <div className="px-2 py-1 text-xs text-muted-foreground">No agents</div>}
          {agents.map((a) => {
            const label = String(a.display_name || a.name || a.agent_id)
            const selected = sel?.kind === 'agent' && sel.id === a.agent_id
            const approvals = a.did ? pendingByDid.get(a.did) ?? 0 : 0
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
                <span className="relative shrink-0">
                  <span
                    className="flex size-7 items-center justify-center rounded-md text-xs font-semibold text-primary-foreground"
                    style={{ background: (a.color as string) || 'var(--primary)' }}
                  >
                    {initials(label)}
                  </span>
                  <StatusDot
                    online={a.online}
                    className="absolute -bottom-0.5 -right-0.5 [&>span:last-child]:hidden"
                  />
                </span>
                <span className={cn('min-w-0 flex-1 truncate', approvals > 0 ? 'font-semibold text-foreground' : 'text-foreground')}>
                  {label}
                </span>
                {approvals > 0 && (
                  <span className="inline-flex items-center gap-1 rounded-full bg-status-warning/15 px-1.5 py-0.5 text-[10px] font-semibold text-status-warning">
                    <ShieldAlert className="size-3" /> {approvals}
                  </span>
                )}
              </button>
            )
          })}
        </aside>

        <main className="overflow-hidden">
          {!sel ? (
            <div className="flex h-full items-center justify-center">
              <EmptyState icon={<MessageSquare className="size-7" />} title="Select a conversation" description="Pick a channel to follow, or an agent to chat." />
            </div>
          ) : sel.kind === 'agent' ? (
            <ChatPanel
              key={sel.id}
              agentId={sel.id}
              agentLabel={String(selectedAgent?.display_name || selectedAgent?.name || sel.label)}
              agentColor={(selectedAgent?.color as string) || 'var(--primary)'}
              commands={commands}
            />
          ) : selectedChannel ? (
            <ChannelPanel
              key={sel.id}
              channel={selectedChannel}
              onOpenMembers={() => setManagingMembers(true)}
              mentionHandles={mentionHandles}
              commands={commands}
              names={names}
              agentNames={agentNames}
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
