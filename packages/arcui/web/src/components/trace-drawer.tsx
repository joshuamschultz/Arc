import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { JsonBlock } from '@/components/json-block'
import { LoadingRows } from '@/components/states'
import { StatusText } from '@/components/status-badge'
import { LlmContent, PromptSectionsView } from '@/components/llm-content-renderer'
import { AgentIdentity } from '@/components/AgentIdentity'
import { CapabilityBadge } from '@/components/llm/capability-badge'
import { useTraceDetail } from '@/lib/queries'
import { fmtCost, fmtLatency, fmtNumber, fmtTime, jobLabel, shortId } from '@/lib/format'
import type { AgentIdentityShape, Trace } from '@/lib/types'

/** Same graceful fallback as trace-table.tsx's — this trace's identity when
 * the roster join didn't attach one. */
function fallbackIdentity(t: Trace): AgentIdentityShape {
  return {
    did: t.agent || '',
    host: 'unknown',
    platform: 'unknown',
    type: 'unknown',
    short_id: 'unknown',
    name: t.agent_label ?? null,
  }
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">{label}</span>
      {/* A div, not a span: some values (e.g. the Type field's badge + operation
         label) are themselves block content, which a <span> can't validly hold. */}
      <div className="font-mono text-sm tabular-nums text-foreground">{value}</div>
    </div>
  )
}

interface Message {
  role?: string
  content?: unknown
}

function extractMessages(trace: Trace): Message[] {
  const req = trace.request as Record<string, unknown> | undefined
  const fromReq = req?.messages
  const direct = (trace as Record<string, unknown>).messages
  const arr = (Array.isArray(fromReq) ? fromReq : Array.isArray(direct) ? direct : []) as Message[]
  return arr
}

/** Pull renderable content out of a provider response envelope so U8 can render
 *  it structured (Anthropic `content[]`, OpenAI `choices[].message.content`).
 *  Returns undefined when the shape is unknown — the caller then shows raw JSON. */
function responseContent(response: unknown): unknown {
  if (response == null || typeof response !== 'object') return undefined
  const obj = response as Record<string, unknown>
  if (Array.isArray(obj.content) || typeof obj.content === 'string') return obj.content
  const choices = obj.choices
  if (Array.isArray(choices) && choices.length > 0) {
    const msg = (choices[0] as Record<string, unknown>)?.message as
      | Record<string, unknown>
      | undefined
    if (msg && (typeof msg.content === 'string' || Array.isArray(msg.content))) return msg.content
  }
  return undefined
}

//: Above this, an unsectioned message is collapsed rather than shown whole —
//: roughly a screenful, past which it hides the messages around it.
const LONG_MESSAGE_CHARS = 1500

/** One `<tag>…</tag>` block of an assembled prompt. */
type PromptSection = { tag: string; body: string }

/**
 * Split an assembled system prompt into its sections.
 *
 * The prompt is built as XML-ish blocks — `<identity>`, `<capabilities>`,
 * `<recall>`, `<procedures>`, `<context>` — so the parts a reader actually wants
 * to compare between calls are already delimited. Text outside any block is
 * returned under an empty tag so nothing is hidden by the split.
 *
 * Returns `null` when the content carries no sections, so an ordinary message
 * renders exactly as before rather than through a needless wrapper.
 */
function splitPromptSections(text: string): PromptSection[] | null {
  const pattern = /<([a-z][a-z0-9_-]*)>\n([\s\S]*?)\n<\/\1>/gi
  const sections: PromptSection[] = []
  let cursor = 0
  let match: RegExpExecArray | null
  while ((match = pattern.exec(text)) !== null) {
    const before = text.slice(cursor, match.index).trim()
    if (before) sections.push({ tag: '', body: before })
    sections.push({ tag: match[1], body: match[2] })
    cursor = match.index + match[0].length
  }
  if (sections.length === 0) return null
  const tail = text.slice(cursor).trim()
  if (tail) sections.push({ tag: '', body: tail })
  return sections
}

/** Rough token count, for showing what a section costs before opening it. */
function approxTokens(text: string): number {
  return Math.round(text.length / 4)
}

