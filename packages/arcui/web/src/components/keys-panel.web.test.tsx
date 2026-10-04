import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { KeysPanel } from '@/components/keys-panel'
import { WebSearchProviderPicker } from '@/components/web-search-provider-picker'
import { renderWithClient, stubApi } from '@/components/maintenance/test-helpers'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const KEYS = {
  keys: [
    { provider: 'anthropic', env_var: 'ANTHROPIC_API_KEY', required: true, present: true, kind: 'model' },
    { provider: 'tavily', env_var: 'TAVILY_API_KEY', required: false, present: false, kind: 'web' },
    { provider: 'firecrawl', env_var: 'FIRECRAWL_API_KEY', required: false, present: true, kind: 'web' },
  ],
}

describe('KeysPanel — web search keys', () => {
  it('shows the web services in their own table and the AI providers in theirs', async () => {
    stubApi({ 'GET /api/keys': KEYS })
    const { unmount } = renderWithClient(<KeysPanel editable={false} kind="web" />)
    expect(await screen.findByText('TAVILY_API_KEY')).toBeTruthy()
    expect(screen.getByText('FIRECRAWL_API_KEY')).toBeTruthy()
    expect(screen.queryByText('ANTHROPIC_API_KEY')).toBeNull()
    unmount()

    renderWithClient(<KeysPanel editable={false} />)
    expect(await screen.findByText('ANTHROPIC_API_KEY')).toBeTruthy()
    expect(screen.queryByText('TAVILY_API_KEY')).toBeNull()
  })

  it('stores a web key through the same key route and never shows it back', async () => {
    const calls = stubApi({
      'GET /api/keys': KEYS,
      'PUT /api/keys/TAVILY_API_KEY': { env_var: 'TAVILY_API_KEY', present: true },
    })
    renderWithClient(<KeysPanel editable kind="web" />)

    const field = await screen.findByLabelText('New value for TAVILY_API_KEY')
    expect(field.getAttribute('type')).toBe('password')
    await userEvent.type(field, 'tvly-secret-123')
    await userEvent.click(screen.getAllByRole('button', { name: 'Save' })[0])

    await vi.waitFor(() =>
      expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({ value: 'tvly-secret-123' }),
    )
    // The path never carries the value, and the field is emptied once saved.
    expect(calls.find((c) => c.method === 'PUT')?.path).not.toContain('tvly')
    await vi.waitFor(() => expect((field as HTMLInputElement).value).toBe(''))
  })

  it('never raises the required-key warning for optional web keys', async () => {
    stubApi({ 'GET /api/keys': KEYS })
    renderWithClient(<KeysPanel editable kind="web" />)

    await screen.findByText('TAVILY_API_KEY')
    expect(screen.queryByText(/required .*not set/i)).toBeNull()
  })
})

describe('WebSearchProviderPicker', () => {
  it('shows the service now chosen in the fleet settings', async () => {
    stubApi({
      'GET /api/system-config/arcagent': {
        file: 'arcagent',
        mtime: 1,
        sections: { modules: { web: { config: { search_provider: 'tavily' } } } },
      },
    })
    renderWithClient(<WebSearchProviderPicker editable />)

    expect(await screen.findByText('Tavily')).toBeTruthy()
  })

  it('is read-only outside operator mode', async () => {
    stubApi({ 'GET /api/system-config/arcagent': { file: 'arcagent', mtime: 1, sections: {} } })
    renderWithClient(<WebSearchProviderPicker editable={false} />)

    expect(await screen.findByText(/Enable operator mode to choose a service/)).toBeTruthy()
    expect(screen.getByRole('combobox').hasAttribute('disabled')).toBe(true)
  })
})
