import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { NewBuildBanner } from './new-build-banner'
import { apiGet } from '@/lib/api'
import { resetBuildWatchForTests, watchForNewBuild } from '@/lib/stale-build'

function setOwnBundle(name: string) {
  document.head.innerHTML = `<script type="module" src="/assets/${name}"></script>`
}

function serveBundle(getName: () => string) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ status: 'ok', bundle: getName() }))),
  )
}

const BANNER = /Arc was updated\. Reload to use the new version\./

describe('new build banner', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    resetBuildWatchForTests()
    setOwnBundle('index-OLD.js')
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('appears once the server serves a different bundle', async () => {
    let live = 'index-OLD.js'
    serveBundle(() => live)
    const stop = watchForNewBuild()
    render(<NewBuildBanner />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(screen.queryByText(BANNER)).toBeNull()

    live = 'index-NEW.js'
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(screen.getByText(BANNER)).toBeTruthy()
    stop()
  })

  it('stays hidden while the ids match, including on window focus', async () => {
    serveBundle(() => 'index-OLD.js')
    const stop = watchForNewBuild()
    render(<NewBuildBanner />)
    await act(async () => {
      window.dispatchEvent(new Event('focus'))
      await vi.advanceTimersByTimeAsync(120_000)
    })
    expect(screen.queryByText(BANNER)).toBeNull()
    stop()
  })

  it('appears on window focus without waiting for the interval', async () => {
    serveBundle(() => 'index-NEW.js')
    const stop = watchForNewBuild()
    render(<NewBuildBanner />)
    await act(async () => {
      window.dispatchEvent(new Event('focus'))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText(BANNER)).toBeTruthy()
    stop()
  })

  it('treats an unreachable server as no information, not as stale', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('offline'))))
    const stop = watchForNewBuild()
    render(<NewBuildBanner />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(screen.queryByText(BANNER)).toBeNull()
    stop()
  })

  it('Reload calls location.reload and there is no dismiss control', async () => {
    serveBundle(() => 'index-NEW.js')
    const reload = vi.fn()
    vi.stubGlobal('location', { ...window.location, reload })
    const stop = watchForNewBuild()
    render(<NewBuildBanner />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getAllByRole('button')).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }))
    expect(reload).toHaveBeenCalledTimes(1)
    stop()
  })

  it('a 5xx API response triggers an immediate build check', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (path: string) =>
        path === '/api/health'
          ? new Response(JSON.stringify({ bundle: 'index-NEW.js' }))
          : new Response('{}', { status: 502 }),
      ),
    )
    render(<NewBuildBanner />)
    await act(async () => {
      await apiGet('/api/anything').catch(() => undefined)
    })
    expect(screen.getByText(BANNER)).toBeTruthy()
  })
})