function SectionBlock({ section }: { section: PromptSection }) {
  // Collapsed by default: an assembled prompt runs to tens of thousands of
  // tokens, and the question a reader arrives with is almost always "which
  // parts are here and how big", not "show me all of it at once".
  return (
    <details className="rounded-md border border-border/60 bg-background/40">
      <summary className="cursor-pointer select-none px-2 py-1.5 text-xs text-muted-foreground hover:text-foreground">
        <span className="font-mono text-foreground">{section.tag || 'prose'}</span>
        <span className="ml-2 tabular-nums">~{fmtNumber(approxTokens(section.body))} tok</span>
      </summary>
      <div className="border-t border-border/60 px-2 py-2">
        <LlmContent content={section.body} />
      </div>
    </details>
  )
}

function MessageBubble({ message }: { message: Message }) {
  const text = typeof message.content === 'string' ? message.content : null
  const sections = text ? splitPromptSections(text) : null
  return (
    <div className="rounded-lg border border-l-2 border-border border-l-primary/40 bg-muted/20 p-3">
      <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.08em] text-primary">
        {message.role || 'message'}
      </div>
      {sections ? (
        <div className="space-y-1.5">
          {sections.map((section, i) => (
            <SectionBlock key={`${section.tag}-${i}`} section={section} />
          ))}
        </div>
      ) : text && text.length > LONG_MESSAGE_CHARS ? (
        // Unsectioned but long — a pasted transcript, a tool result, a replayed
        // turn. Collapsed for the same reason, so the shape of a call stays
        // readable instead of one message burying every other.
        <SectionBlock section={{ tag: '', body: text }} />
      ) : (
        <LlmContent content={message.content} />
      )}
    </div>
  )
}

/** O2 — prompt-cache accounting tiles + hit-rate. Renders only when the call
 *  carried cache figures (older rows predate cache capture). */
function CacheBreakdown({ trace }: { trace: Trace }) {
  const input = trace.input_tokens ?? trace.prompt_tokens ?? trace.total_tokens ?? null
  const output = trace.output_tokens ?? trace.completion_tokens ?? null
  const cacheRead = trace.cache_read_tokens ?? null
  const cacheWrite = trace.cache_write_tokens ?? null
  if (cacheRead == null && cacheWrite == null) return null

  const denom = (input ?? 0) + (cacheRead ?? 0)
  const hitRate = cacheRead != null && denom > 0 ? cacheRead / denom : null

  return (
    <div className="space-y-2">
      <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
        Cache breakdown
      </div>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Field label="Input" value={fmtNumber(input ?? undefined)} />
        <Field label="Cache read" value={fmtNumber(cacheRead ?? undefined)} />
        <Field label="Cache write" value={fmtNumber(cacheWrite ?? undefined)} />
        <Field label="Output" value={fmtNumber(output ?? undefined)} />
      </div>
      <div className="text-xs text-muted-foreground">
        Cache hit rate{' '}
        <span className="font-mono text-foreground">
          {hitRate != null ? `${Math.round(hitRate * 100)}%` : '—'}
        </span>
      </div>
    </div>
  )
}

