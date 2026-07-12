// U8/U11 — structured rendering for LLM call bodies. Prompt/response content is a
// wall of text mixing XML-ish tool blocks, JSON, fenced code, lists, and markdown.
// This renderer detects and formats each so a call reads cleanly instead of as one
// undifferentiated blob. It handles both shapes a message `content` takes:
//   - a plain string (segmented into code / XML / JSON / prose), and
//   - an array of Anthropic-style content blocks (text / tool_use / tool_result).
// The headline case (U11) is the `AVAILABLE-SKILLS` system block — a run-together
// wall of nested <skill>…</skill> elements — which must render as an indented tree.
import { Markdown } from '@/components/markdown'
import { JsonBlock } from '@/components/json-block'
import { cn } from '@/lib/utils'

// --- XML pretty-printer ------------------------------------------------------

// Tokenize into tags (<…>) and text runs, then re-emit one node per line with
// depth-based indentation and a blank line between sibling elements. Tolerant of
// XML-ish (not strictly well-formed) content — unbalanced tags degrade to a flat
// list rather than throwing.
function formatXml(src: string): string {
  const tokens = src.match(/<[^>]+>|[^<]+/g) ?? []
  const lines: string[] = []
  let depth = 0
  const pad = (): string => '  '.repeat(Math.max(0, depth))
  for (const token of tokens) {
    if (token.startsWith('<')) {
      const isClose = token.startsWith('</')
      const isSelfContained =
        token.endsWith('/>') || token.startsWith('<?') || token.startsWith('<!')
      if (isClose) {
        depth = Math.max(0, depth - 1)
        lines.push(pad() + token.trim())
      } else if (isSelfContained) {
        lines.push(pad() + token.trim())
      } else {
        // A new element opening right after a sibling closed gets a blank line
        // so consecutive <skill>…</skill><skill>… reads as separated blocks.
        const prev = lines[lines.length - 1]?.trim() ?? ''
        if (/<\/[^>]+>$/.test(prev)) lines.push('')
        lines.push(pad() + token.trim())
        depth += 1
      }
    } else {
      const text = token.trim()
      if (text) lines.push(pad() + text)
    }
  }
  return lines.join('\n')
}

/** True when a string contains at least one XML-ish element tag. */
function hasXml(text: string): boolean {
  return /<[a-zA-Z][\w:-]*(?:\s[^>]*)?>[\s\S]*<\/[a-zA-Z][\w:-]*>/.test(text)
}

/** Parse a whole trimmed string as JSON, but only if it is a JSON container —
 *  avoids mis-catching prose that merely contains braces. Returns the pretty
 *  form, or null when it is not JSON. */
function tryPrettyJson(text: string): string | null {
  const t = text.trim()
  if (!(t.startsWith('{') || t.startsWith('['))) return null
  try {
    return JSON.stringify(JSON.parse(t), null, 2)
  } catch {
    return null
  }
}

// --- string segmentation ----------------------------------------------------

type Segment =
  | { kind: 'code'; lang: string; text: string }
  | { kind: 'xml'; text: string }
  | { kind: 'text'; text: string }

const CODE_FENCE = /```([\w+-]*)\n?([\s\S]*?)```/g
// A balanced <tag …>…</tag> block, matched as a unit (backreference on the name)
// so an entire <available-skills>…</available-skills> or <skill>…</skill> region
// is captured whole and then pretty-printed as a tree.
const XML_BLOCK = /<([a-zA-Z][\w:-]*)(?:\s[^>]*)?>[\s\S]*?<\/\1>/g

/** Split a string on whole XML-ish blocks, leaving prose between them. */
function splitXml(text: string): Segment[] {
  const out: Segment[] = []
  let last = 0
  XML_BLOCK.lastIndex = 0
  for (let m = XML_BLOCK.exec(text); m !== null; m = XML_BLOCK.exec(text)) {
    if (m.index > last) out.push({ kind: 'text', text: text.slice(last, m.index) })
    out.push({ kind: 'xml', text: m[0] })
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

/** An XML-ish block rendered as an indented tree for scannability. */
function XmlSegment({ text }: { text: string }) {
  return (
    <pre className="overflow-x-auto whitespace-pre rounded-lg border border-primary/30 bg-primary/5 p-3 font-mono text-xs leading-relaxed text-foreground">
      {formatXml(text)}
    </pre>
  )
}

/** A prose segment: pretty-printed JSON if it is JSON, indented XML if it is a
 *  stray tag region, else markdown. */
function TextSegment({ text }: { text: string }) {
  const trimmed = text.trim()
  if (!trimmed) return null
  const json = tryPrettyJson(trimmed)
  if (json !== null) {
    return (
      <pre className="overflow-x-auto rounded-lg border border-border bg-muted/30 p-3 font-mono text-xs text-foreground">
        {json}
      </pre>
    )
  }
  // A stray XML region that slipped past whole-block matching (e.g. an unclosed
  // tag): still indent it rather than dumping a wall.
  if (hasXml(trimmed)) {
    return (
      <pre className="overflow-x-auto whitespace-pre rounded-lg border border-primary/30 bg-primary/5 p-3 font-mono text-xs text-foreground">
        {formatXml(trimmed)}
      </pre>
    )
  }
  return (
    <div className="prose-arc text-sm">
      <Markdown>{trimmed}</Markdown>
    </div>
  )
}

/** Render a raw string as its ordered code / XML / JSON / markdown segments. */
function StringContent({ content }: { content: string }) {
  const segments = parseString(content)
  return (
    <div className="space-y-2">
      {segments.map((seg, i) => {
        if (seg.kind === 'code') return <CodeSegment key={i} lang={seg.lang} text={seg.text} />
        if (seg.kind === 'xml') return <XmlSegment key={i} text={seg.text} />
        return <TextSegment key={i} text={seg.text} />
      })}
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
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-muted/20">
      <div className="border-b border-border px-3 py-1 font-mono text-[11px] text-muted-foreground">
        ← tool result
      </div>
      <div className="p-3">
        {typeof block.content === 'string' ? (
          <StringContent content={block.content} />
        ) : (
          <JsonBlock value={block.content ?? {}} className="border-0 bg-transparent p-0" />
        )}
      </div>
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

// Exported for unit-style verification of the XML pretty-printer (U11).
export { formatXml }
