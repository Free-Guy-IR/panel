import { StringTagPicker } from '@/components/common/string-tag-picker'
import type { SectionHeaderAddPulse } from '@/features/core-editor/hooks/use-section-header-add-pulse'
import { SbField, SbListSection, SbNumberField, SbRawJsonField, SbSelectField, SbTextField, useSbDraft, type WireRow } from '@/features/core-editor/components/singbox/sb-section-kit'
import { createDefaultSingBoxOutbound, SINGBOX_OUTBOUND_TYPES, type SingBoxOutboundType } from '@pasarguard/singbox-config-kit'
import type { ColumnDef } from '@tanstack/react-table'
import { useCallback, useMemo } from 'react'
import { useTranslation } from 'react-i18next'

const SERVER_TYPES = new Set(['socks', 'http', 'shadowsocks', 'vmess', 'vless', 'trojan', 'hysteria2', 'tuic', 'anytls', 'ssh'])
const GROUP_TYPES = new Set(['selector', 'urltest'])

function outboundServerLabel(o: WireRow): string {
  const type = String(o.type ?? '')
  if (GROUP_TYPES.has(type)) return Array.isArray(o.outbounds) ? (o.outbounds as unknown[]).map(String).join(', ') : ''
  if (SERVER_TYPES.has(type)) return o.server ? `${o.server}${o.server_port != null ? `:${o.server_port}` : ''}` : ''
  return '—'
}

interface SectionProps {
  headerAddPulse?: SectionHeaderAddPulse
  headerAddEpoch?: number
  /** When set, restrict to these outbound types (Balancers = selector/urltest). */
  onlyTypes?: readonly SingBoxOutboundType[]
  sectionId?: 'outbounds' | 'balancers'
}

export function SbOutboundsSection({ headerAddPulse, headerAddEpoch, onlyTypes, sectionId = 'outbounds' }: SectionProps) {
  const { t } = useTranslation()
  const { draft, updateSbDraft } = useSbDraft()

  const allOutbounds = useMemo(() => (draft?.outbounds ?? []) as unknown as WireRow[], [draft])
  // Balancers view = only selector/urltest, but writes must map back into the shared outbounds array.
  const visibleIndexes = useMemo(
    () => allOutbounds.map((_, i) => i).filter(i => !onlyTypes || onlyTypes.includes(String(allOutbounds[i]!.type) as SingBoxOutboundType)),
    [allOutbounds, onlyTypes],
  )
  const rows = useMemo(() => visibleIndexes.map(i => allOutbounds[i]!), [allOutbounds, visibleIndexes])

  const setRows = useCallback(
    (nextVisible: WireRow[]) => {
      updateSbDraft(d => {
        const all = [...((d.outbounds ?? []) as unknown as WireRow[])]
        const idxs = all.map((_o, i) => i).filter(i => !onlyTypes || onlyTypes.includes(String(all[i]!.type) as SingBoxOutboundType))
        if (nextVisible.length === idxs.length) {
          // Edit / reorder of the visible slice: write each back in its original position so the
          // rest of the outbounds array (and its order) is untouched.
          const out = [...all]
          idxs.forEach((origIdx, k) => {
            out[origIdx] = nextVisible[k]!
          })
          return { ...d, outbounds: out as never }
        }
        // Add / remove: keep the non-visible outbounds, then the new visible slice.
        const nonVisible = all.filter((_, i) => !idxs.includes(i))
        return { ...d, outbounds: [...nonVisible, ...nextVisible] as never }
      })
    },
    [onlyTypes, updateSbDraft],
  )

  const outboundTags = useMemo(() => allOutbounds.map(o => String(o.tag ?? '')).filter(Boolean), [allOutbounds])

  const createRow = useCallback(
    (type?: string) => createDefaultSingBoxOutbound((type as SingBoxOutboundType) ?? (onlyTypes?.[0] ?? 'direct'), outboundTags) as WireRow,
    [onlyTypes, outboundTags],
  )

  const columns = useMemo<ColumnDef<WireRow, unknown>[]>(
    () => [
      { id: 'index', header: '#', cell: ({ row }) => row.index + 1 },
      { accessorKey: 'tag', header: () => t('coreEditor.col.tag', { defaultValue: 'Tag' }), cell: ({ row }) => <span className="font-mono text-xs">{String(row.original.tag ?? '')}</span> },
      { accessorKey: 'type', header: () => t('coreEditor.col.type', { defaultValue: 'Type' }), cell: ({ row }) => <span className="bg-muted text-muted-foreground rounded px-1.5 py-0.5 text-[10px] font-medium uppercase">{String(row.original.type ?? '')}</span> },
      { id: 'server', header: () => (onlyTypes ? t('coreEditor.col.outbounds', { defaultValue: 'Outbounds' }) : t('coreEditor.col.server', { defaultValue: 'Server' })), cell: ({ row }) => <span className="text-muted-foreground truncate text-xs">{outboundServerLabel(row.original)}</span> },
    ],
    [onlyTypes, t],
  )

  const types = onlyTypes ?? SINGBOX_OUTBOUND_TYPES

  return (
    <SbListSection<WireRow>
      headerAddPulse={headerAddPulse}
      headerAddEpoch={headerAddEpoch}
      sectionId={sectionId}
      rows={rows}
      onRowsChange={setRows}
      columns={columns}
      getSearchableText={o => `${o.tag ?? ''} ${o.type ?? ''} ${o.server ?? ''}`}
      searchPlaceholder={t('coreEditor.outbound.search', { defaultValue: 'Search outbounds…' })}
      emptyLabel={onlyTypes ? t('coreEditor.singbox.noBalancers', { defaultValue: 'No selector / urltest outbounds yet.' }) : t('coreEditor.singbox.noOutbounds', { defaultValue: 'No outbounds yet.' })}
      reorder={!onlyTypes}
      createRow={createRow}
      addTypes={types}
      dialogTitleAdd={onlyTypes ? t('coreEditor.singbox.addBalancer', { defaultValue: 'Add balancer' }) : t('coreEditor.singbox.addOutbound', { defaultValue: 'Add outbound' })}
      dialogTitleEdit={onlyTypes ? t('coreEditor.singbox.editBalancer', { defaultValue: 'Edit balancer' }) : t('coreEditor.singbox.editOutbound', { defaultValue: 'Edit outbound' })}
      dialogSize="lg"
      renderBody={(row, patch) => <OutboundBody row={row} patch={patch} outboundTags={outboundTags} />}
    />
  )
}

