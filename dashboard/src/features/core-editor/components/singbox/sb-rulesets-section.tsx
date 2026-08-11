import type { SectionHeaderAddPulse } from '@/features/core-editor/hooks/use-section-header-add-pulse'
import { SbListSection, SbRawJsonField, SbSelectField, SbTextField, useSbDraft, useSbOutboundTags, type WireRow } from '@/features/core-editor/components/singbox/sb-section-kit'
import { createDefaultSingBoxRuleSet, SINGBOX_RULE_SET_TYPES, type SingBoxRuleSetType } from '@pasarguard/singbox-config-kit'
import type { ColumnDef } from '@tanstack/react-table'
import { useCallback, useMemo } from 'react'
import { useTranslation } from 'react-i18next'

export function SbRuleSetsSection({ headerAddPulse, headerAddEpoch }: { headerAddPulse?: SectionHeaderAddPulse; headerAddEpoch?: number }) {
  const { t } = useTranslation()
  const { draft, updateSbDraft } = useSbDraft()
  const outboundTags = useSbOutboundTags(draft)
  const route = (draft?.route ?? {}) as WireRow
  const ruleSets = (Array.isArray(route.rule_set) ? route.rule_set : []) as WireRow[]

  const setRuleSets = useCallback(
    (next: WireRow[]) => updateSbDraft(d => ({ ...d, route: { ...(d.route as WireRow), rule_set: next } as never })),
    [updateSbDraft],
  )
  const existingTags = useMemo(() => ruleSets.map(rs => String(rs.tag ?? '')).filter(Boolean), [ruleSets])

  const columns = useMemo<ColumnDef<WireRow, unknown>[]>(
    () => [
      { id: 'index', header: '#', cell: ({ row }) => row.index + 1 },
      { accessorKey: 'tag', header: () => t('coreEditor.col.tag', { defaultValue: 'Tag' }), cell: ({ row }) => <span className="font-mono text-xs">{String(row.original.tag ?? '')}</span> },
      { accessorKey: 'type', header: () => t('coreEditor.col.type', { defaultValue: 'Type' }), cell: ({ row }) => <span className="bg-muted text-muted-foreground rounded px-1.5 py-0.5 text-[10px] font-medium uppercase">{String(row.original.type ?? '')}</span> },
      { accessorKey: 'format', header: () => t('coreEditor.singbox.format', { defaultValue: 'Format' }), cell: ({ row }) => <span className="text-muted-foreground text-xs">{String(row.original.format ?? '—')}</span>, hideOnMobile: true },
      { id: 'source', header: () => t('coreEditor.singbox.source', { defaultValue: 'Source' }), cell: ({ row }) => <span className="text-muted-foreground truncate font-mono text-[11px]">{String(row.original.url ?? row.original.path ?? '')}</span>, width: 'minmax(0, 2fr)' },
    ],
    [t],
  )

  return (
    <SbListSection<WireRow>
      headerAddPulse={headerAddPulse}
      headerAddEpoch={headerAddEpoch}
      sectionId="ruleSets"
      rows={ruleSets}
      onRowsChange={setRuleSets}
      columns={columns}
      getSearchableText={r => `${r.tag ?? ''} ${r.type ?? ''} ${r.url ?? ''} ${r.path ?? ''}`}
      searchPlaceholder={t('coreEditor.singbox.searchRuleSets', { defaultValue: 'Search rule sets…' })}
      emptyLabel={t('coreEditor.singbox.noRuleSets', { defaultValue: 'No rule sets yet.' })}
      reorder
      createRow={type => createDefaultSingBoxRuleSet((type as SingBoxRuleSetType) ?? 'remote', existingTags) as WireRow}
      addTypes={SINGBOX_RULE_SET_TYPES}
      dialogTitleAdd={t('coreEditor.singbox.addRuleSet', { defaultValue: 'Add rule set' })}
      dialogTitleEdit={t('coreEditor.singbox.editRuleSet', { defaultValue: 'Edit rule set' })}
      renderBody={(row, patch) => {
        const set = (k: string, v: unknown) => patch({ ...row, [k]: v })
        const type = String(row.type ?? '')
        return (
          <>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <SbTextField label={t('coreEditor.col.tag', { defaultValue: 'Tag' })} value={row.tag} onChange={v => set('tag', v)} />
              <SbSelectField label={t('coreEditor.singbox.format', { defaultValue: 'Format' })} value={row.format} onChange={v => set('format', v)} options={[{ value: 'binary' }, { value: 'source' }]} />
              {type === 'remote' && (
                <>
                  <SbTextField label="URL" value={row.url} onChange={v => set('url', v)} placeholder="https://…/geosite-ir.srs" className="sm:col-span-2" />
                  <SbSelectField label={t('coreEditor.singbox.downloadDetour', { defaultValue: 'Download detour' })} value={row.download_detour} onChange={v => set('download_detour', v || undefined)} options={[{ value: '' }, ...outboundTags.map(v => ({ value: v }))]} />
                  <SbTextField label={t('coreEditor.singbox.updateInterval', { defaultValue: 'Update interval' })} value={row.update_interval} onChange={v => set('update_interval', v || undefined)} placeholder="1d" />
                </>
              )}
              {type === 'local' && <SbTextField label={t('coreEditor.singbox.path', { defaultValue: 'Path' })} value={row.path} onChange={v => set('path', v)} placeholder="/etc/sing-box/geosite.srs" className="sm:col-span-2" />}
            </div>
            <SbRawJsonField row={row} patch={patch} />
          </>
        )
      }}
    />
  )
}
