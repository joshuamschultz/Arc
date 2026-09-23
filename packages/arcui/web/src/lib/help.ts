import content from '@/content/screen-help.json'

export interface FieldHelpEntry {
  key: string
  label: string
  description: string
  example?: string
}

export interface ScreenHelpEntry {
  title: string
  summary: string
  steps: string[]
  fields: FieldHelpEntry[]
  troubleshooting: { symptom: string; action: string }[]
}

const screens = content as Record<string, ScreenHelpEntry>

export function helpRoute(pathname: string): string | null {
  const parts = pathname.split('/').filter(Boolean)
  if (parts.length >= 2 && (parts[0] === 'agents' || parts[0] === 'workflows')) {
    return `${parts[0]}/:id`
  }
  return parts[0] in screens ? parts[0] : null
}

export function screenHelp(pathname: string): ScreenHelpEntry | null {
  const route = helpRoute(pathname)
  return route ? screens[route] : null
}

export function fieldHelp(key: string, route?: string): FieldHelpEntry | null {
  if (route) return screens[route]?.fields.find((field) => field.key === key) ?? null
  for (const screen of Object.values(screens)) {
    const field = screen.fields.find((entry) => entry.key === key)
    if (field) return field
  }
  return null
}

const CONFIG_HELP_KEYS = new Set(screens.settings.fields.map((field) => field.key))
const CONFIG_PATTERNS = screens.settings.fields
  .filter((field) => field.key.includes('*'))
  .map((field) => ({ key: field.key, segments: field.key.split('.') }))
  .sort((a, b) => a.segments.filter((segment) => segment === '*').length - b.segments.filter((segment) => segment === '*').length)

const SHIPPED_MODULES = new Set(
  screens.settings.fields.flatMap((field) => {
    const segments = field.key.split('.')
    return segments[1] === 'arcagent' && segments[2] === 'modules' && segments[3] !== '*'
      ? [segments[3]] : []
  }),
)

export function configHelpKey(file: string, path: string[]): string {
  const segments = ['settings', file, ...path]
  const exact = segments.join('.')
  if (CONFIG_HELP_KEYS.has(exact)) return exact

  const pattern = CONFIG_PATTERNS.find(({ segments: candidate }) =>
    candidate.length === segments.length &&
    candidate.every((segment, index) => segment === '*' || segment === segments[index]),
  )
  if (pattern) return pattern.key

  if (path.includes('extensions') ||
    (file === 'arcagent' && path[0] === 'modules' && !SHIPPED_MODULES.has(path[1])) ||
    (file === 'arcllm' && path[0] === 'providers')) {
    return 'settings.unknown_extension_field'
  }
  return 'settings.config_value'
}
