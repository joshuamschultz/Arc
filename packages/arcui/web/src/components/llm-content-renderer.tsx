// U8/U11 — structured rendering for LLM call bodies. Prompt/response content is a
// wall of text mixing XML-ish tool blocks, JSON, fenced code, lists, and markdown.
// This renderer detects and formats each so a call reads cleanly instead of as one
// undifferentiated blob. It handles both shapes a message `content` takes:
//   - a plain string (segmented into code / XML / JSON / prose), and
//   - an array of Anthropic-style content blocks (text / tool_use / tool_result).
// The headline case (U11) is the `AVAILABLE-SKILLS` system block — a run-together
// wall of nested <skill>…</skill> elements — which must render as an indented tree.
import { useState, type ReactNode } from 'react'
import { Markdown } from '@/components/markdown'
import { JsonBlock } from '@/components/json-block'
import { formatXml } from '@/lib/format'
import { cn } from '@/lib/utils'

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

// --- assembled prompt sections ----------------------------------------------
// arcagent assembles the system prompt as a sequence of top-level XML elements
// (<base>, <identity>, <capabilities>, <context>, …) and attaches a turn's
// retrieved material to the user message inside <agent-context>. Rendering that
// as one indented block is still a wall to scroll, so a body that IS such a
// sequence gets a collapsed, named row per section — the prompt becomes a table
// of contents you open, instead of a page you scroll past.

interface PromptSection {
  name: string
  /** The element's own name attribute, when it has one (a tool, a skill). */
  label: string | null
  body: string
}

/** The `name="…"` of an open tag — what distinguishes one `<tool>` from 79. */
function nameAttribute(openTag: string): string | null {
  return /\sname="([^"]+)"/.exec(openTag)?.[1] ?? null
}

/** Top-level `<tag>…</tag>` sections, or null when the body is not a prompt. */
function parsePromptSections(content: string): PromptSection[] | null {
  const sections: PromptSection[] = []
  let covered = 0
  XML_BLOCK.lastIndex = 0
  for (let m = XML_BLOCK.exec(content); m !== null; m = XML_BLOCK.exec(content)) {
    if (content.slice(covered, m.index).trim()) return null // prose between sections
    const name = m[1]
    const openTag = m[0].slice(0, m[0].indexOf('>') + 1)
    sections.push({
      name,
      label: nameAttribute(openTag),
      body: m[0].slice(m[0].indexOf('>') + 1, -(name.length + 3)).trim(),
    })
    covered = m.index + m[0].length
  }
  // One element counts. Requiring two made a `<tool>` carrying only a
  // `<description>` fall through to the raw-XML renderer while its neighbour
  // with a second child rendered as tidy rows — the same prompt, two looks.
  if (sections.length < 1 || content.slice(covered).trim()) return null
  return sections
}

function PromptSections({ sections }: { sections: PromptSection[] }) {
  return (
    <div className="space-y-1.5">
      {sections.map((s, i) => (
        <CollapsedBlock
          key={`${s.name}-${i}`}
          name={s.name}
          label={s.label}
          lines={s.body ? s.body.split('\n').length : 0}
        >
          <StringContent content={s.body} />
        </CollapsedBlock>
      ))}
    </div>
  )
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
        <div className="border-b border-border px-3 py-1 font-mono text-[10px] uppercase tracking-[0.08em] text-muted-foreground">
          {lang}
        </div>
      )}
      <pre className="overflow-x-auto p-3 font-mono text-xs text-foreground">{text}</pre>
    </div>
  )
}

// A long block is what makes a trace unscrollable — the retrieved
// <agent-context> on a user turn, or the AVAILABLE-SKILLS wall. Past this many
// lines the element collapses behind its tag name; shorter ones stay inline so
// small blocks are not hidden behind a click.
const XML_COLLAPSE_LINES = 12

/** An XML-ish block rendered as an indented tree, collapsed when it is long. */
function XmlSegment({ text }: { text: string }) {
  const tree = formatXml(text)
  const lines = tree.split('\n').length
  const name = /^<([a-zA-Z][\w:-]*)/.exec(text.trim())?.[1]
  const body = (
    <pre className="overflow-x-auto whitespace-pre rounded-lg border border-primary/30 bg-primary/5 p-3 font-mono text-xs leading-relaxed text-foreground">
      {tree}
    </pre>
  )
  if (lines <= XML_COLLAPSE_LINES || !name) return body
  return <CollapsedBlock name={name} lines={lines}>{body}</CollapsedBlock>
}

/** A named, collapsed shell around a long block. */
function CollapsedBlock({
  name,
  label,
  lines,
  children,
}: {
  name: string
  label?: string | null
  lines: number
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 bg-muted/30 px-3 py-1.5 text-left hover:bg-muted/50"
      >
        <span className="font-mono text-[10px] text-muted-foreground">{open ? '▾' : '▸'}</span>
        <span className="font-mono text-xs font-semibold text-primary">{name}</span>
        {label && (
          <span className="truncate font-mono text-xs text-foreground">{label}</span>
        )}
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">{lines} lines</span>
      </button>
      {open && <div className="border-t border-border p-2">{children}</div>}
    </div>
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
  const sections = parsePromptSections(content)
  if (sections) return <PromptSections sections={sections} />
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
    <div className="overflow-hidden rounded-lg border border-l-2 border-primary/30 border-l-primary/50 bg-primary/5">
      <div className="border-b border-primary/20 px-3 py-1 font-mono text-[11px] text-primary">
        → tool call · <span className="font-semibold">{block.name || 'unknown'}</span>
      </div>
      <JsonBlock value={block.input ?? {}} className="border-0 bg-transparent p-3" />
    </div>
  )
}

function ToolResultBlock({ block }: { block: Block }) {
  return (
    <div className="overflow-hidden rounded-lg border border-l-2 border-border border-l-muted-foreground/30 bg-muted/20">
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
