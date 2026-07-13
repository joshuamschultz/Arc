import { Markdown } from '@/components/markdown'

/** Split leading YAML frontmatter (`--- … ---`) from the markdown body. */
function splitFrontmatter(content: string): { fm: [string, string][]; body: string } {
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(content)
  if (!m) return { fm: [], body: content }
  const fm: [string, string][] = []
  for (const line of m[1].split(/\r?\n/)) {
    const i = line.indexOf(':')
    if (i > 0) fm.push([line.slice(0, i).trim(), line.slice(i + 1).trim()])
  }
  return { fm, body: content.slice(m[0].length) }
}

/** Frontmatter key/value pairs as a clean metadata table. */
export function FrontmatterMeta({ fm }: { fm: [string, string][] }) {
  if (fm.length === 0) return null
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 rounded-lg border border-border bg-muted/30 p-3 text-xs">
      {fm.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="font-mono text-muted-foreground">{k}</dt>
          <dd className="break-all font-mono text-foreground">{v || '—'}</dd>
        </div>
      ))}
    </dl>
  )
}

/** A markdown document: frontmatter as a metadata table, body as markdown. */
export function MarkdownFile({ content }: { content: string }) {
  const { fm, body } = splitFrontmatter(content)
  return (
    <div className="space-y-4">
      <FrontmatterMeta fm={fm} />
      <Markdown>{body}</Markdown>
    </div>
  )
}
