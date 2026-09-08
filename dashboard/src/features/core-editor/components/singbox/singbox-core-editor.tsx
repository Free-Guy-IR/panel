import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { CoreEditorDataTable } from '@/features/core-editor/components/shared/core-editor-data-table'
import { CoreEditorFormDialog } from '@/features/core-editor/components/shared/core-editor-form-dialog'
import { SbBindingsSection } from '@/features/core-editor/components/singbox/sb-bindings-section'
import { SbDnsSection } from '@/features/core-editor/components/singbox/sb-dns-section'
import { SbExperimentalSection } from '@/features/core-editor/components/singbox/sb-experimental-section'
import { SbOutboundsSection } from '@/features/core-editor/components/singbox/sb-outbounds-section'
import { SbRouteSection } from '@/features/core-editor/components/singbox/sb-route-section'
import { SbRuleSetsSection } from '@/features/core-editor/components/singbox/sb-rulesets-section'
import { SingBoxInboundForm } from '@/features/core-editor/components/singbox/singbox-inbound-form'
import { XrayAdvancedSection } from '@/features/core-editor/components/xray/xray-advanced-section'
import { useSectionHeaderAddPulseEffect, type SectionHeaderAddPulse } from '@/features/core-editor/hooks/use-section-header-add-pulse'
import { remapIndexAfterArrayMove } from '@/features/core-editor/kit/remap-index-after-move'
import { createNewHysteria2InboundDraft, createNewInboundDraft } from '@/features/core-editor/kit/singbox-adapter'
import { useCoreEditorStore } from '@/features/core-editor/state/core-editor-store'
import type { SbCoreSection } from '@/features/core-editor/state/core-editor-store'
import { cn } from '@/lib/utils'
import { arrayMove } from '@dnd-kit/sortable'
import { SINGBOX_BALANCER_OUTBOUND_TYPES, validateInboundDraft } from '@pasarguard/singbox-config-kit'
import type { SingBoxInboundDraft, SingBoxProtocol, SingBoxVersion } from '@pasarguard/singbox-config-kit'
import type { ColumnDef } from '@tanstack/react-table'
import { ChevronDown, Copy, Pencil, Plus } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

interface SingBoxCoreEditorProps {
  headerAddPulse?: SectionHeaderAddPulse
  headerAddEpoch?: number
}

const ADD_PROTOCOLS: readonly SingBoxProtocol[] = ['vless', 'vmess', 'trojan', 'shadowsocks', 'tuic', 'hysteria2']
const VERSIONS: readonly SingBoxVersion[] = ['1.11', '1.12']

/** sing-box core editor: dispatches to a section by activeSection, with a core-wide version switch. */
export function SingBoxCoreEditor({ headerAddPulse, headerAddEpoch }: SingBoxCoreEditorProps) {
  const { t } = useTranslation()
  const section = useCoreEditorStore(s => s.activeSection) as SbCoreSection
  const draft = useCoreEditorStore(s => s.sbDraft)
  const updateSbDraft = useCoreEditorStore(s => s.updateSbDraft)

  if (!draft) return null
  const version = draft.singboxVersion ?? '1.12'
  const setVersion = (v: SingBoxVersion) => updateSbDraft(d => ({ ...d, singboxVersion: v }))

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-muted-foreground text-xs">{t('coreEditor.singbox.versionHint', { defaultValue: 'sing-box release these settings target.' })}</p>
        <div className="bg-background inline-flex h-9 items-center gap-1 rounded-lg border p-0.5 shadow-sm" role="radiogroup" aria-label="sing-box version">
          {VERSIONS.map(v => (
            <button
              key={v}
              type="button"
              role="radio"
              aria-checked={version === v}
              onClick={() => setVersion(v)}
              className={cn(
                'h-8 min-w-8 rounded-md px-3 text-sm font-medium transition-colors',
                version === v ? 'bg-primary text-primary-foreground shadow-sm' : 'text-muted-foreground hover:bg-muted',
              )}
            >
              {v}.x
            </button>
          ))}
        </div>
      </div>

      {section === 'inbounds' && <SbInboundsSection headerAddPulse={headerAddPulse} headerAddEpoch={headerAddEpoch} />}
      {section === 'outbounds' && <SbOutboundsSection headerAddPulse={headerAddPulse} headerAddEpoch={headerAddEpoch} sectionId="outbounds" />}
      {section === 'balancers' && <SbOutboundsSection headerAddPulse={headerAddPulse} headerAddEpoch={headerAddEpoch} sectionId="balancers" onlyTypes={SINGBOX_BALANCER_OUTBOUND_TYPES} />}
      {section === 'route' && <SbRouteSection headerAddPulse={headerAddPulse} headerAddEpoch={headerAddEpoch} />}
      {section === 'ruleSets' && <SbRuleSetsSection headerAddPulse={headerAddPulse} headerAddEpoch={headerAddEpoch} />}
      {section === 'dns' && <SbDnsSection headerAddPulse={headerAddPulse} headerAddEpoch={headerAddEpoch} />}
      {section === 'bindings' && <SbBindingsSection />}
      {section === 'experimental' && <SbExperimentalSection />}
      {section === 'advanced' && <XrayAdvancedSection />}
    </div>
  )
}

function duplicateTag(tag: string, taken: readonly string[]) {
  const base = tag ? `${tag}-copy` : 'inbound-copy'
  if (!taken.includes(base)) return base
  let n = 2
  while (taken.includes(`${base}-${n}`)) n += 1
  return `${base}-${n}`
}

