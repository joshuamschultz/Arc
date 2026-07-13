import { useMemo } from 'react'
import { highlightToHtml } from '@/lib/highlight'
import { cn } from '@/lib/utils'

/**
 * Syntax-highlighted, scrollable code block. `language` is a highlight.js
 * language name (see `inferLanguage`); an unknown/absent one falls back to
 * auto-detection so the block is still highlighted, never raw. Token colors
 * are theme-aware `.hljs-*` styles defined in `index.css`.
 */
export function CodeBlock({
  code,
  language,
  className,
}: {
  code: string
  language?: string
  className?: string
}) {
  const html = useMemo(() => highlightToHtml(code, language), [code, language])

  return (
    <pre
      className={cn(
        'overflow-auto rounded-lg border border-border bg-muted/30 p-3 font-mono text-xs leading-relaxed text-foreground',
        className,
      )}
    >
      {html != null ? (
        <code className="hljs bg-transparent p-0" dangerouslySetInnerHTML={{ __html: html }} />
      ) : (
        <code className="hljs bg-transparent p-0">{code}</code>
      )}
    </pre>
  )
}
