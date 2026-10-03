// A line-level reader and editor for the one TOML shape a datastore's semantic
// layer uses. The file is hand edited and carries things the friendly editor does
// not show (comments, sample values, searchable columns), so an edit changes ONLY
// the line it means to change and leaves every other byte alone. That is also what
// keeps the raw view and the friendly view saying the same thing: one text, two
// ways to edit it.

export interface ColumnMeaning {
  name: string
  label: string
  description: string
}

export interface TableMeaning {
  name: string
  entity: string
  description: string
  hidden: boolean
  columns: ColumnMeaning[]
}

type Target = { table: string; column: string | null }
type Block = Target & { headerLine: number; endLine: number }

const HEADER = /^\s*\[(?!\[)(.+)\]\s*(?:#.*)?$/
const KEY_LINE = /^(\s*)([A-Za-z_][\w-]*)\s*=\s*(.*)$/

/** Split a dotted key path, honouring quoted segments ("a.b".c -> ["a.b", "c"]). */
function splitPath(raw: string): string[] | null {
  const parts: string[] = []
  let i = 0
  while (i < raw.length) {
    while (raw[i] === ' ') i++
    if (raw[i] === '"') {
      let j = i + 1
      while (j < raw.length && raw[j] !== '"') j += raw[j] === '\\' ? 2 : 1
      try {
        parts.push(JSON.parse(raw.slice(i, j + 1)) as string)
      } catch {
        return null
      }
      i = j + 1
    } else {
      let j = i
      while (j < raw.length && raw[j] !== '.') j++
      parts.push(raw.slice(i, j).trim())
      i = j
    }
    while (raw[i] === ' ') i++
    if (raw[i] === '.') i++
    else if (i < raw.length) return null
  }
  return parts
}

function targetOf(path: string[]): Target | null {
  if (path[0] !== 'table') return null
  if (path.length === 2) return { table: path[1], column: null }
  if (path.length === 4 && path[2] === 'column') return { table: path[1], column: path[3] }
  return null
}

function blocksOf(lines: string[]): Block[] {
  const blocks: Block[] = []
  lines.forEach((line, index) => {
    const match = HEADER.exec(line)
    if (!match) return
    const last = blocks[blocks.length - 1]
    if (last) last.endLine = index
    const path = splitPath(match[1])
    const target = path ? targetOf(path) : null
    // Any header, even one that is not a table or column, ends the block before it.
    if (target) blocks.push({ ...target, headerLine: index, endLine: lines.length })
  })
  return blocks
}

function readString(raw: string): string {
  const text = raw.trim()
  if (!text.startsWith('"')) return ''
  let j = 1
  while (j < text.length && text[j] !== '"') j += text[j] === '\\' ? 2 : 1
  try {
    return JSON.parse(text.slice(0, j + 1)) as string
  } catch {
    return ''
  }
}

function fieldsIn(lines: string[], block: Block): Record<string, string> {
  const fields: Record<string, string> = {}
  for (let i = block.headerLine + 1; i < block.endLine; i++) {
    const match = KEY_LINE.exec(lines[i])
    if (match) fields[match[2]] = match[3]
  }
  return fields
}

/** Every table (and its columns) the layer describes, in file order. */
export function readTables(content: string): TableMeaning[] {
  const lines = content.split('\n')
  const tables = new Map<string, TableMeaning>()
  for (const block of blocksOf(lines)) {
    const fields = fieldsIn(lines, block)
    let table = tables.get(block.table)
    if (!table) {
      table = { name: block.table, entity: '', description: '', hidden: false, columns: [] }
      tables.set(block.table, table)
    }
    if (block.column === null) {
      table.entity = readString(fields.entity ?? '')
      table.description = readString(fields.description ?? '')
      table.hidden = /^true\b/.test((fields.hidden ?? '').trim())
    } else {
      table.columns.push({
        name: block.column,
        label: readString(fields.label ?? ''),
        description: readString(fields.description ?? ''),
      })
    }
  }
  return [...tables.values()]
}

function encode(value: string | boolean): string {
  return typeof value === 'boolean' ? String(value) : JSON.stringify(value)
}

/** Set one field on one table or column, changing only that line. A block the
 *  file does not have is left alone: an edit never invents a table. */
export function setField(
  content: string,
  target: Target,
  key: 'entity' | 'description' | 'hidden' | 'label',
  value: string | boolean,
): string {
  const lines = content.split('\n')
  const block = blocksOf(lines).find((b) => b.table === target.table && b.column === target.column)
  if (!block) return content
  for (let i = block.headerLine + 1; i < block.endLine; i++) {
    const match = KEY_LINE.exec(lines[i])
    if (match && match[2] === key) {
      lines[i] = `${match[1]}${key} = ${encode(value)}`
      return lines.join('\n')
    }
  }
  lines.splice(block.headerLine + 1, 0, `${key} = ${encode(value)}`)
  return lines.join('\n')
}
