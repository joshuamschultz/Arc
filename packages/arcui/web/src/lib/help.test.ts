import { describe, expect, it } from 'vitest'
import { createElement } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DataTable } from '@/components/data-table'
import { configHelpKey, fieldHelp, helpRoute } from '@/lib/help'
import content from '@/content/screen-help.json'

const staticHelpSources = import.meta.glob(['../pages/**/*.tsx', '../components/**/*.tsx'], {
  eager: true,
  query: '?raw',
  import: 'default',
}) as Record<string, string>

const staticHelpUses = Object.entries(staticHelpSources)
  .flatMap(([file, source]) => {
    return [...source.matchAll(/<FieldHelp\s+helpKey="([^"]+)"(?:\s+route="([^"]+)")?/g)]
      .map(([, key, route]) => ({ file, key, route }))
  })

describe('configuration field help', () => {
  it.each([
    ['arcllm', ['llm', 'model'], 'settings.arcllm.llm.model'],
    ['arcrun', ['max_turns'], 'settings.arcrun.max_turns'],
    ['arcagent', ['agent', 'name'], 'settings.arcagent.agent.name'],
    ['arcagent', ['modules', 'browser', 'config', 'browser_use', 'max_steps'], 'settings.arcagent.modules.browser.config.browser_use.max_steps'],
    ['arcllm', ['defaults', 'provider'], 'settings.arcllm.defaults.provider'],
    ['gateway', ['gateway', 'tier'], 'settings.gateway.gateway.tier'],
  ])('resolves the serialized %s path %j to %s', (file, path, key) => {
    expect(configHelpKey(file, path)).toBe(key)
    expect(fieldHelp(key, 'settings')?.description).toBeTruthy()
  })

  it.each([
    ['arcllm', ['providers', 'openai', 'provider', 'base_url'], 'settings.arcllm.providers.*.provider.base_url'],
    ['arcagent', ['tools', 'mcp_servers', 'lookup', 'command'], 'settings.arcagent.tools.mcp_servers.*.command'],
    ['arcagent', ['modules', 'memory', 'enabled'], 'settings.arcagent.modules.*.enabled'],
    ['arcllm', ['providers', 'openai', 'models', 'gpt-5', 'context_window'], 'settings.arcllm.providers.*.models.*.context_window'],
  ])('matches named entries for %s path %j', (file, path, key) => {
    expect(configHelpKey(file, path)).toBe(key)
  })

  it('gives an exact key priority over a wildcard', () => {
    expect(configHelpKey('arcagent', ['modules', 'browser', 'config', 'browser_use', 'max_steps']))
      .toBe('settings.arcagent.modules.browser.config.browser_use.max_steps')
  })

  it('does not strip an unrelated first segment or let a wildcard span segments', () => {
    expect(configHelpKey('arcllm', ['unrelated', 'llm', 'model'])).toBe('settings.config_value')
    expect(configHelpKey('arcllm', ['providers', 'one', 'nested', 'provider', 'base_url']))
      .toBe('settings.unknown_extension_field')
    expect(configHelpKey('arcagent', ['tools', 'mcp_servers', 'one', 'nested', 'command']))
      .toBe('settings.config_value')
  })

  it('uses the unavailable description for unknown extension module fields', () => {
    expect(configHelpKey('arcagent', ['modules', 'third_party', 'config', 'custom']))
      .toBe('settings.unknown_extension_field')
    expect(configHelpKey('arcagent', ['modules', 'browser', 'config', 'custom']))
      .toBe('settings.config_value')
  })

  it('documents queue tenant scope, revisions, limits, and cancellation outcomes', () => {
    expect(helpRoute('/queue')).toBe('queue')
    const queue = content.queue
    expect(queue.fields.map((field) => field.key)).toEqual([
      'queue.jobs.state',
      'queue.control.paused',
      'queue.limits.max_concurrent',
      'queue.limits.max_queued',
      'queue.limits.wait_timeout',
      'queue.limits.history_limit',
      'queue.cancel.status',
    ])
    expect(queue.fields.find((field) => field.key === 'queue.jobs.state')?.description).toContain('authenticated tenant scope')
    expect(queue.fields.find((field) => field.key === 'queue.control.paused')?.description).toContain('refresh controls')
    expect(queue.fields.find((field) => field.key === 'queue.limits.max_concurrent')?.description).toContain('at least 1')
    expect(queue.fields.find((field) => field.key === 'queue.limits.max_queued')?.description).toContain('Zero allows no waiting calls')
    expect(queue.fields.find((field) => field.key === 'queue.limits.wait_timeout')?.description).toContain('seconds')
    expect(queue.fields.find((field) => field.key === 'queue.limits.history_limit')?.description).toContain('Positive integer')
    expect(queue.fields.find((field) => field.key === 'queue.cancel.status')?.description).toContain('does not undo external effects')
  })

  it('documents the hosted first-account setup journey and safe claim recovery', () => {
    const setup = content.setup
    expect(setup.title).toBe('Create your account')
    expect(setup.fields.map((field) => field.key)).toEqual([
      'setup.status',
      'setup.password',
      'setup.password_confirmation',
      'setup.create_account',
      'setup.sign_in',
    ])
    expect(setup.fields.find((field) => field.key === 'setup.password')?.description).toContain('at least 12 characters')
    expect(setup.fields.find((field) => field.key === 'setup.password_confirmation')?.description).toContain('must match')
    expect(setup.fields.find((field) => field.key === 'setup.create_account')?.description).toContain('verified order page')
    expect(setup.troubleshooting[2].action).toContain('Do not try a manual account bootstrap')
  })

  it('keeps every screen field ID unique', () => {
    const fields = Object.values(content).flatMap((screen) => screen.fields)
    expect(new Set(fields.map((field) => field.key)).size).toBe(fields.length)
  })

  it.each(staticHelpUses)('resolves live FieldHelp usage $key in $file', ({ file, key, route }) => {
    const entry = route ? fieldHelp(key, route) : fieldHelp(key)
    expect(entry, `${file} references missing help ID ${key}${route ? ` on ${route}` : ''}`).toBeTruthy()
    expect(entry?.description.trim()).not.toBe('')
  })

  it('maps the rendered agent and workflow detail routes to their shared help entries', () => {
    expect(helpRoute('/agents/agent-123/inbox')).toBe('agents/:id')
    expect(helpRoute('/workflows/workflow-123')).toBe('workflows/:id')
    expect(helpRoute('/unrecognized')).toBeNull()
  })

  it('places optional table-search help beside the search field without changing generic tables', async () => {
    const columns = [{ accessorKey: 'name', header: 'Name' }]
    const { unmount } = render(createElement(DataTable, {
      columns,
      data: [{ name: 'call one' }],
      searchable: true,
      searchPlaceholder: 'Search calls…',
      searchHelpKey: 'trace.search',
    }))
    expect(screen.getByPlaceholderText('Search calls…')).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Help for Search model calls' }))
    expect(screen.getByText(content.arcllm.fields.find((field) => field.key === 'trace.search')!.description)).toBeTruthy()
    unmount()

    const generic = render(createElement(DataTable, { columns, data: [{ name: 'call one' }], searchable: true }))
    expect(screen.queryByRole('button', { name: 'Help for Search model calls' })).toBeNull()
    generic.unmount()
  })
})
