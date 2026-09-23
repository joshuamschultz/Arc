import { afterEach, expect, it, vi } from 'vitest'
import { ApiError, apiGetText } from '@/lib/api'
import { clearToken, setToken } from '@/lib/auth'

afterEach(() => {
  clearToken()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('sends the bearer token and carries caller cancellation to report fetch', async () => {
  setToken('signed-in-session')
  const caller = new AbortController()
  const fetch = vi.fn((_path: string, options: RequestInit) => {
    expect(options.headers).toEqual({ Authorization: 'Bearer signed-in-session' })
    expect(options.signal).toBeTruthy()
    return new Promise<Response>((_resolve, reject) => {
      options.signal?.addEventListener('abort', () => reject(new DOMException('cancelled', 'AbortError')))
    })
  })
  vi.stubGlobal('fetch', fetch)
  const pending = apiGetText('/api/agents/ada/files/report', caller.signal)
  caller.abort()
  await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  expect(fetch).toHaveBeenCalledTimes(1)
})

it('turns the bounded report fetch timeout into an actionable error', async () => {
  const timeout = new AbortController()
  vi.spyOn(AbortSignal, 'timeout').mockReturnValue(timeout.signal)
  vi.stubGlobal('fetch', vi.fn((_path: string, options: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      options.signal?.addEventListener('abort', () => reject(new DOMException('timed out', 'TimeoutError')))
    })))
  const pending = apiGetText('/api/agents/ada/files/report')
  timeout.abort()
  await expect(pending).rejects.toMatchObject({ status: 408, name: ApiError.name })
})