function SbInboundsSection({ headerAddPulse, headerAddEpoch }: SingBoxCoreEditorProps) {
  const { t } = useTranslation()
  const draft = useCoreEditorStore(s => s.sbDraft)
  const updateSbDraft = useCoreEditorStore(s => s.updateSbDraft)
  const [detailOpen, setDetailOpen] = useState(false)
  const [selected, setSelected] = useState(0)

  const openNewInbound = useCallback(
    (create: (d: NonNullable<typeof draft>) => SingBoxInboundDraft) => {
      updateSbDraft(d => {
        const nextInbounds = [...d.inbounds, create(d)]
        setSelected(nextInbounds.length - 1)
        setDetailOpen(true)
        return { ...d, inbounds: nextInbounds }
      })
    },
    [updateSbDraft],
  )

  const addInboundOfProtocol = useCallback((protocol: SingBoxProtocol) => openNewInbound(d => createNewInboundDraft(d, protocol)), [openNewInbound])
  const addInbound = useCallback(() => openNewInbound(d => createNewHysteria2InboundDraft(d)), [openNewInbound])

  useSectionHeaderAddPulseEffect(headerAddPulse, headerAddEpoch, 'inbounds', addInbound)

  const allTags = useMemo(() => (draft?.inbounds ?? []).map(i => i.tag), [draft])
  const rows = useMemo(() => [...(draft?.inbounds ?? [])], [draft])

  const columns = useMemo<ColumnDef<SingBoxInboundDraft, unknown>[]>(
    () => [
      {
        id: 'index',
        header: '#',
        cell: ({ row }) => row.index + 1,
      },
      {
        accessorKey: 'tag',
        header: () => t('coreEditor.col.tag', { defaultValue: 'Tag' }),
        cell: ({ row }) => <span className="text-xs">{row.original.tag || t('coreEditor.singbox.untitledInbound', { defaultValue: 'Untitled inbound' })}</span>,
      },
      {
        accessorKey: 'protocol',
        header: () => t('coreEditor.col.protocol', { defaultValue: 'Protocol' }),
        cell: ({ row }) => row.original.protocol,
      },
      {
        id: 'port',
        header: () => t('coreEditor.col.port', { defaultValue: 'Port' }),
        cell: ({ row }) => row.original.listenPort || '?',
      },
      {
        id: 'status',
        header: () => t('coreEditor.col.status', { defaultValue: 'Status' }),
        cell: ({ row }) =>
          validateInboundDraft(row.original, row.index, allTags).length > 0 ? (
            <span className="text-destructive text-xs font-medium">{t('coreEditor.singbox.hasErrors', { defaultValue: 'Needs attention' })}</span>
          ) : null,
      },
    ],
    [t, allTags],
  )

  if (!draft) return null

  const current = draft.inbounds[selected]
  const currentIssues = current ? validateInboundDraft(current, selected, allTags) : []

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-muted-foreground text-sm">{t('coreEditor.singbox.inboundsHint', { defaultValue: 'Each inbound is a separate listener on this sing-box core.' })}</p>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="outline" size="sm" className="gap-1.5">
              <Plus className="size-4" />
              {t('coreEditor.inbound.add', { defaultValue: 'Add inbound' })}
              <ChevronDown className="size-3.5 opacity-60" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {ADD_PROTOCOLS.map(p => (
              <DropdownMenuItem key={p} onClick={() => addInboundOfProtocol(p)}>
                {p}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <CoreEditorDataTable
        columns={columns}
        data={rows}
        getRowId={(_row: SingBoxInboundDraft, i: number) => String(i)}
        emptyLabel={t('coreEditor.singbox.emptyInbounds', { defaultValue: 'No inbounds yet. Click "Add inbound" to create a listener.' })}
        onRowClick={(_row, rowIndex) => {
          setSelected(rowIndex)
          setDetailOpen(true)
        }}
        onRemoveRow={i => {
          updateSbDraft(d => ({ ...d, inbounds: d.inbounds.filter((_, idx) => idx !== i) }))
          setSelected(0)
        }}
        onBulkRemove={indices => {
          const rm = new Set(indices)
          updateSbDraft(d => ({ ...d, inbounds: d.inbounds.filter((_, idx) => !rm.has(idx)) }))
          setSelected(0)
        }}
        enableReorder
        onReorder={(from, to) => {
          updateSbDraft(d => ({ ...d, inbounds: arrayMove([...d.inbounds], from, to) }))
          setSelected(sel => remapIndexAfterArrayMove(sel, from, to))
        }}
        getRowActions={(_row, index) => [
          {
            key: 'duplicate',
            label: t('duplicate'),
            icon: <Copy className="size-4 shrink-0" />,
            onSelect: () => {
              updateSbDraft(d => {
                const source = d.inbounds[index]
                if (!source) return d
                const copy = {
                  ...structuredClone(source),
                  tag: duplicateTag(
                    source.tag,
                    d.inbounds.map(i => i.tag),
                  ),
                }
                const next = [...d.inbounds]
                next.splice(index + 1, 0, copy)
                return { ...d, inbounds: next }
              })
              setSelected(index + 1)
            },
          },
        ]}
      />

      <CoreEditorFormDialog
        isDialogOpen={detailOpen && Boolean(current)}
        onOpenChange={setDetailOpen}
        leadingIcon={<Pencil className="h-5 w-5 shrink-0" />}
        title={t('coreEditor.inbound.dialogTitleEdit', { defaultValue: 'Edit inbound' })}
        size="md"
      >
        {current ? (
          <SingBoxInboundForm
            inbound={current}
            issues={currentIssues}
            onChange={updater => {
              updateSbDraft(d => ({
                ...d,
                inbounds: d.inbounds.map((it, i) => (i === selected ? updater(it) : it)),
              }))
            }}
          />
        ) : null}
      </CoreEditorFormDialog>
    </div>
  )
}
