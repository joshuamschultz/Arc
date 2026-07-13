import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { CodeBlock } from '@/components/code-block'
import { inferLanguage } from '@/lib/highlight'

// Render fenced/multi-line code through the syntax-highlighted CodeBlock while
// keeping inline `code` spans as lightweight inline chips. `pre` is a pass-through
// so the block isn't double-wrapped (CodeBlock renders its own <pre>).
const COMPONENTS: Components = {
  pre: ({ children }) => <>{children}</>,
  code: ({ className, children, ...props }) => {
    const text = String(children ?? '')
    const lang = /language-(\w+)/.exec(className ?? '')?.[1]
    const isBlock = lang != null || text.includes('\n')
    if (!isBlock) {
      return (
        <code className={className} {...props}>
          {children}
        </code>
      )
    }
    return <CodeBlock code={text.replace(/\n$/, '')} language={inferLanguage(lang)} />
  },
}

/**
 * Renders markdown (memory, skills, README, and agent-authored replies) with
 * theme styles. GFM adds tables/strikethrough/task-lists; fenced code is
 * syntax-highlighted. Agent output is untrusted, so we rely on react-markdown's
 * default HTML escaping (no rehype-raw) — raw HTML in the source renders as
 * text, not markup.
 */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-arc">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {children}
      </ReactMarkdown>
    </div>
  )
}
