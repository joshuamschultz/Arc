// Turn a dropped or picked skill FOLDER into one ZIP the import route accepts
// (J4 M3/G9). The server does all the trust work — layout, path safety, limits,
// binary refusal — on the bytes it receives; this only packages what the
// operator chose, so it stays a minimal, dependency-free store-only writer.

export interface PackEntry {
  /** Folder-relative POSIX path, starting with the dropped folder's name. */
  path: string
  file: File
}

export interface ZipEntry {
  path: string
  data: Uint8Array
}

/** Hard ceiling so a stray drop of a huge tree cannot hang the tab. */
export const MAX_PACK_FILES = 512

const JUNK_DIRS = new Set(['__MACOSX'])
const JUNK_FILES = new Set(['.DS_Store', 'Thumbs.db', 'desktop.ini'])

/** Archive-manager noise the server would drop anyway; never upload it. */
export function isPackJunk(path: string): boolean {
  const parts = path.split('/')
  return parts.some((part) => JUNK_DIRS.has(part)) || JUNK_FILES.has(parts[parts.length - 1] ?? '')
}

const CRC_TABLE = (() => {
  const table = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[n] = c >>> 0
  }
  return table
})()

function crc32(data: Uint8Array): number {
  let crc = 0xffffffff
  for (const byte of data) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8)
  return (crc ^ 0xffffffff) >>> 0
}

function assertSafePath(path: string): void {
  const parts = path.split('/')
  if (!path || path.startsWith('/') || path.includes('\\') || parts.some((p) => p === '' || p === '.' || p === '..')) {
    throw new Error(`unsafe path in folder: ${path}`)
  }
}

// DOS date for 1980-01-01 00:00 — a fixed stamp keeps the archive (and so the
// server's content-addressed import id) identical for identical files.
const DOS_TIME = 0
const DOS_DATE = (1 << 5) | 1
const UTF8_FLAG = 0x0800

/** Build a deterministic store-only (no compression) ZIP, entries sorted by path. */
export function buildStoreZip(entries: ZipEntry[]): Uint8Array {
  const sorted = [...entries].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0))
  const encoder = new TextEncoder()
  const locals: Uint8Array[] = []
  const centrals: Uint8Array[] = []
  let offset = 0
  for (const entry of sorted) {
    assertSafePath(entry.path)
    const name = encoder.encode(entry.path)
    const crc = crc32(entry.data)
    const size = entry.data.length
    const local = new Uint8Array(30 + name.length)
    const lv = new DataView(local.buffer)
    lv.setUint32(0, 0x04034b50, true)
    lv.setUint16(4, 20, true)
    lv.setUint16(6, UTF8_FLAG, true)
    lv.setUint16(8, 0, true)
    lv.setUint16(10, DOS_TIME, true)
    lv.setUint16(12, DOS_DATE, true)
    lv.setUint32(14, crc, true)
    lv.setUint32(18, size, true)
    lv.setUint32(22, size, true)
    lv.setUint16(26, name.length, true)
    lv.setUint16(28, 0, true)
    local.set(name, 30)
    const central = new Uint8Array(46 + name.length)
    const cv = new DataView(central.buffer)
    cv.setUint32(0, 0x02014b50, true)
    cv.setUint16(4, 20, true)
    cv.setUint16(6, 20, true)
    cv.setUint16(8, UTF8_FLAG, true)
    cv.setUint16(10, 0, true)
    cv.setUint16(12, DOS_TIME, true)
    cv.setUint16(14, DOS_DATE, true)
    cv.setUint32(16, crc, true)
    cv.setUint32(20, size, true)
    cv.setUint32(24, size, true)
    cv.setUint16(28, name.length, true)
    cv.setUint32(42, offset, true)
    central.set(name, 46)
    locals.push(local, entry.data)
    centrals.push(central)
    offset += local.length + size
  }
  const centralSize = centrals.reduce((sum, part) => sum + part.length, 0)
  const end = new Uint8Array(22)
  const ev = new DataView(end.buffer)
  ev.setUint32(0, 0x06054b50, true)
  ev.setUint16(8, sorted.length, true)
  ev.setUint16(10, sorted.length, true)
  ev.setUint32(12, centralSize, true)
  ev.setUint32(16, offset, true)
  const out = new Uint8Array(offset + centralSize + end.length)
  let at = 0
  for (const part of [...locals, ...centrals, end]) {
    out.set(part, at)
    at += part.length
  }
  return out
}

