import ReactMarkdown from 'react-markdown'

/** Renders trusted markdown (memory, skills, README) with theme styles. */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-arc">
      <ReactMarkdown>{children}</ReactMarkdown>
    </div>
  )
}
