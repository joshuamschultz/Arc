import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import css from 'highlight.js/lib/languages/css'
import ini from 'highlight.js/lib/languages/ini'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import markdown from 'highlight.js/lib/languages/markdown'
import python from 'highlight.js/lib/languages/python'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'

// Register only the languages Arc actually renders (tool source, skill bodies,
// config), keeping the bundle lean vs. the full highlight.js language pack.
// `ini` covers TOML well enough; `xml` backs HTML/JSX markup.
const LANGUAGES: Record<string, Parameters<typeof hljs.registerLanguage>[1]> = {
  bash,
  css,
  ini,
  javascript,
  json,
  markdown,
  python,
  typescript,
  xml,
  yaml,
}
for (const [name, lang] of Object.entries(LANGUAGES)) hljs.registerLanguage(name, lang)

const EXTENSION_LANGUAGE: Record<string, string> = {
  py: 'python',
  sh: 'bash',
  bash: 'bash',
  zsh: 'bash',
  json: 'json',
  js: 'javascript',
  jsx: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  toml: 'ini',
  ini: 'ini',
  cfg: 'ini',
  yaml: 'yaml',
  yml: 'yaml',
  md: 'markdown',
  markdown: 'markdown',
  html: 'xml',
  xml: 'xml',
  css: 'css',
}

/** Resolve a highlight.js language from a fenced-code info string or file path. */
export function inferLanguage(hint?: string | null): string | undefined {
  if (!hint) return undefined
  const token = hint.split('/').pop()?.split('.').pop()?.toLowerCase() ?? ''
  if (token in EXTENSION_LANGUAGE) return EXTENSION_LANGUAGE[token]
  const lowered = hint.toLowerCase()
  return hljs.getLanguage(lowered) ? lowered : EXTENSION_LANGUAGE[lowered]
}

/**
 * Highlight `code` to token HTML. A known `language` is used directly; an
 * unknown/absent one falls back to auto-detection so the block is still
 * highlighted, never raw. Returns null if highlighting throws (caller renders
 * the escaped source). highlight.js escapes its input, so the HTML is safe
 * token markup — no raw source HTML passes through (LLM05).
 */
export function highlightToHtml(code: string, language?: string): string | null {
  try {
    if (language && hljs.getLanguage(language)) {
      return hljs.highlight(code, { language }).value
    }
    return hljs.highlightAuto(code).value
  } catch {
    return null
  }
}
