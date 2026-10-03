import { expect, it } from 'vitest'

// Source-level guard for the 375px layout rules. jsdom has no layout engine, so
// these assert the class contracts that keep a screen from scrolling sideways.
const RAW = import.meta.glob(['/src/**/*.tsx', '!/src/**/*.test.tsx'], {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

const FILES = Object.entries(RAW).map(([path, text]) => ({ path: path.slice('/src/'.length), text }))

it('scans the real source tree', () => {
  expect(FILES.length).toBeGreaterThan(50)
})

it('never clips a raw table: its wrapper scrolls instead of hiding overflow', () => {
  const offenders: string[] = []
  for (const { path, text } of FILES) {
    const lines = text.split('\n')
    lines.forEach((line, i) => {
      if (!/<table\b/.test(line) || path.endsWith('ui/table.tsx')) return
      const wrapper = lines.slice(Math.max(0, i - 2), i).join('\n')
      if (!/overflow-x-auto/.test(wrapper)) offenders.push(`${path}:${i + 1}`)
    })
  }
  expect(offenders).toEqual([])
})

it('uses no fixed pixel width wider than a 375px phone without a breakpoint', () => {
  const offenders: string[] = []
  for (const { path, text } of FILES) {
    for (const m of text.matchAll(/(?<![:\w\-[])(?:w|min-w)-\[(\d+)px\]/g)) {
      if (Number(m[1]) > 375) offenders.push(`${path}: ${m[0]}`)
    }
  }
  expect(offenders).toEqual([])
})

it('sizes full-height frames with dvh so the iOS toolbar cannot hide the composer', () => {
  const offenders = FILES.filter(({ text }) => /\bh-screen\b/.test(text)).map(({ path }) => path)
  expect(offenders).toEqual([])
})

it('keeps page gutters responsive: no unprefixed 24px page padding', () => {
  const offenders: string[] = []
  for (const { path, text } of FILES) {
    if (!path.startsWith('pages/')) continue
    if (/(?<![:\w\-[])(?:p|px)-6(?![\w\-/\]])/.test(text)) offenders.push(path)
  }
  expect(offenders).toEqual([])
})
