import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { CopyButton } from '@/components/common/copy-button'
import { LoaderButton } from '@/components/ui/loader-button'
import { Separator } from '@/components/ui/separator'
import { CoreEditorDataTable } from '@/features/core-editor/components/shared/core-editor-data-table'
import { CoreEditorFormDialog } from '@/features/core-editor/components/shared/core-editor-form-dialog'
import { OpenVPNInstanceForm } from '@/features/core-editor/components/openvpn/openvpn-instance-form'
import { XrayAdvancedSection } from '@/features/core-editor/components/xray/xray-advanced-section'
import { useSectionHeaderAddPulseEffect, type SectionHeaderAddPulse } from '@/features/core-editor/hooks/use-section-header-add-pulse'
import { createNewOpenVPNInstanceDraft } from '@/features/core-editor/kit/openvpn-adapter'
import { useCoreEditorStore } from '@/features/core-editor/state/core-editor-store'
import type { OvCoreSection } from '@/features/core-editor/state/core-editor-store'
import { useGenerateOpenvpnPkiBundle } from '@/service/api'
import { isOpenVPNPKIDraftComplete, validateOpenVPNInstanceDraft } from '@pasarguard/openvpn-config-kit'
import type { OpenVPNCoreDraft, OpenVPNInstanceDraft } from '@pasarguard/openvpn-config-kit'
import type { ColumnDef } from '@tanstack/react-table'
import { CircleCheck, KeyRound, RotateCw, ShieldAlert } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

interface OpenVPNCoreEditorProps {
  headerAddPulse?: SectionHeaderAddPulse
  headerAddEpoch?: number
}

const PKI_FIELD_ORDER = ['caCert', 'serverCert', 'serverKey', 'tlsCryptKey'] as const

