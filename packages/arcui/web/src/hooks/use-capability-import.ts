import { useCallback, useEffect, useRef, useState } from 'react'
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
  promoted_paths?: string[]
  signer_did?: string
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
  | { status: 'promoted'; review: CapabilityImportReview; error: null }
  | { status: 'revoked'; review: CapabilityImportReview; error: null }
  | { status: 'modified'; review: CapabilityImportReview; error: null }
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
  const [reviews, setReviews] = useState<CapabilityImportReview[]>([])
  const abortRef = useRef<AbortController | null>(null)

  const stateForReview = useCallback((review: CapabilityImportReview): CapabilityImportState => {
    const status = review.status === 'promoted'
      ? 'promoted'
      : review.status === 'revoked'
        ? 'revoked'
        : review.status === 'modified'
          ? 'modified'
          : 'review_ready'
    return { status, review, error: null }
  }, [])

  useEffect(() => {
    let cancelled = false
    if (!agentId) {
      return () => { cancelled = true }
    }
    const load = async () => {
      try {
        const response = await fetch(
          `/api/agents/${encodeURIComponent(agentId)}/capability-imports`,
          { headers: authHeaders() },
        )
        if (!response.ok) throw new Error(await readError(response))
        const body = (await response.json()) as { imports?: CapabilityImportReview[] }
        if (cancelled) return
        const imported = Array.isArray(body.imports) ? body.imports : []
        setReviews(imported)
        setState(imported[0] ? stateForReview(imported[0]) : { status: 'idle', review: null, error: null })
      } catch (error) {
        if (cancelled) return
        setState({
          status: 'rejected',
          review: null,
          error: error instanceof Error ? error.message : 'Unable to load capability imports.',
        })
      }
    }
    void load()
    return () => { cancelled = true }
  }, [agentId, stateForReview])

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
      setReviews((current) => [updated, ...current.filter((item) => item.import_id !== updated.import_id)])
      setState({ status: 'review_ready', review: updated, error: null })
      return updated
    },
    [editRequest],
  )

  const trustRequest = useCallback(
    async (review: CapabilityImportReview, action: 'promote' | 'revoke') => {
      if (!agentId) throw new Error('Choose an agent first.')
      const response = await fetch(
        `/api/agents/${encodeURIComponent(agentId)}/capability-imports/${review.import_id}/${action}`,
        { method: 'POST', headers: authHeaders() },
      )
      if (!response.ok) throw new Error(await readError(response))
      const updated = (await response.json()) as CapabilityImportReview
      setReviews((current) => [updated, ...current.filter((item) => item.import_id !== updated.import_id)])
      setState({ status: updated.status as 'promoted' | 'revoked', review: updated, error: null })
      return updated
    },
    [agentId],
  )

  const promote = useCallback(
    (review: CapabilityImportReview) => trustRequest(review, 'promote'),
    [trustRequest],
  )

  const revoke = useCallback(
    (review: CapabilityImportReview) => trustRequest(review, 'revoke'),
    [trustRequest],
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
        const review = (await response.json()) as CapabilityImportReview
        setReviews((current) => [review, ...current.filter((item) => item.import_id !== review.import_id)])
        setState(stateForReview(review))
      } catch (error) {
        if (controller.signal.aborted) return
        setState({
          status: 'rejected',
          review: null,
          error: error instanceof Error ? error.message : 'Capability import failed.',
        })
      }
    },
    [agentId, stateForReview],
  )

  const reset = useCallback(() => {
    abortRef.current?.abort()
    setReviews([])
    setState({ status: 'idle', review: null, error: null })
  }, [])

  const selectReview = useCallback(
    (review: CapabilityImportReview) => setState(stateForReview(review)),
    [stateForReview],
  )

  return { ...state, reviews, upload, reset, readFile, editFile, promote, revoke, selectReview }
}
