import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Hash, MessageSquare, Send, Wrench } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/states'
import { StatusDot } from '@/components/status-badge'
import { useChatSession, type ChatMessage } from '@/hooks/use-chat'
import { useRoster, useTeamChannels, useChannelMessages } from '@/lib/queries'
import { initials } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { Dict } from '@/lib/types'

type Selection =
  | { kind: 'agent'; id: string; label: string }
  | { kind: 'channel'; id: string; label: string }
  | null

function Bubble({ m }: { m: ChatMessage }) {
  if (m.role === 'tool_call') {
    return (
      <div className="flex items-center gap-2 self-center rounded-md border border-border bg-muted/40 px-2 py-1 text-xs text-muted-foreground">
        <Wrench className="size-3" /> {m.tool}
        {m.text && <span className="font-mono opacity-70">{m.text}</span>}
      </div>
    )
  }
  if (m.role === 'system') {
    return <div className="self-center text-xs italic text-muted-foreground">{m.text}</div>
  }
  const mine = m.role === 'user'
  return (
    <div
      className={cn(
        'max-w-[75%] rounded-xl px-3 py-2 text-sm',
        mine
          ? 'self-end bg-primary/15 text-foreground'
          : 'self-start border border-border bg-card text-foreground',
      )}
    >
      <p className="whitespace-pre-wrap break-words">{m.text}</p>
    </div>
  )
}

function ChatPanel({ agentId }: { agentId: string }) {
  const { messages, status, sendMessage } = useChatSession(agentId)
  const [text, setText] = useState('')
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

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-2 text-xs text-muted-foreground">
        <span className="font-mono">{agentId}</span>
        <span className="capitalize">{status}</span>
      </div>
      <div className="flex flex-1 flex-col gap-2 overflow-auto p-4">
        {messages.length === 0 ? (
          <EmptyState icon={<MessageSquare className="size-7" />} title="No messages yet" description="Say hello to start the conversation." />
        ) : (
          messages.map((m) => <Bubble key={m.id} m={m} />)
        )}
        <div ref={endRef} />
      </div>
      <div className="flex items-center gap-2 border-t border-border p-3">
        <Input
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

function ChannelPanel({ name }: { name: string }) {
  const q = useChannelMessages(name)
  const endRef = useRef<HTMLDivElement>(null)
  const messages = q.data?.messages ?? []
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length])

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2 text-xs text-muted-foreground">
        <Hash className="size-3.5" /> <span className="font-mono">{name}</span>
      </div>
      <div className="flex flex-1 flex-col gap-2 overflow-auto p-4">
        {messages.length === 0 ? (
          <EmptyState icon={<Hash className="size-7" />} title="No messages in this channel" />
        ) : (
          messages.map((m: Dict, i) => (
            <div key={i} className="rounded-lg border border-border bg-card p-2.5 text-sm">
              <div className="mb-0.5 flex items-center gap-2 text-xs text-muted-foreground">
                <span className="font-medium text-foreground">{String(m.sender ?? m.from ?? m.agent_id ?? 'agent')}</span>
                <span>{String(m.ts ?? m.timestamp ?? '')}</span>
              </div>
              <p className="whitespace-pre-wrap break-words text-foreground">{String(m.text ?? m.content ?? '')}</p>
            </div>
          ))
        )}
        <div ref={endRef} />
      </div>
    </div>
  )
}

export function MessagesPage() {
  const roster = useRoster()
  const channels = useTeamChannels()
  const [sel, setSel] = useState<Selection>(null)

  const agents = (roster.data?.agents ?? []).filter((a) => !a.hidden)
  const channelList = (channels.data?.channels ?? []).map((c) =>
    typeof c === 'string' ? c : String((c as Dict).name ?? ''),
  ).filter(Boolean)

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Messages" description="Direct agent chat and team channels." />
      <div className="grid flex-1 grid-cols-[260px_1fr] overflow-hidden">
        <aside className="overflow-auto border-r border-border p-2">
          <div className="px-2 py-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Agents</div>
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
                  'flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm hover:bg-muted/50',
                  selected && 'bg-primary/10',
                )}
              >
                <span className="flex size-7 items-center justify-center rounded-md text-xs font-semibold text-primary-foreground" style={{ background: (a.color as string) || 'var(--primary)' }}>
                  {initials(label)}
                </span>
                <span className="min-w-0 flex-1 truncate text-foreground">{label}</span>
                <StatusDot online={a.online} degraded={a.degraded} className="[&>span:last-child]:hidden" />
              </button>
            )
          })}

          <div className="mt-3 px-2 py-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Channels</div>
          {channelList.length === 0 && <div className="px-2 py-1 text-xs text-muted-foreground">No channels</div>}
          {channelList.map((name) => {
            const selected = sel?.kind === 'channel' && sel.id === name
            return (
              <button
                key={name}
                type="button"
                onClick={() => setSel({ kind: 'channel', id: name, label: name })}
                className={cn(
                  'flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm text-foreground hover:bg-muted/50',
                  selected && 'bg-primary/10',
                )}
              >
                <Hash className="size-4 text-muted-foreground" />
                <span className="truncate">{name}</span>
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
          ) : (
            <ChannelPanel key={sel.id} name={sel.id} />
          )}
        </main>
      </div>
    </div>
  )
}
