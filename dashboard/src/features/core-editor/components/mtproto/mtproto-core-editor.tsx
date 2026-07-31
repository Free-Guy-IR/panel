import { Button } from '@/components/ui/button'
import { CoreEditorDataTable } from '@/features/core-editor/components/shared/core-editor-data-table'
import { CoreEditorFormDialog } from '@/features/core-editor/components/shared/core-editor-form-dialog'
import { MTProtoInstanceForm } from '@/features/core-editor/components/mtproto/mtproto-instance-form'
import { XrayAdvancedSection } from '@/features/core-editor/components/xray/xray-advanced-section'
import { useSectionHeaderAddPulseEffect, type SectionHeaderAddPulse } from '@/features/core-editor/hooks/use-section-header-add-pulse'
import { createNewMTProtoInstanceDraft } from '@/features/core-editor/kit/mtproto-adapter'
import { useCoreEditorStore } from '@/features/core-editor/state/core-editor-store'
import type { MtCoreSection } from '@/features/core-editor/state/core-editor-store'
import { validateMTProtoInstanceDraft } from '@pasarguard/mtproto-config-kit'
import type { MTProtoInstanceDraft } from '@pasarguard/mtproto-config-kit'
import type { ColumnDef } from '@tanstack/react-table'
import { useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

interface MTProtoCoreEditorProps {
  headerAddPulse?: SectionHeaderAddPulse
  headerAddEpoch?: number
}

/** Top-level MTProto section: a searchable/reorderable table of instances (add/edit/delete), plus the shared Advanced JSON tab. Unlike OpenVPN, there is no PKI section - MTProto secrets are symmetric, nothing is server-generated. */
export function MTProtoCoreEditor({ headerAddPulse, headerAddEpoch }: MTProtoCoreEditorProps) {
  const { t } = useTranslation()
  const section = useCoreEditorStore(s => s.activeSection) as MtCoreSection
  const draft = useCoreEditorStore(s => s.mtDraft)
  const updateMtDraft = useCoreEditorStore(s => s.updateMtDraft)

  const [dialogOpen, setDialogOpen] = useState(false)
  const [dialogMode, setDialogMode] = useState<'add' | 'edit'>('add')
  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const [draftInstance, setDraftInstance] = useState<MTProtoInstanceDraft | null>(null)
  const [initialDialogInstance, setInitialDialogInstance] = useState<MTProtoInstanceDraft | null>(null)

  const beginAddInstance = useCallback(() => {
    if (!draft) return
    const created = createNewMTProtoInstanceDraft(draft)
    setDraftInstance(created)
    setInitialDialogInstance(created)
    setDialogMode('add')
    setEditingIndex(null)
    setDialogOpen(true)
  }, [draft])

  // Reacts to the page header's "+ Add" button, but only while the Instances section is active.
  useSectionHeaderAddPulseEffect(headerAddPulse, headerAddEpoch, 'instances', beginAddInstance)

  const beginEditInstance = useCallback(
    (index: number) => {
      if (!draft) return
      const current = draft.instances[index]
      if (!current) return
      const clone = { ...current }
      setDraftInstance(clone)
      setInitialDialogInstance(clone)
      setDialogMode('edit')
      setEditingIndex(index)
      setDialogOpen(true)
    },
    [draft],
  )

  const removeInstance = useCallback(
    (index: number) => {
      updateMtDraft(d => ({ ...d, instances: d.instances.filter((_, i) => i !== index) }))
    },
    [updateMtDraft],
  )

  // Validated as if this row already replaced/joined the rest of the list, so tag/port
  // uniqueness checks against the other rows are meaningful while the dialog is open.
  const dialogIssues = useMemo(() => {
    if (!draft || !draftInstance) return []
    const others = draft.instances.filter((_, i) => i !== editingIndex)
    const allTags = [...others.map(i => i.tag), draftInstance.tag]
    const allPorts = [...others.map(i => i.port), draftInstance.port]
    return validateMTProtoInstanceDraft(draftInstance, allTags.length - 1, allTags, allPorts)
  }, [draft, draftInstance, editingIndex])

  const commitDialog = useCallback(() => {
    if (!draftInstance) return
    if (dialogIssues.length > 0) {
      const first = dialogIssues[0]!
      toast.error(`${first.path}: ${first.message}`)
      return
    }
    if (dialogMode === 'add') {
      updateMtDraft(d => ({ ...d, instances: [...d.instances, draftInstance] }))
    } else if (editingIndex !== null) {
      updateMtDraft(d => ({ ...d, instances: d.instances.map((it, i) => (i === editingIndex ? draftInstance : it)) }))
    }
    setDialogOpen(false)
  }, [draftInstance, dialogIssues, dialogMode, editingIndex, updateMtDraft])

  const columns = useMemo<ColumnDef<MTProtoInstanceDraft, unknown>[]>(
    () => [
      {
        id: 'index',
        header: '#',
        cell: ({ row }) => row.index + 1,
      },
      {
        accessorKey: 'tag',
        header: () => t('coreEditor.col.tag', { defaultValue: 'Tag' }),
        cell: ({ row }) => <span className="text-xs">{row.original.tag}</span>,
      },
      {
        id: 'port',
        header: () => t('coreEditor.col.port', { defaultValue: 'Port' }),
        cell: ({ row }) => <span className="text-xs">{row.original.port}</span>,
      },
      {
        accessorKey: 'fakeTlsDomain',
        header: () => t('coreEditor.mtproto.fields.fakeTlsDomain', { defaultValue: 'Fake-TLS domain' }),
        cell: ({ row }) => (
          <span dir="ltr" className="text-xs">
            {row.original.fakeTlsDomain}
          </span>
        ),
      },
    ],
    [t],
  )

  if (section === 'advanced') return <XrayAdvancedSection />
  if (!draft) return null

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-muted-foreground text-sm">
          {t('coreEditor.mtproto.instancesHint', { defaultValue: 'Each instance is an independent fake-TLS listener (port + disguise domain), shared by every authorized user.' })}
        </p>
        <Button type="button" variant="outline" size="sm" onClick={beginAddInstance} className="gap-1.5">
          {t('coreEditor.instance.add', { defaultValue: 'Add instance' })}
        </Button>
      </div>

      <CoreEditorDataTable
        columns={columns}
        data={[...draft.instances]}
        getRowId={(_row, i) => String(i)}
        getSearchableText={item => `${item.tag} ${item.port} ${item.fakeTlsDomain}`}
        minRowCount={1}
        minRowCountMessage={t('coreEditor.mtproto.keepAtLeastOne', { defaultValue: 'At least one instance is required.' })}
        emptyLabel={t('coreEditor.mtproto.emptyInstances', { defaultValue: 'No instances yet. Click "Add instance" to create one.' })}
        onRowClick={(_row, rowIndex) => beginEditInstance(rowIndex)}
        onRemoveRow={removeInstance}
        onBulkRemove={indices => {
          const rm = new Set(indices)
          updateMtDraft(d => ({ ...d, instances: d.instances.filter((_, idx) => !rm.has(idx)) }))
        }}
        enableReorder
        onReorder={(from, to) => {
          updateMtDraft(d => {
            const next = [...d.instances]
            const [moved] = next.splice(from, 1)
            if (!moved) return d
            next.splice(to, 0, moved)
            return { ...d, instances: next }
          })
        }}
      />

      <CoreEditorFormDialog
        isDialogOpen={dialogOpen}
        onOpenChange={setDialogOpen}
        title={dialogMode === 'add' ? t('coreEditor.instance.add', { defaultValue: 'Add instance' }) : t('coreEditor.instance.edit', { defaultValue: 'Edit instance' })}
        size="lg"
        initialData={initialDialogInstance}
        getCurrentData={() => draftInstance}
        footerExtra={
          <Button type="button" onClick={commitDialog}>
            {dialogMode === 'add' ? t('add', { defaultValue: 'Add' }) : t('save', { defaultValue: 'Save' })}
          </Button>
        }
      >
        {draftInstance && <MTProtoInstanceForm instance={draftInstance} issues={dialogIssues} onChange={updater => setDraftInstance(prev => (prev ? updater(prev) : prev))} />}
      </CoreEditorFormDialog>
    </div>
  )
}
