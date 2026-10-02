import { describe, expect, it } from 'vitest'
import { buildStoreZip, collectFromFileList, isPackJunk, packFolder } from './skill-pack'

const enc = new TextEncoder()

function u32(bytes: Uint8Array, offset: number): number {
  return new DataView(bytes.buffer, bytes.byteOffset).getUint32(offset, true)
}

function u16(bytes: Uint8Array, offset: number): number {
  return new DataView(bytes.buffer, bytes.byteOffset).getUint16(offset, true)
}

/** Walk the local file headers of a store-only ZIP — enough to prove the format. */
function readLocalEntries(zip: Uint8Array) {
  const out: Array<{ name: string; crc: number; data: string }> = []
  let at = 0
  while (u32(zip, at) === 0x04034b50) {
    const crc = u32(zip, at + 14)
    const size = u32(zip, at + 18)
    const nameLength = u16(zip, at + 26)
    const extraLength = u16(zip, at + 28)
    const name = new TextDecoder().decode(zip.slice(at + 30, at + 30 + nameLength))
    const start = at + 30 + nameLength + extraLength
    out.push({ name, crc, data: new TextDecoder().decode(zip.slice(start, start + size)) })
    at = start + size
  }
  return { entries: out, centralAt: at }
}

describe('buildStoreZip', () => {
  it('writes readable store entries with a correct CRC-32, sorted by path', () => {
    const zip = buildStoreZip([
      { path: 'b/second.md', data: enc.encode('second') },
      { path: 'a/hello.txt', data: enc.encode('hello') },
    ])
    const { entries, centralAt } = readLocalEntries(zip)
    expect(entries.map((e) => e.name)).toEqual(['a/hello.txt', 'b/second.md'])
    expect(entries[0].data).toBe('hello')
    expect(entries[0].crc).toBe(0x3610a686)
    expect(u32(zip, centralAt)).toBe(0x02014b50)
    const eocd = zip.length - 22
    expect(u32(zip, eocd)).toBe(0x06054b50)
    expect(u16(zip, eocd + 10)).toBe(2)
  })

  it('is deterministic for the same files', () => {
    const files = [{ path: 'x/SKILL.md', data: enc.encode('---\nname: x\n---\n') }]
    expect(buildStoreZip(files)).toEqual(buildStoreZip(files))
  })

  it('refuses an unsafe entry path', () => {
    expect(() => buildStoreZip([{ path: '../evil', data: enc.encode('x') }])).toThrow()
    expect(() => buildStoreZip([{ path: '/abs', data: enc.encode('x') }])).toThrow()
  })
})

describe('folder packing', () => {
  it('drops platform junk', () => {
    expect(isPackJunk('pdf/.DS_Store')).toBe(true)
    expect(isPackJunk('__MACOSX/pdf/._SKILL.md')).toBe(true)
    expect(isPackJunk('pdf/SKILL.md')).toBe(false)
  })

  it('zips a picked folder under its own name', async () => {
    const skill = new File(['---\nname: pdf\n---\n'], 'SKILL.md')
    Object.defineProperty(skill, 'webkitRelativePath', { value: 'pdf/SKILL.md' })
    const junk = new File(['x'], '.DS_Store')
    Object.defineProperty(junk, 'webkitRelativePath', { value: 'pdf/.DS_Store' })

    const entries = collectFromFileList([skill, junk])
    expect(entries.map((e) => e.path)).toEqual(['pdf/SKILL.md'])
    const packed = await packFolder(entries)
    expect(packed.name).toBe('pdf.zip')
    const { entries: zipped } = readLocalEntries(new Uint8Array(await packed.arrayBuffer()))
    expect(zipped.map((e) => e.name)).toEqual(['pdf/SKILL.md'])
  })
})
