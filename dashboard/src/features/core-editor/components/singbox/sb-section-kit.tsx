import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { CoreEditorDataTable } from '@/features/core-editor/components/shared/core-editor-data-table'
import { CoreEditorFormDialog } from '@/features/core-editor/components/shared/core-editor-form-dialog'
import { useSectionHeaderAddPulseEffect, type SectionHeaderAddPulse } from '@/features/core-editor/hooks/use-section-header-add-pulse'
import { useCoreEditorStore, type SbCoreSection } from '@/features/core-editor/state/core-editor-store'
import { cn } from '@/lib/utils'
import { arrayMove } from '@dnd-kit/sortable'
import type { ColumnDef } from '@tanstack/react-table'
import type { SingBoxCoreDraft } from '@pasarguard/singbox-config-kit'
import { ChevronDown, Pencil, Plus } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

/** A wire-shaped row (outbound / route rule / rule-set / dns server / dns rule). */
export type WireRow = Record<string, unknown>

export interface SbListSectionProps<T extends WireRow> {
  headerAddPulse?: SectionHeaderAddPulse
  headerAddEpoch?: number
  /** Which nav section this is — the page-header "+ Add" pulse is keyed on it. */
  sectionId: SbCoreSection
  rows: readonly T[]
  onRowsChange: (rows: T[]) => void
  columns: ColumnDef<T, unknown>[]
  getSearchableText?: (row: T) => string
  searchPlaceholder?: string
  emptyLabel?: string
  minRowCount?: number
  minRowCountMessage?: string
  /** Either a single default factory (single "+ Add" button) or a typed menu. */
  createRow: (type?: string) => T
  addTypes?: readonly string[]
  addTypeLabel?: (type: string) => string
  /** Single "+ Add" toolbar button (used when there is no type menu and no page-header pulse). */
  singleAddLabel?: string
  reorder?: boolean
  dialogTitleAdd: string
  dialogTitleEdit: string
  dialogSize?: 'md' | 'lg' | 'xl'
  /** Render the dialog body for `row`; call `patch` with a whole new row on any change. */
  renderBody: (row: T, patch: (next: T) => void) => React.ReactNode
}

/**
 * The shared list-shell + add/edit-dialog machine for every sing-box wire-object section.
 * Rows are plain JSON objects (exactly the sing-box wire shape); edits are live in edit mode
 * and buffered in add mode, mirroring the Xray sections. Persist validation is sing-box's own
 * (Monaco + save-time), so the Xray inline persist-validation is disabled here.
 */
