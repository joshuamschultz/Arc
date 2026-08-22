import { useCallback, useRef, useState } from 'react'
import { getToken } from '@/lib/auth'

export interface CapabilityImportReview {
  import_id: string
  status: string
  target_agent_did: string
  archive_sha256: string
  review_digest: string
  tools: string[]
  skills: string[]
  files: Array<{ path: string; sha256: string; size: number }>
  supplier_metadata_keys?: string[]
  activation: 'review_only'
}

export interface CapabilityImportSource {
  import_id: string
  path: string
  sha256: string
  content: string
}

export type CapabilityImportState =
  | { status: 'idle'; review: null; error: null }
  | { status: 'uploading'; review: null; error: null }
  | { status: 'review_ready'; review: CapabilityImportReview; error: null }
  | { status: 'rejected'; review: null; error: string }

function authHeaders(): Record<string, string> {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function readError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { error?: string }
    return body.error || `HTTP ${response.status}`
  } catch {
    return `HTTP ${response.status}`
  }
}

export function useCapabilityImport(agentId: string | null) {
  const [state, setState] = useState<CapabilityImportState>({
    status: 'idle',
    review: null,
    error: null,
  })
  const abortRef = useRef<AbortController | null>(null)

  const editRequest = useCallback(
    async (review: CapabilityImportReview, path: string, content?: string) => {
      if (!agentId) throw new Error('Choose an agent first.')
      const url = `/api/agents/${encodeURIComponent(agentId)}/capability-imports/${review.import_id}/files/${path.split('/').map(encodeURIComponent).join('/')}`
      const response = await fetch(url, {
        method: content === undefined ? 'GET' : 'PUT',
        headers: { ...authHeaders(), ...(content === undefined ? {} : { 'Content-Type': 'application/json' }) },
        ...(content === undefined ? {} : { body: JSON.stringify({ path, content }) }),
      })
      if (!response.ok) throw new Error(await readError(response))
      return (await response.json()) as CapabilityImportReview | CapabilityImportSource
    },
    [agentId],
  )

  const readFile = useCallback(
    async (review: CapabilityImportReview, path: string) =>
      (await editRequest(review, path)) as CapabilityImportSource,
    [editRequest],
  )

  const editFile = useCallback(
    async (review: CapabilityImportReview, path: string, content: string) => {
      const updated = (await editRequest(review, path, content)) as CapabilityImportReview
      setState({ status: 'review_ready', review: updated, error: null })
      return updated
    },
    [editRequest],
  )

  const upload = useCallback(
    async (file: File) => {
      if (!agentId) {
        setState({ status: 'rejected', review: null, error: 'Choose an agent first.' })
        return
      }
      if (!file.name.toLowerCase().endsWith('.zip')) {
        setState({ status: 'rejected', review: null, error: 'Choose a ZIP archive.' })
        return
      }
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      setState({ status: 'uploading', review: null, error: null })
      const body = new FormData()
      body.append('file', file, file.name)
      try {
        const response = await fetch(`/api/agents/${encodeURIComponent(agentId)}/capability-imports`, {
          method: 'POST',
          headers: authHeaders(),
          body,
          signal: controller.signal,
        })
        if (!response.ok) throw new Error(await readError(response))
        setState({
          status: 'review_ready',
          review: (await response.json()) as CapabilityImportReview,
          error: null,
        })
      } catch (error) {
        if (controller.signal.aborted) return
        setState({
          status: 'rejected',
          review: null,
          error: error instanceof Error ? error.message : 'Capability import failed.',
        })
      }
    },
    [agentId],
  )

  const reset = useCallback(() => {
    abortRef.current?.abort()
    setState({ status: 'idle', review: null, error: null })
  }, [])

  return { ...state, upload, reset, readFile, editFile }
}