function OutboundBody({ row, patch, outboundTags }: { row: WireRow; patch: (next: WireRow) => void; outboundTags: string[] }) {
  const { t } = useTranslation()
  const type = String(row.type ?? '')
  const set = (key: string, value: unknown) => patch({ ...row, [key]: value })
  const isServer = SERVER_TYPES.has(type)
  const isGroup = GROUP_TYPES.has(type)

  return (
    <>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <SbTextField label={t('coreEditor.col.tag', { defaultValue: 'Tag' })} value={row.tag} onChange={v => set('tag', v)} placeholder="proxy" />
        <SbField label={t('coreEditor.col.type', { defaultValue: 'Type' })}>
          <span className="bg-muted text-muted-foreground inline-flex h-9 items-center rounded-md px-3 text-xs font-medium uppercase">{type}</span>
        </SbField>
      </div>

      {isServer && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <SbTextField label={t('coreEditor.col.server', { defaultValue: 'Server' })} value={row.server} onChange={v => set('server', v)} placeholder="example.com" />
          <SbNumberField label={t('coreEditor.col.port', { defaultValue: 'Port' })} value={row.server_port} onChange={v => set('server_port', v)} placeholder="443" />
          {'uuid' in row && <SbTextField label="UUID" value={row.uuid} onChange={v => set('uuid', v)} />}
          {'password' in row && <SbTextField label={t('coreEditor.col.password', { defaultValue: 'Password' })} value={row.password} onChange={v => set('password', v)} />}
          {'method' in row && <SbTextField label={t('coreEditor.col.method', { defaultValue: 'Method' })} value={row.method} onChange={v => set('method', v)} />}
          {'user' in row && <SbTextField label={t('coreEditor.col.user', { defaultValue: 'User' })} value={row.user} onChange={v => set('user', v)} />}
        </div>
      )}

      {isGroup && (
        <>
          <SbField label={t('coreEditor.col.outbounds', { defaultValue: 'Member outbounds' })} hint={t('coreEditor.singbox.balancerMembersHint', { defaultValue: 'Outbounds this selector/urltest picks between.' })}>
            <StringTagPicker
              mode="multi"
              options={outboundTags.filter(tg => tg !== row.tag)}
              valueMulti={Array.isArray(row.outbounds) ? (row.outbounds as unknown[]).map(String) : []}
              onChangeMulti={v => set('outbounds', v)}
              placeholder={t('coreEditor.singbox.pickOutbounds', { defaultValue: 'Pick member outbounds' })}
              clearAllLabel={t('clearAll', { defaultValue: 'Clear all' })}
            />
          </SbField>
          {type === 'urltest' && (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <SbTextField label="URL" value={row.url} onChange={v => set('url', v)} placeholder="https://www.gstatic.com/generate_204" className="sm:col-span-2" />
              <SbTextField label={t('coreEditor.singbox.interval', { defaultValue: 'Interval' })} value={row.interval} onChange={v => set('interval', v)} placeholder="3m" />
            </div>
          )}
          {type === 'selector' && <SbSelectField label={t('coreEditor.singbox.default', { defaultValue: 'Default' })} value={row.default} onChange={v => set('default', v)} options={[{ value: '' }, ...outboundTags.filter(tg => tg !== row.tag).map(v => ({ value: v }))]} />}
        </>
      )}

      <SbRawJsonField row={row} patch={patch} />
    </>
  )
}
