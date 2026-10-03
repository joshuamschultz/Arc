import { describe, expect, it } from 'vitest'
import { readTables, setField } from '@/lib/semantic-layer-toml'

const LAYER = `# Semantic layer for the "sales" datastore - YOURS TO EDIT.
classification = "internal"

[table.invoices]
entity = "invoice"
description = ""
hidden = false
searchable = ["memo"]

[table.invoices.column.amt]
label = "amt"
description = ""
samples = ["19.99", "5"]

[table."order lines"]
entity = "line"
hidden = true
`

describe('semantic layer TOML', () => {
  it('reads every table and column the file describes', () => {
    const tables = readTables(LAYER)
    expect(tables.map((t) => t.name)).toEqual(['invoices', 'order lines'])
    expect(tables[0]).toMatchObject({ entity: 'invoice', hidden: false })
    expect(tables[0].columns).toEqual([{ name: 'amt', label: 'amt', description: '' }])
    expect(tables[1]).toMatchObject({ entity: 'line', hidden: true })
  })

  it('changes only the line it means to change', () => {
    const next = setField(LAYER, { table: 'invoices', column: 'amt' }, 'description', 'Amount in US dollars')
    expect(next).toBe(LAYER.replace('description = ""\nsamples', 'description = "Amount in US dollars"\nsamples'))
    expect(next).toContain('samples = ["19.99", "5"]')
    expect(next).toContain('# Semantic layer for the "sales" datastore')
  })

  it('adds a missing key under the right header and flips booleans', () => {
    const next = setField(LAYER, { table: 'order lines', column: null }, 'description', 'One row per product')
    expect(readTables(next)[1].description).toBe('One row per product')
    expect(readTables(setField(next, { table: 'order lines', column: null }, 'hidden', false))[1].hidden).toBe(false)
  })

  it('round-trips quotes and finds quoted table names', () => {
    const next = setField(LAYER, { table: 'order lines', column: null }, 'entity', 'a "line" item')
    expect(readTables(next)[1].entity).toBe('a "line" item')
  })

  it('never invents a table the file does not have', () => {
    expect(setField(LAYER, { table: 'ghosts', column: null }, 'entity', 'x')).toBe(LAYER)
  })
})
