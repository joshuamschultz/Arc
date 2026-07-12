import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { JsonBlock } from '@/components/json-block'
import { StatusText } from '@/components/status-badge'
import { LlmContent } from '@/components/llm-content-renderer'
import { useTraceDetail } from '@/lib/queries'
import { fmtCost, fmtLatency, fmtNumber, fmtTime, shortId } from '@/lib/format'
import type { Trace } from '@/lib/types'

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">{label}</span>
      <span className="font-mono text-sm tabular-nums text-foreground">{value}</span>
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

function MessageBubble({ message }: { message: Message }) {
  return (
    <div className="rounded-lg border border-l-2 border-border border-l-primary/40 bg-muted/20 p-3">
      <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.08em] text-primary">
        {message.role || 'message'}
      </div>
      <LlmContent content={message.content} />
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
  // The list row is lightweight; fetch the full record for request/response.
  const detail = useTraceDetail(open ? (trace?.trace_id ?? null) : null)
  const full: Trace = { ...(trace ?? {}), ...(detail.data ?? {}) }
  const messages = extractMessages(full)
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
            <StatusText value={full.status} />
          </div>
          <SheetDescription>
            {full.model || 'unknown model'} · {fmtTime(full.timestamp)}
          </SheetDescription>
        </SheetHeader>

        <Tabs defaultValue="structured" className="flex flex-1 flex-col overflow-hidden">
          <TabsList className="mx-5 mt-3 w-fit">
            <TabsTrigger value="structured">Structured</TabsTrigger>
            <TabsTrigger value="raw">Raw</TabsTrigger>
          </TabsList>

          <TabsContent value="structured" className="flex-1 space-y-5 overflow-auto p-5">
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
              <Field label="Model" value={full.model || '—'} />
              <Field label="Provider" value={full.provider || '—'} />
              <Field label="Agent" value={full.agent_label || full.agent || '—'} />
              <Field label="Status" value={<StatusText value={full.status} />} />
              <Field label="Tokens in" value={fmtNumber(inTokens)} />
              <Field label="Tokens out" value={fmtNumber(full.output_tokens)} />
              <Field label="Latency" value={fmtLatency(full.duration_ms)} />
              <Field label="Cost" value={fmtCost(full.cost_usd)} />
            </div>

            <CacheBreakdown trace={full} />

            {messages.length > 0 && (
              <div className="space-y-2">
                <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                  Messages
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

            {messages.length === 0 && request !== undefined && (
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
      </SheetContent>
    </Sheet>
  )
}