/** Core-level PKI status + generate/regenerate action. Unlike instances, there is exactly one of these per config. */
function OpenVPNPkiSection({ draft }: { draft: OpenVPNCoreDraft }) {
  const { t } = useTranslation()
  const updateOvDraft = useCoreEditorStore(s => s.updateOvDraft)
  const [confirmRegenerateOpen, setConfirmRegenerateOpen] = useState(false)
  const generatePki = useGenerateOpenvpnPkiBundle()
  const complete = isOpenVPNPKIDraftComplete(draft.pki)

  const fieldLabels: Record<(typeof PKI_FIELD_ORDER)[number], string> = {
    caCert: t('coreEditor.openvpn.pki.caCert', { defaultValue: 'CA certificate' }),
    serverCert: t('coreEditor.openvpn.pki.serverCert', { defaultValue: 'Server certificate' }),
    serverKey: t('coreEditor.openvpn.pki.serverKey', { defaultValue: 'Server key' }),
    tlsCryptKey: t('coreEditor.openvpn.pki.tlsCryptKey', { defaultValue: 'tls-crypt static key' }),
  }

  const runGenerate = useCallback(() => {
    generatePki.mutate(undefined, {
      onSuccess: result => {
        updateOvDraft(d => ({
          ...d,
          pki: {
            caCert: result.ca_cert,
            serverCert: result.server_cert,
            serverKey: result.server_key,
            tlsCryptKey: result.tls_crypt_key,
          },
        }))
        toast.success(t('coreEditor.openvpn.pki.generateSuccess', { defaultValue: 'PKI generated. Save the core to persist it.' }))
      },
      onError: (error: unknown) => {
        const message = error instanceof Error ? error.message : t('coreEditor.openvpn.pki.generateFailed', { defaultValue: 'Failed to generate PKI' })
        toast.error(message)
      },
    })
  }, [generatePki, updateOvDraft, t])

  const handlePrimaryAction = () => {
    if (complete) {
      setConfirmRegenerateOpen(true)
      return
    }
    runGenerate()
  }

  return (
    <div className="space-y-4">
      <div className="border-border bg-muted/15 space-y-3 rounded-lg border p-4 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            {complete ? <CircleCheck className="size-4 shrink-0 text-green-600" /> : <ShieldAlert className="text-destructive size-4 shrink-0" />}
            <div>
              <p className="text-sm font-medium">
                {complete
                  ? t('coreEditor.openvpn.pki.statusGenerated', { defaultValue: 'PKI generated' })
                  : t('coreEditor.openvpn.pki.statusMissing', { defaultValue: 'PKI not generated yet' })}
              </p>
              <p className="text-muted-foreground text-xs">
                {complete
                  ? t('coreEditor.openvpn.pki.statusGeneratedHint', {
                      defaultValue: 'Every instance in this core shares this CA, server certificate/key, and tls-crypt static key.',
                    })
                  : t('coreEditor.openvpn.pki.statusMissingHint', {
                      defaultValue: 'This core cannot be saved until PKI is generated - every OpenVPN instance needs it.',
                    })}
              </p>
            </div>
          </div>
          <Badge variant={complete ? 'green' : 'red'} className="shrink-0">
            {complete ? t('coreEditor.openvpn.pki.badgeReady', { defaultValue: 'Ready' }) : t('coreEditor.openvpn.pki.badgeMissing', { defaultValue: 'Missing' })}
          </Badge>
        </div>

        <Separator />

        <LoaderButton type="button" variant={complete ? 'outline' : 'default'} onClick={handlePrimaryAction} isLoading={generatePki.isPending} loadingText={t('coreEditor.openvpn.pki.generating', { defaultValue: 'Generating…' })}>
          <span className="flex items-center gap-2">
            {complete ? <RotateCw className="size-4" /> : <KeyRound className="size-4" />}
            {complete ? t('coreEditor.openvpn.pki.regenerate', { defaultValue: 'Regenerate PKI' }) : t('coreEditor.openvpn.pki.generate', { defaultValue: 'Generate PKI' })}
          </span>
        </LoaderButton>
      </div>

      {complete && (
        <div className="space-y-2">
          {PKI_FIELD_ORDER.map(field => (
            <div key={field} className="flex min-w-0 items-center gap-2 rounded-md border px-3 py-2">
              <div className="min-w-0 flex-1">
                <p className="text-muted-foreground text-[11px] font-semibold tracking-wide uppercase">{fieldLabels[field]}</p>
                <p dir="ltr" className="truncate font-mono text-xs">
                  {draft.pki[field]}
                </p>
              </div>
              <CopyButton
                value={draft.pki[field]}
                icon="copy"
                className="h-8 w-8 shrink-0"
                copiedMessage={t('copied', { defaultValue: 'Copied!' })}
                defaultMessage={t('copyToClipboard', { defaultValue: 'Copy to clipboard' })}
              />
            </div>
          ))}
        </div>
      )}

      <AlertDialog open={confirmRegenerateOpen} onOpenChange={setConfirmRegenerateOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('coreEditor.openvpn.pki.regenerateConfirmTitle', { defaultValue: 'Regenerate PKI?' })}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('coreEditor.openvpn.pki.regenerateConfirmDesc', {
                defaultValue:
                  'This replaces the CA, server certificate/key, and tls-crypt static key for this core. Every previously-downloaded .ovpn file for every user on this core will stop working and must be re-downloaded after you save.',
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('cancel')}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setConfirmRegenerateOpen(false)
                runGenerate()
              }}
            >
              {t('coreEditor.openvpn.pki.regenerateConfirmAction', { defaultValue: 'Regenerate' })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

/** Top-level OpenVPN section: a searchable/reorderable table of instances (add/edit/delete), plus the core-level PKI status and the shared Advanced JSON tab. */
export function OpenVPNCoreEditor({ headerAddPulse, headerAddEpoch }: OpenVPNCoreEditorProps) {
  const { t } = useTranslation()
  const section = useCoreEditorStore(s => s.activeSection) as OvCoreSection
  const draft = useCoreEditorStore(s => s.ovDraft)
  const updateOvDraft = useCoreEditorStore(s => s.updateOvDraft)

  const [dialogOpen, setDialogOpen] = useState(false)
  const [dialogMode, setDialogMode] = useState<'add' | 'edit'>('add')
  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const [draftInstance, setDraftInstance] = useState<OpenVPNInstanceDraft | null>(null)
  const [initialDialogInstance, setInitialDialogInstance] = useState<OpenVPNInstanceDraft | null>(null)

  const beginAddInstance = useCallback(() => {
    if (!draft) return
    const created = createNewOpenVPNInstanceDraft(draft)
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
      const clone = { ...current, dnsServers: [...current.dnsServers] }
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
      updateOvDraft(d => ({ ...d, instances: d.instances.filter((_, i) => i !== index) }))
    },
    [updateOvDraft],
  )

  // Validated as if this row already replaced/joined the rest of the list, so tag/port
  // uniqueness checks against the other rows are meaningful while the dialog is open.
  const dialogIssues = useMemo(() => {
    if (!draft || !draftInstance) return []
    const others = draft.instances.filter((_, i) => i !== editingIndex)
    const allTags = [...others.map(i => i.tag), draftInstance.tag]
    const allPortKeys = [...others.map(i => `${i.protocol}/${i.port}`), `${draftInstance.protocol}/${draftInstance.port}`]
    return validateOpenVPNInstanceDraft(draftInstance, allTags.length - 1, allTags, allPortKeys)
  }, [draft, draftInstance, editingIndex])

  const commitDialog = useCallback(() => {
    if (!draftInstance) return
    if (dialogIssues.length > 0) {
      const first = dialogIssues[0]!
      toast.error(`${first.path}: ${first.message}`)
      return
    }
    if (dialogMode === 'add') {
      updateOvDraft(d => ({ ...d, instances: [...d.instances, draftInstance] }))
    } else if (editingIndex !== null) {
      updateOvDraft(d => ({ ...d, instances: d.instances.map((it, i) => (i === editingIndex ? draftInstance : it)) }))
    }
    setDialogOpen(false)
  }, [draftInstance, dialogIssues, dialogMode, editingIndex, updateOvDraft])

  const columns = useMemo<ColumnDef<OpenVPNInstanceDraft, unknown>[]>(
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
        accessorKey: 'protocol',
        header: () => t('coreEditor.openvpn.fields.protocol', { defaultValue: 'Protocol' }),
        cell: ({ row }) => <span className="text-xs uppercase">{row.original.protocol}</span>,
      },
      {
        id: 'port',
        header: () => t('coreEditor.col.port', { defaultValue: 'Port' }),
        cell: ({ row }) => <span className="text-xs">{row.original.port}</span>,
      },
      {
        accessorKey: 'network',
        header: () => t('coreEditor.openvpn.fields.network', { defaultValue: 'Network' }),
        cell: ({ row }) => (
          <span dir="ltr" className="text-xs">
            {row.original.network}
          </span>
        ),
      },
      {
        id: 'maxClients',
        header: () => t('coreEditor.openvpn.fields.maxClients', { defaultValue: 'Max clients' }),
        cell: ({ row }) => <span className="text-xs">{row.original.maxClients || t('coreEditor.openvpn.fields.maxClientsPlaceholder', { defaultValue: 'Unlimited' })}</span>,
      },
    ],
    [t],
  )

  if (section === 'advanced') return <XrayAdvancedSection />
  if (!draft) return null

  if (section === 'pki') {
    return <OpenVPNPkiSection draft={draft} />
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-muted-foreground text-sm">
          {t('coreEditor.openvpn.instancesHint', { defaultValue: 'Each instance is an independent openvpn process on a distinct protocol/port.' })}
        </p>
        <Button type="button" variant="outline" size="sm" onClick={beginAddInstance} className="gap-1.5">
          {t('coreEditor.instance.add', { defaultValue: 'Add instance' })}
        </Button>
      </div>

      <CoreEditorDataTable
        columns={columns}
        data={[...draft.instances]}
        getRowId={(_row, i) => String(i)}
        getSearchableText={item => `${item.tag} ${item.protocol} ${item.port} ${item.network}`}
        minRowCount={1}
        minRowCountMessage={t('coreEditor.openvpn.keepAtLeastOne', { defaultValue: 'At least one instance is required.' })}
        emptyLabel={t('coreEditor.openvpn.emptyInstances', { defaultValue: 'No instances yet. Click "Add instance" to create one.' })}
        onRowClick={(_row, rowIndex) => beginEditInstance(rowIndex)}
        onRemoveRow={removeInstance}
        onBulkRemove={indices => {
          const rm = new Set(indices)
          updateOvDraft(d => ({ ...d, instances: d.instances.filter((_, idx) => !rm.has(idx)) }))
        }}
        enableReorder
        onReorder={(from, to) => {
          updateOvDraft(d => {
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
        {draftInstance && <OpenVPNInstanceForm instance={draftInstance} issues={dialogIssues} onChange={updater => setDraftInstance(prev => (prev ? updater(prev) : prev))} />}
      </CoreEditorFormDialog>
    </div>
  )
}
