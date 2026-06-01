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
import { useTraceDetail } from '@/lib/queries'
import { fmtCost, fmtLatency, fmtNumber, fmtTime, shortId } from '@/lib/format'
import type { Trace } from '@/lib/types'

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="font-mono text-sm text-foreground">{value}</span>
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

function MessageBubble({ message }: { message: Message }) {
  const content =
    typeof message.content === 'string'
      ? message.content
      : JSON.stringify(message.content, null, 2)
  return (
    <div className="rounded-lg border border-border bg-muted/20 p-3">
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-primary">
        {message.role || 'message'}
      </div>
      <pre className="whitespace-pre-wrap break-words font-mono text-xs text-foreground">
        {content}
      </pre>
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
          <SheetTitle className="font-mono text-sm">{shortId(full.trace_id, 16)}</SheetTitle>
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

            {messages.length > 0 && (
              <div className="space-y-2">
                <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Messages
                </div>
                {messages.map((m, i) => (
                  <MessageBubble key={i} message={m} />
                ))}
              </div>
            )}

            {response !== undefined && (
              <div className="space-y-1.5">
                <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Response
                </div>
                <JsonBlock value={response} />
              </div>
            )}

            {messages.length === 0 && request !== undefined && (
              <div className="space-y-1.5">
                <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
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
