import { useCallback, useEffect, useRef, useState } from 'react'
import { getToken } from '@/lib/auth'

export type AttachmentStatus = 'pending' | 'uploading' | 'clean' | 'rejected' | 'failed' | 'canceled'

export interface AttachmentItem {
  localId: string
  name: string
  size: number
  mime: string
  status: AttachmentStatus
  progress: number
  attachmentId?: string
  error?: string
}

interface AttachmentManifest {
  attachment_id: string
  declared_name: string
  size_bytes: number
  detected_mime: string
  scan_status: string
}

interface UploadResponse {
  error?: string
  attachment_id?: string
  declared_name?: string
  size_bytes?: number
  detected_mime?: string
  scan_status?: string
}

function localId(): string {
  return typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`
}

function parseResponse(xhr: XMLHttpRequest): UploadResponse {
  try {
    return JSON.parse(xhr.responseText) as UploadResponse
  } catch {
    return {}
  }
}

/**
 * Uploads browser files through the authenticated HTTP custody route.
 *
 * The browser keeps bytes in the multipart request only. Once the server has
 * accepted and scanned a file, the chat WebSocket receives its opaque
 * attachment_id; filenames, paths, and bytes never cross that wire.
 */
export function useAttachmentUploader(agentId: string | null, sessionKey: string | null) {
  const [items, setItems] = useState<AttachmentItem[]>([])
  const requests = useRef(new Map<string, XMLHttpRequest>())
  const files = useRef(new Map<string, File>())
  const sessionRef = useRef(sessionKey)

  const update = useCallback((id: string, patch: Partial<AttachmentItem>) => {
    setItems((current) => current.map((item) => (item.localId === id ? { ...item, ...patch } : item)))
  }, [])

  const upload = useCallback(
    (id: string, file: File) => {
      if (!agentId || !sessionKey) {
        update(id, { status: 'failed', error: 'Chat is not ready for attachments.' })
        return
      }
      const xhr = new XMLHttpRequest()
      requests.current.set(id, xhr)
      update(id, { status: 'uploading', progress: 0, error: undefined })
      xhr.open('POST', `/api/agents/${encodeURIComponent(agentId)}/attachments`)
      const token = getToken()
      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
      xhr.setRequestHeader('X-Session-Key', sessionKey)
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) update(id, { progress: Math.round((event.loaded / event.total) * 100) })
      }
      xhr.onload = () => {
        requests.current.delete(id)
        const response = parseResponse(xhr)
        if (xhr.status < 200 || xhr.status >= 300) {
          update(id, { status: 'failed', progress: 0, error: response.error || `Upload failed (${xhr.status}).` })
          return
        }
        const manifest: AttachmentManifest = {
          attachment_id: response.attachment_id || '',
          declared_name: response.declared_name || file.name,
          size_bytes: response.size_bytes ?? file.size,
          detected_mime: response.detected_mime || file.type || 'application/octet-stream',
          scan_status: response.scan_status || 'rejected',
        }
        if (!manifest.attachment_id || manifest.scan_status !== 'clean') {
          update(id, { status: 'rejected', progress: 100, error: 'Attachment was rejected by the scanner.' })
          return
        }
        update(id, {
          status: 'clean',
          progress: 100,
          attachmentId: manifest.attachment_id,
          name: manifest.declared_name,
          size: manifest.size_bytes,
          mime: manifest.detected_mime,
        })
      }
      xhr.onerror = () => {
        requests.current.delete(id)
        update(id, { status: 'failed', progress: 0, error: 'Upload failed. Check the connection and retry.' })
      }
      xhr.onabort = () => {
        requests.current.delete(id)
        update(id, { status: 'canceled', progress: 0, error: 'Upload canceled.' })
      }
      const form = new FormData()
      form.append('file', file, file.name)
      xhr.send(form)
    },
    [agentId, sessionKey, update],
  )

  const addFiles = useCallback(
    (fileList: FileList | File[]) => {
      const selected = Array.from(fileList)
      const next = selected.map((file) => ({
        localId: localId(),
        name: file.name,
        size: file.size,
        mime: file.type || 'application/octet-stream',
        status: 'pending' as const,
        progress: 0,
      }))
      setItems((current) => [...current, ...next])
      next.forEach((item, index) => {
        files.current.set(item.localId, selected[index])
        upload(item.localId, selected[index])
      })
    },
    [upload],
  )

  const cancelAttachment = useCallback((id: string) => {
    requests.current.get(id)?.abort()
  }, [])

  const retryAttachment = useCallback(
    (id: string) => {
      const item = items.find((candidate) => candidate.localId === id)
      const file = files.current.get(id)
      if (!item || !file) return
      update(id, { status: 'pending', progress: 0, error: undefined, attachmentId: undefined })
      upload(id, file)
    },
    [items, update, upload],
  )

  const removeAttachment = useCallback((id: string) => {
    requests.current.get(id)?.abort()
    files.current.delete(id)
    setItems((current) => current.filter((item) => item.localId !== id))
  }, [])

  const clearClean = useCallback(() => {
    setItems((current) => {
      current.forEach((item) => {
        if (item.status === 'clean') files.current.delete(item.localId)
      })
      return current.filter((item) => item.status !== 'clean')
    })
  }, [])

  useEffect(() => () => {
    requests.current.forEach((xhr) => xhr.abort())
    requests.current.clear()
  }, [])

  useEffect(() => {
    if (sessionRef.current === sessionKey) return
    requests.current.forEach((xhr) => xhr.abort())
    requests.current.clear()
    files.current.clear()
    setItems([])
    sessionRef.current = sessionKey
  }, [sessionKey])

  const cleanIds = items.flatMap((item) => (item.status === 'clean' && item.attachmentId ? [item.attachmentId] : []))
  return { items, cleanIds, addFiles, cancelAttachment, retryAttachment, removeAttachment, clearClean }
}
