import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/**
 * Renders markdown (memory, skills, README, and agent-authored replies) with
 * theme styles. GFM adds tables/strikethrough/task-lists. Agent output is
 * untrusted, so we rely on react-markdown's default HTML escaping (no
 * rehype-raw) — raw HTML in the source renders as text, not markup.
 */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-arc">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  )
}
