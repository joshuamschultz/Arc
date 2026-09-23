import { describe, expect, it } from 'vitest'
import { configHelpKey, fieldHelp } from '@/lib/help'
import content from '@/content/screen-help.json'

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

  it('keeps every screen field ID unique', () => {
    const fields = Object.values(content).flatMap((screen) => screen.fields)
    expect(new Set(fields.map((field) => field.key)).size).toBe(fields.length)
  })
})
