import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { UpdatesSection } from '@/components/maintenance/updates-section'
import { renderWithClient, stubApi } from '@/components/maintenance/test-helpers'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const RUNTIME = {
  active: '0.2.0-bbbbbbb',
  versions: [
    { version: '0.2.0-aaaaaaa', active: false, installed_at: '2026-09-01T10:00:00+00:00', relation: 'older' },
    { version: '0.2.0-bbbbbbb', active: true, installed_at: '2026-09-20T10:00:00+00:00', relation: 'active' },
    { version: '0.3.0-ccccccc', active: false, installed_at: '2026-09-30T10:00:00+00:00', relation: 'newer' },
  ],
  newer_available: true,
  note: 'A newer version is already installed. Choose Switch to use it.',
}

const fastWatch = { intervalMs: 1, graceMs: 5, maxMs: 200 }

describe('UpdatesSection', () => {
  it('shows the version in use, when each was installed, and the plain note', async () => {
    stubApi({ 'GET /api/maintenance/runtime': RUNTIME })
    renderWithClient(<UpdatesSection editable />)

    expect(await screen.findByText('In use')).toBeTruthy()
    expect(screen.getAllByText('0.2.0-bbbbbbb').length).toBeGreaterThan(0)
    expect(screen.getByText('0.2.0-aaaaaaa')).toBeTruthy()
    expect(screen.getAllByText(/Installed /).length).toBe(3)
    expect(screen.getByText(/newer version is already installed/i)).toBeTruthy()
  })

  it('says plainly that nothing newer is installed and that downloading is out of scope', async () => {
    stubApi({
      'GET /api/maintenance/runtime': {
        ...RUNTIME,
        versions: RUNTIME.versions.slice(0, 2),
        newer_available: false,
        note: 'Nothing newer is installed on this computer. Arc cannot download a new version from here yet; a new version has to be installed on the computer first.',
      },
    })
    renderWithClient(<UpdatesSection editable />)

    expect(await screen.findByText(/cannot download a new version/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /switch to/i })).toBeNull()
  })

  it('offers Roll back for an older install and Switch for a newer one', async () => {
    stubApi({ 'GET /api/maintenance/runtime': RUNTIME })
    renderWithClient(<UpdatesSection editable />)

    expect(await screen.findByRole('button', { name: 'Roll back to 0.2.0-aaaaaaa' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Switch to 0.3.0-ccccccc' })).toBeTruthy()
  })

  it('needs two steps: nothing is sent until the confirm', async () => {
    const calls = stubApi({ 'GET /api/maintenance/runtime': RUNTIME })
    renderWithClient(<UpdatesSection editable />)

    await userEvent.click(await screen.findByRole('button', { name: 'Roll back to 0.2.0-aaaaaaa' }))
    expect(screen.getByText(/restarts Arc for everyone/i)).toBeTruthy()
    expect(calls.filter((c) => c.method === 'POST')).toEqual([])

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(calls.filter((c) => c.method === 'POST')).toEqual([])
  })

  it('shows Restarting, waits for health, then reloads', async () => {
    let healthCalls = 0
    const calls = stubApi({
      'GET /api/maintenance/runtime': RUNTIME,
      'POST /api/maintenance/runtime/activate': {
        restarting: true,
        version: '0.2.0-aaaaaaa',
        message: 'Switched.',
      },
      // Down once (the restart), then back.
      'GET /api/health': () => {
        healthCalls += 1
        return healthCalls === 1 ? { status: 503, body: {} } : { status: 'ok' }
      },
    })
    const onBack = vi.fn()
    renderWithClient(<UpdatesSection editable watch={fastWatch} onBack={onBack} />)

    await userEvent.click(await screen.findByRole('button', { name: 'Roll back to 0.2.0-aaaaaaa' }))
    await userEvent.click(screen.getByRole('button', { name: 'Restart and switch' }))

    expect(await screen.findByText(/Restarting…/)).toBeTruthy()
    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1))
    const post = calls.find((c) => c.method === 'POST')
    expect(post?.body).toEqual({ version: '0.2.0-aaaaaaa' })
  })

  it('tells the person when Arc does not come back', async () => {
    stubApi({
      'GET /api/maintenance/runtime': RUNTIME,
      'POST /api/maintenance/runtime/activate': { restarting: true, version: 'x', message: 'm' },
      'GET /api/health': { status: 503, body: {} },
    })
    renderWithClient(<UpdatesSection editable watch={{ intervalMs: 1, graceMs: 5, maxMs: 30 }} />)

    await userEvent.click(await screen.findByRole('button', { name: 'Roll back to 0.2.0-aaaaaaa' }))
    await userEvent.click(screen.getByRole('button', { name: 'Restart and switch' }))

    expect(await screen.findByText(/has not answered for a while/i)).toBeTruthy()
  })

  it('shows the server refusal in plain words and stays on the page', async () => {
    stubApi({
      'GET /api/maintenance/runtime': RUNTIME,
      'POST /api/maintenance/runtime/activate': {
        status: 404,
        body: { error: 'Version 0.2.0-aaaaaaa is not installed here.' },
      },
    })
    renderWithClient(<UpdatesSection editable />)

    await userEvent.click(await screen.findByRole('button', { name: 'Roll back to 0.2.0-aaaaaaa' }))
    await userEvent.click(screen.getByRole('button', { name: 'Restart and switch' }))

    expect(await screen.findByRole('alert')).toHaveProperty(
      'textContent',
      'Version 0.2.0-aaaaaaa is not installed here.',
    )
  })

  it('hides every switch button from someone who is not in operator mode', async () => {
    stubApi({ 'GET /api/maintenance/runtime': RUNTIME })
    renderWithClient(<UpdatesSection editable={false} />)

    await screen.findByText('In use')
    expect(screen.queryByRole('button')).toBeNull()
  })
})
