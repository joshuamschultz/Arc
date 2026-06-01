import { useState } from 'react'
import {
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from '@tanstack/react-table'
import { ArrowDown, ArrowUp, ChevronsUpDown, Search } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import { EmptyState } from '@/components/states'

interface DataTableProps<T> {
  columns: ColumnDef<T, unknown>[]
  data: T[]
  /** Enable the client-side search box (matches across all cells). */
  searchable?: boolean
  searchPlaceholder?: string
  onRowClick?: (row: T) => void
  /** Marks a row visually selected (e.g. open in a detail drawer). */
  isRowActive?: (row: T) => boolean
  emptyTitle?: string
  emptyDescription?: string
}

/**
 * Reusable table built on TanStack Table. Replaces the old `log-table.js`.
 * Client-side sort/filter today; column defs accept server-driven data too,
 * so high-volume tables (ArcLLM calls) can push sort/filter to the backend
 * later without changing call sites (plan §storage-evolution).
 */
export function DataTable<T>({
  columns,
  data,
  searchable,
  searchPlaceholder = 'Search…',
  onRowClick,
  isRowActive,
  emptyTitle = 'No rows',
  emptyDescription,
}: DataTableProps<T>) {
  const [sorting, setSorting] = useState<SortingState>([])
  const [globalFilter, setGlobalFilter] = useState('')

  // React Compiler can't analyze TanStack Table's hook; we don't run the
  // compiler in the Vite build, so this advisory is informational only.
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable({
    data,
    columns,
    state: { sorting, globalFilter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  })

  const rows = table.getRowModel().rows

  return (
    <div className="flex flex-col gap-3">
      {searchable && (
        <div className="relative w-full max-w-xs">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={globalFilter}
            onChange={(e) => setGlobalFilter(e.target.value)}
            placeholder={searchPlaceholder}
            className="pl-8"
          />
        </div>
      )}

      <div className="overflow-hidden rounded-xl border border-border bg-card">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 z-10 bg-muted/40 backdrop-blur">
              {table.getHeaderGroups().map((hg) => (
                <tr key={hg.id} className="border-b border-border">
                  {hg.headers.map((h) => {
                    const sortable = h.column.getCanSort()
                    const dir = h.column.getIsSorted()
                    return (
                      <th
                        key={h.id}
                        className={cn(
                          'px-3 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground',
                          sortable && 'cursor-pointer select-none hover:text-foreground',
                        )}
                        onClick={sortable ? h.column.getToggleSortingHandler() : undefined}
                      >
                        <span className="inline-flex items-center gap-1">
                          {flexRender(h.column.columnDef.header, h.getContext())}
                          {sortable &&
                            (dir === 'asc' ? (
                              <ArrowUp className="size-3" />
                            ) : dir === 'desc' ? (
                              <ArrowDown className="size-3" />
                            ) : (
                              <ChevronsUpDown className="size-3 opacity-40" />
                            ))}
                        </span>
                      </th>
                    )
                  })}
                </tr>
              ))}
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.id}
                  onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                  className={cn(
                    'border-b border-border/60 last:border-0 transition-colors',
                    onRowClick && 'cursor-pointer hover:bg-muted/40',
                    isRowActive?.(row.original) && 'bg-primary/10',
                  )}
                >
                  {row.getVisibleCells().map((cell) => (
                    <td key={cell.id} className="px-3 py-2 align-middle">
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {rows.length === 0 && (
          <div className="p-6">
            <EmptyState title={emptyTitle} description={emptyDescription} />
          </div>
        )}
      </div>
    </div>
  )
}