export function SbListSection<T extends WireRow>(props: SbListSectionProps<T>) {
  const { t } = useTranslation()
  const { rows, onRowsChange, columns, createRow, addTypes, sectionId, headerAddPulse, headerAddEpoch } = props

  const [selected, setSelected] = useState(0)
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<'add' | 'edit'>('edit')
  const [draftRow, setDraftRow] = useState<T | null>(null)
  const initialRef = useRef<T | null>(null)
  const originalRef = useRef<T | null>(null)

  const row = mode === 'add' && draftRow ? draftRow : rows[selected]

  const finalizeClose = useCallback(() => {
    setOpen(false)
    setMode('edit')
    setDraftRow(null)
  }, [])

  const beginAdd = useCallback(
    (type?: string) => {
      const created = createRow(type)
      initialRef.current = created
      setDraftRow(created)
      setMode('add')
      setOpen(true)
    },
    [createRow],
  )

  useSectionHeaderAddPulseEffect(headerAddPulse, headerAddEpoch, sectionId, () => beginAdd(addTypes?.[0]))

  const patch = useCallback(
    (next: T) => {
      if (mode === 'add') {
        setDraftRow(next)
        return
      }
      onRowsChange(rows.map((r, i) => (i === selected ? next : r)))
    },
    [mode, onRowsChange, rows, selected],
  )

  const commitAdd = useCallback(() => {
    if (!draftRow) return
    onRowsChange([...rows, draftRow])
    setSelected(rows.length)
    finalizeClose()
  }, [draftRow, finalizeClose, onRowsChange, rows])

  const addMenu =
    addTypes && addTypes.length > 0 ? (
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="outline" size="sm" className="gap-1.5">
            <Plus className="size-4" />
            {t('add', { defaultValue: 'Add' })}
            <ChevronDown className="size-3.5 opacity-60" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="max-h-72 overflow-y-auto">
          {addTypes.map(type => (
            <DropdownMenuItem key={type} onClick={() => beginAdd(type)}>
              {props.addTypeLabel ? props.addTypeLabel(type) : type}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    ) : props.singleAddLabel ? (
      <Button type="button" variant="outline" size="sm" className="gap-1.5" onClick={() => beginAdd()}>
        <Plus className="size-4" />
        {props.singleAddLabel}
      </Button>
    ) : null

  return (
    <div className="space-y-6">
      <CoreEditorDataTable<T>
        columns={columns}
        data={rows as T[]}
        getRowId={(_r, i) => String(i)}
        getSearchableText={props.getSearchableText ?? (r => JSON.stringify(r))}
        searchPlaceholder={props.searchPlaceholder}
        emptyLabel={props.emptyLabel}
        minRowCount={props.minRowCount}
        minRowCountMessage={props.minRowCountMessage}
        toolbarActions={addMenu || undefined}
        onRowClick={(_r, rowIndex) => {
          setDraftRow(null)
          setMode('edit')
          setSelected(rowIndex)
          originalRef.current = rows[rowIndex] ?? null
          setOpen(true)
        }}
        onRemoveRow={i => {
          onRowsChange(rows.filter((_, idx) => idx !== i))
          setSelected(0)
        }}
        onBulkRemove={indices => {
          const rm = new Set(indices)
          onRowsChange(rows.filter((_, idx) => !rm.has(idx)))
          setSelected(0)
        }}
        enableReorder={props.reorder}
        onReorder={
          props.reorder
            ? (from, to) => {
                onRowsChange(arrayMove(rows as T[], from, to))
                setSelected(sel => (sel === from ? to : sel))
              }
            : undefined
        }
      />

      <CoreEditorFormDialog
        isDialogOpen={open}
        onOpenChange={next => (next ? setOpen(true) : finalizeClose())}
        inlinePersistValidation={false}
        initialData={mode === 'add' ? initialRef.current : originalRef.current}
        getCurrentData={() => (mode === 'add' ? draftRow : row)}
        leadingIcon={mode === 'add' ? <Plus className="h-5 w-5 shrink-0" /> : <Pencil className="h-5 w-5 shrink-0" />}
        title={mode === 'add' ? props.dialogTitleAdd : props.dialogTitleEdit}
        size={props.dialogSize ?? 'md'}
        footerExtra={
          mode === 'add' ? (
            <Button type="button" onClick={commitAdd}>
              {t('coreEditor.outbound.addToList', { defaultValue: 'Add to list' })}
            </Button>
          ) : (
            <Button type="button" onClick={finalizeClose}>
              {t('modify', { defaultValue: 'Done' })}
            </Button>
          )
        }
      >
        {row ? <div className="flex flex-col gap-4">{props.renderBody(row, patch)}</div> : null}
      </CoreEditorFormDialog>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Small field controls used by the section dialogs.
// ---------------------------------------------------------------------------

export function SbField({ label, hint, children, className }: { label: React.ReactNode; hint?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn('flex min-w-0 flex-col gap-1.5', className)}>
      <Label className="text-xs font-medium">{label}</Label>
      {children}
      {hint ? <p className="text-muted-foreground text-[11px] leading-relaxed">{hint}</p> : null}
    </div>
  )
}

export function SbTextField({ label, value, onChange, placeholder, hint, mono = true, className }: { label: React.ReactNode; value: unknown; onChange: (v: string) => void; placeholder?: string; hint?: React.ReactNode; mono?: boolean; className?: string }) {
  return (
    <SbField label={label} hint={hint} className={className}>
      <Input dir="ltr" className={cn('text-xs', mono && 'font-mono')} value={value == null ? '' : String(value)} placeholder={placeholder} onChange={e => onChange(e.target.value)} />
    </SbField>
  )
}

export function SbNumberField({ label, value, onChange, placeholder, hint, className }: { label: React.ReactNode; value: unknown; onChange: (v: number | undefined) => void; placeholder?: string; hint?: React.ReactNode; className?: string }) {
  return (
    <SbField label={label} hint={hint} className={className}>
      <Input
        dir="ltr"
        type="text"
        inputMode="numeric"
        className="font-mono text-xs"
        value={value == null ? '' : String(value)}
        placeholder={placeholder}
        onChange={e => {
          const raw = e.target.value.trim()
          onChange(raw === '' ? undefined : Number(raw))
        }}
      />
    </SbField>
  )
}

export function SbSelectField({ label, value, onChange, options, hint, className }: { label: React.ReactNode; value: unknown; onChange: (v: string) => void; options: readonly { value: string; label?: string }[]; hint?: React.ReactNode; className?: string }) {
  return (
    <SbField label={label} hint={hint} className={className}>
      <Select value={value == null ? '' : String(value)} onValueChange={onChange}>
        <SelectTrigger className="h-9 text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map(o => (
            <SelectItem key={o.value} value={o.value} className="text-xs">
              {o.label ?? o.value}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </SbField>
  )
}

export function SbSwitchRow({ label, hint, checked, onChange }: { label: React.ReactNode; hint?: React.ReactNode; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="flex items-start justify-between gap-3 rounded-md border px-3 py-2.5">
      <div className="min-w-0">
        <p className="text-xs font-medium">{label}</p>
        {hint ? <p className="text-muted-foreground text-[11px]">{hint}</p> : null}
      </div>
      <Switch checked={checked} onCheckedChange={v => onChange(v === true)} />
    </div>
  )
}

/**
 * Advanced escape hatch: edits the whole wire object as raw JSON. Because every section stores
 * exactly the sing-box wire shape, this exposes any field the structured controls don't model.
 */
export function SbRawJsonField<T extends WireRow>({ row, patch, label }: { row: T; patch: (next: T) => void; label?: React.ReactNode }) {
  const { t } = useTranslation()
  const [text, setText] = useState(() => JSON.stringify(row, null, 2))
  const [error, setError] = useState<string | null>(null)
  // Re-sync when a different row is opened (structured edits also flow in).
  const rowKey = JSON.stringify(row)
  const lastRowKey = useRef(rowKey)
  useEffect(() => {
    if (lastRowKey.current !== rowKey) {
      lastRowKey.current = rowKey
      setText(JSON.stringify(row, null, 2))
      setError(null)
    }
  }, [row, rowKey])

  return (
    <SbField label={label ?? t('coreEditor.singbox.rawJson', { defaultValue: 'Advanced (raw JSON)' })} hint={error ? undefined : t('coreEditor.singbox.rawJsonHint', { defaultValue: 'Full sing-box object. Edits merge into the entry; must stay valid JSON.' })}>
      <Textarea
        dir="ltr"
        spellCheck={false}
        className="min-h-40 font-mono text-[11px] leading-relaxed"
        value={text}
        onChange={e => {
          const next = e.target.value
          setText(next)
          try {
            const parsed = JSON.parse(next)
            if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
              lastRowKey.current = JSON.stringify(parsed)
              patch(parsed as T)
              setError(null)
            } else {
              setError(t('coreEditor.singbox.rawJsonObject', { defaultValue: 'Must be a JSON object.' }))
            }
          } catch (err) {
            setError((err as Error).message)
          }
        }}
      />
      {error ? <p className="text-destructive text-[11px] font-medium">{error}</p> : null}
    </SbField>
  )
}

/** All outbound tags in the draft (for selector/urltest member + route action pickers). */
export function useSbOutboundTags(draft: SingBoxCoreDraft | null): string[] {
  return useMemo(() => {
    if (!draft) return []
    return draft.outbounds.map(o => (typeof (o as WireRow).tag === 'string' ? ((o as WireRow).tag as string) : '')).filter(Boolean)
  }, [draft])
}

/** Convenience: read the sb draft + a stable updateSbDraft-based setter for a wire-array path. */
export function useSbDraft() {
  const draft = useCoreEditorStore(s => s.sbDraft)
  const updateSbDraft = useCoreEditorStore(s => s.updateSbDraft)
  return { draft, updateSbDraft }
}