/** Per-LLM-call drawer with Structured (parsed) and Raw (payload) views. */
export function TraceDrawer({
  trace,
  open,
  onOpenChange,
}: {
  trace: Trace | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  // The caller (e.g. run-detail-drawer) opens this with only {trace_id} — no
  // model/status/tokens yet, those live solely in the detail fetch. Rendering
  // real UI against that gap reads as "this call has no data" (Unknown status,
  // "unknown model", every field "—") when it's really just "not fetched yet";
  // `stillLoading` tells the two apart so a brief fetch isn't mistaken for a
  // broken/empty record.
  const detail = useTraceDetail(open ? (trace?.trace_id ?? null) : null)
  const full: Trace = { ...(trace ?? {}), ...(detail.data ?? {}) }
  const stillLoading = detail.isLoading && full.model == null
  const allMessages = extractMessages(full)
  // H-049: when the server split the prompt into ordered sections, show those
  // (they cover the whole system prompt + this turn's retrieved data) and keep
  // only the actual dialogue as Messages — the system blob no longer repeats.
  const promptSections = Array.isArray(full.prompt_sections) ? full.prompt_sections : []
  const messages = promptSections.length
    ? allMessages.filter((m) => m.role !== 'system')
    : allMessages
  const inTokens = full.input_tokens ?? full.total_tokens
  const response = full.response
  const request = full.request

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <div className="flex items-center gap-2 pr-8">
            <SheetTitle className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-foreground">
              {shortId(full.trace_id, 16)}
            </SheetTitle>
            {!stillLoading && <StatusText value={full.status} />}
          </div>
          <SheetDescription>
            {stillLoading ? 'Loading…' : `${full.model || 'unknown model'} · ${fmtTime(full.timestamp)}`}
          </SheetDescription>
        </SheetHeader>

        {stillLoading ? (
          <div className="flex-1 overflow-auto p-5">
            <LoadingRows rows={8} />
          </div>
        ) : (
        <Tabs defaultValue="structured" className="flex flex-1 flex-col overflow-hidden">
          <TabsList className="mx-5 mt-3 w-fit">
            <TabsTrigger value="structured">Structured</TabsTrigger>
            <TabsTrigger value="raw">Raw</TabsTrigger>
          </TabsList>

          <TabsContent value="structured" className="flex-1 space-y-5 overflow-auto p-5">
            {/* H-007/H-029: the canonical identity block, resolved server-side
               and joined by DID (H-008) — replaces the raw agent_label/DID text. */}
            <div className="flex items-center justify-between gap-3 rounded-lg border border-border bg-muted/20 p-3">
              <AgentIdentity
                identity={full.identity ?? fallbackIdentity(full)}
                fallbackName={full.agent_label || undefined}
                size="sm"
              />
              {jobLabel(full.job ?? null) && (
                <span
                  className="shrink-0 rounded border border-border bg-muted/40 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
                  title="A background job the agent ran on its own (not a person-driven call)"
                >
                  {jobLabel(full.job ?? null)}
                </span>
              )}
            </div>

            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
              <Field label="Model" value={full.model || '—'} />
              <Field label="Provider" value={full.provider || '—'} />
              {/* H-028/H-029: what the call WAS, plus its short operation label
                 (embed:… / retrieve:…) when the embed path stamped one. */}
              <Field
                label="Type"
                value={
                  <div className="flex flex-col items-start gap-1">
                    <CapabilityBadge value={full.capability_class} />
                    {full.operation && (
                      <span className="font-mono text-[11px] normal-case text-muted-foreground">
                        {full.operation}
                      </span>
                    )}
                  </div>
                }
              />
              <Field label="Status" value={<StatusText value={full.status} />} />
              <Field label="Tokens in" value={fmtNumber(inTokens)} />
              <Field label="Tokens out" value={fmtNumber(full.output_tokens)} />
              <Field label="Latency" value={fmtLatency(full.duration_ms)} />
              <Field label="Cost" value={fmtCost(full.cost_usd)} />
            </div>

            <CacheBreakdown trace={full} />

            {promptSections.length > 0 && (
              <div className="space-y-2">
                <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                  Prompt
                </div>
                <PromptSectionsView sections={promptSections} />
              </div>
            )}

            {messages.length > 0 && (
              <div className="space-y-2">
                <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                  {promptSections.length ? 'Conversation' : 'Messages'}
                </div>
                {messages.map((m, i) => (
                  <MessageBubble key={i} message={m} />
                ))}
              </div>
            )}

            {response !== undefined && (
              <div className="space-y-1.5">
                <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                  Response
                </div>
                {responseContent(response) !== undefined ? (
                  <LlmContent content={responseContent(response)} />
                ) : (
                  <JsonBlock value={response} />
                )}
              </div>
            )}

            {promptSections.length === 0 && messages.length === 0 && request !== undefined && (
              <div className="space-y-1.5">
                <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                  Request
                </div>
                <JsonBlock value={request} />
              </div>
            )}
          </TabsContent>

          <TabsContent value="raw" className="flex-1 overflow-auto p-5">
            <JsonBlock value={full} />
          </TabsContent>
        </Tabs>
        )}
      </SheetContent>
    </Sheet>
  )
}
