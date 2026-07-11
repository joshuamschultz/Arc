// U8 — structured rendering for LLM call bodies. Prompt/response content is a
// wall of text mixing XML-ish tool blocks, fenced code, lists, and markdown.
// This renderer detects and formats each so a call reads cleanly instead of as
// one undifferentiated blob. It handles both shapes a message `content` takes:
//   - a plain string (parsed into code / XML / prose segments), and
//   - an array of Anthropic-style content blocks (text / tool_use / tool_result).
import { Markdown } from '@/components/markdown'
import { JsonBlock } from '@/components/json-block'
import { cn } from '@/lib/utils'

// --- string segmentation ----------------------------------------------------

type Segment =
  | { kind: 'code'; lang: string; text: string }
  | { kind: 'xml'; tag: string; text: string }
  | { kind: 'text'; text: string }

const CODE_FENCE = /```([\w+-]*)\n?([\s\S]*?)```/g
// A balanced <tag …>…</tag> block. Non-greedy body; backreference on the name
// so <thinking>…</thinking> and <function_calls>…</function_calls> match as a
// unit. Anchored to XML-ish tags used by tool protocols (letter-led names).
const XML_BLOCK = /<([a-zA-Z][\w:-]*)(?:\s[^>]*)?>([\s\S]*?)<\/\1>/g

/** Split a string on XML-ish tool blocks, leaving prose between them. */
function splitXml(text: string): Segment[] {
  const out: Segment[] = []
  let last = 0
  XML_BLOCK.lastIndex = 0
  for (let m = XML_BLOCK.exec(text); m !== null; m = XML_BLOCK.exec(text)) {
    if (m.index > last) out.push({ kind: 'text', text: text.slice(last, m.index) })
    out.push({ kind: 'xml', tag: m[1], text: m[2].trim() })
    last = m.index + m[0].length
  }
  if (last < text.length) out.push({ kind: 'text', text: text.slice(last) })
  return out
}

/** Parse a raw string into ordered code / XML / prose segments. */
function parseString(content: string): Segment[] {
  const segments: Segment[] = []
  let last = 0
  CODE_FENCE.lastIndex = 0
  for (let m = CODE_FENCE.exec(content); m !== null; m = CODE_FENCE.exec(content)) {
    if (m.index > last) segments.push(...splitXml(content.slice(last, m.index)))
    segments.push({ kind: 'code', lang: m[1] || '', text: m[2].replace(/\n$/, '') })
    last = m.index + m[0].length
  }
  if (last < content.length) segments.push(...splitXml(content.slice(last)))
  return segments
}

function CodeSegment({ lang, text }: { lang: string; text: string }) {
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-muted/30">
      {lang && (
        <div className="border-b border-border px-3 py-1 font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
          {lang}
        </div>
      )}
      <pre className="overflow-x-auto p-3 font-mono text-xs text-foreground">{text}</pre>
    </div>
  )
}

/** An XML-ish tool block, rendered with its tag as a header for scannability. */
function XmlSegment({ tag, text }: { tag: string; text: string }) {
  return (
    <div className="overflow-hidden rounded-lg border border-primary/30 bg-primary/5">
      <div className="border-b border-primary/20 px-3 py-1 font-mono text-[10px] uppercase tracking-wide text-primary">
        {tag}
      </div>
      <pre className="overflow-x-auto whitespace-pre-wrap break-words p-3 font-mono text-xs text-foreground">
        {text}
      </pre>
    </div>
  )
}

// --- structured content blocks (array shape) --------------------------------

interface Block {
  type?: string
  text?: string
  name?: string
  input?: unknown
  content?: unknown
  tool_use_id?: string
  id?: string
}

function ToolUseBlock({ block }: { block: Block }) {
  return (
    <div className="overflow-hidden rounded-lg border border-primary/30 bg-primary/5">
      <div className="border-b border-primary/20 px-3 py-1 font-mono text-[11px] text-primary">
        → tool call · <span className="font-semibold">{block.name || 'unknown'}</span>
      </div>
      <JsonBlock value={block.input ?? {}} className="border-0 bg-transparent p-3" />
    </div>
  )
}

function ToolResultBlock({ block }: { block: Block }) {
  const body =
    typeof block.content === 'string' ? block.content : JSON.stringify(block.content, null, 2)
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-muted/20">
      <div className="border-b border-border px-3 py-1 font-mono text-[11px] text-muted-foreground">
        ← tool result
      </div>
      <pre className="overflow-x-auto whitespace-pre-wrap break-words p-3 font-mono text-xs text-foreground">
        {body}
      </pre>
    </div>
  )
}

function ContentBlock({ block }: { block: Block }) {
  if (block.type === 'tool_use') return <ToolUseBlock block={block} />
  if (block.type === 'tool_result') return <ToolResultBlock block={block} />
  if (typeof block.text === 'string') return <StringContent content={block.text} />
  // Unknown block shape — show it raw rather than dropping signal.
  return <JsonBlock value={block} />
}

/** Render a raw string as its ordered code / XML / markdown segments. */
function StringContent({ content }: { content: string }) {
  const segments = parseString(content)
  return (
    <div className="space-y-2">
      {segments.map((seg, i) => {
        if (seg.kind === 'code') return <CodeSegment key={i} lang={seg.lang} text={seg.text} />
        if (seg.kind === 'xml') return <XmlSegment key={i} tag={seg.tag} text={seg.text} />
        const trimmed = seg.text.trim()
        if (!trimmed) return null
        return (
          <div key={i} className="prose-arc text-sm">
            <Markdown>{trimmed}</Markdown>
          </div>
        )
      })}
    </div>
  )
}

/** Public entry: structured rendering for any LLM message `content`. */
export function LlmContent({ content, className }: { content: unknown; className?: string }) {
  if (content == null) return null
  if (typeof content === 'string') {
    return (
      <div className={cn(className)}>
        <StringContent content={content} />
      </div>
    )
  }
  if (Array.isArray(content)) {
    return (
      <div className={cn('space-y-2', className)}>
        {(content as Block[]).map((block, i) => (
          <ContentBlock key={i} block={block} />
        ))}
      </div>
    )
  }
  // Object / other — fall back to a JSON view rather than "[object Object]".
  return <JsonBlock value={content} className={className} />
}