/** Entries from an ``<input webkitdirectory>`` pick (paths from webkitRelativePath). */
export function collectFromFileList(files: FileList | File[]): PackEntry[] {
  return Array.from(files)
    .map((file) => ({ path: file.webkitRelativePath || file.name, file }))
    .filter((entry) => !isPackJunk(entry.path))
}

// The File and Directory Entries API surface we use — typed locally because
// jsdom and some lib.dom versions do not ship these shapes.
interface FsEntry {
  isFile: boolean
  isDirectory: boolean
  name: string
  fullPath: string
}
interface FsFileEntry extends FsEntry {
  file(resolve: (file: File) => void, reject?: (error: unknown) => void): void
}
interface FsDirectoryEntry extends FsEntry {
  createReader(): { readEntries(resolve: (entries: FsEntry[]) => void, reject?: (error: unknown) => void): void }
}
interface EntryItem {
  kind: string
  webkitGetAsEntry?: () => FsEntry | null
}

async function walk(entry: FsEntry, out: PackEntry[]): Promise<void> {
  const path = entry.fullPath.replace(/^\/+/, '')
  if (isPackJunk(path)) return
  if (entry.isFile) {
    if (out.length >= MAX_PACK_FILES) throw new Error(`folder has more than ${MAX_PACK_FILES} files`)
    const file = await new Promise<File>((resolve, reject) => (entry as FsFileEntry).file(resolve, reject))
    out.push({ path, file })
    return
  }
  if (!entry.isDirectory) return
  const reader = (entry as FsDirectoryEntry).createReader()
  // readEntries returns batches; an empty batch means the directory is done.
  for (;;) {
    const batch = await new Promise<FsEntry[]>((resolve, reject) => reader.readEntries(resolve, reject))
    if (batch.length === 0) break
    for (const child of batch) await walk(child, out)
  }
}

/**
 * Entries for a drop that contains a FOLDER, or ``null`` when the drop is plain
 * files (a ZIP or SKILL.md) that should upload unchanged.
 */
export async function collectFromDrop(items: ArrayLike<EntryItem> | undefined): Promise<PackEntry[] | null> {
  const roots = Array.from(items ?? [])
    .filter((item) => item.kind === 'file' && typeof item.webkitGetAsEntry === 'function')
    .map((item) => item.webkitGetAsEntry?.() ?? null)
    .filter((entry): entry is FsEntry => entry !== null)
  if (!roots.some((entry) => entry.isDirectory)) return null
  const out: PackEntry[] = []
  for (const root of roots) await walk(root, out)
  return out
}

async function bytesOf(file: File): Promise<Uint8Array> {
  return new Uint8Array(await file.arrayBuffer())
}

/** Zip a collected folder into ``<folder>.zip`` for the existing upload route. */
export async function packFolder(entries: PackEntry[]): Promise<File> {
  if (entries.length === 0) throw new Error('the folder is empty')
  const roots = new Set(entries.map((entry) => entry.path.split('/')[0]))
  const name = roots.size === 1 ? `${[...roots][0]}.zip` : 'skill-pack.zip'
  const zipEntries = await Promise.all(entries.map(async (entry) => ({ path: entry.path, data: await bytesOf(entry.file) })))
  const zip = buildStoreZip(zipEntries)
  return new File([zip.buffer as ArrayBuffer], name, { type: 'application/zip' })
}
