import { StringArrayPopoverInput } from '@/components/common/string-array-popover-input'
import { Card, CardContent } from '@/components/ui/card'
import type { SectionHeaderAddPulse } from '@/features/core-editor/hooks/use-section-header-add-pulse'
import { SbField, SbListSection, SbRawJsonField, SbSelectField, SbSwitchRow, useSbDraft, useSbOutboundTags, type WireRow } from '@/features/core-editor/components/singbox/sb-section-kit'
import { createDefaultSingBoxRouteRule } from '@pasarguard/singbox-config-kit'
import type { ColumnDef } from '@tanstack/react-table'
import { useCallback, useMemo } from 'react'
import { useTranslation } from 'react-i18next'

function firstList(v: unknown): string[] {
  if (Array.isArray(v)) return v.map(String)
  if (typeof v === 'string' && v) return [v]
  return []
}

function ruleMatchSummary(r: WireRow): string {
  const parts: string[] = []
  for (const key of ['inbound', 'protocol', 'network', 'domain', 'domain_suffix', 'domain_keyword', 'ip_cidr', 'rule_set', 'clash_mode', 'port']) {
    const v = (r as Record<string, unknown>)[key]
    const list = firstList(v)
    if (list.length) parts.push(`${key}:${list.slice(0, 2).join(',')}${list.length > 2 ? '…' : ''}`)
    if (parts.length >= 2) break
  }
  return parts.join('  ') || '—'
}

function ruleAction(r: WireRow): string {
  if (typeof r.outbound === 'string' && r.outbound) return r.outbound
  if (typeof r.action === 'string') return r.action
  return '—'
}

export function SbRouteSection({ headerAddPulse, headerAddEpoch }: { headerAddPulse?: SectionHeaderAddPulse; headerAddEpoch?: number }) {
  const { t } = useTranslation()
  const { draft, updateSbDraft } = useSbDraft()
  const outboundTags = useSbOutboundTags(draft)
  const route = (draft?.route ?? {}) as WireRow
  const rules = (Array.isArray(route.rules) ? route.rules : []) as WireRow[]

  const setRules = useCallback(
    (next: WireRow[]) => updateSbDraft(d => ({ ...d, route: { ...(d.route as WireRow), rules: next } as never })),
    [updateSbDraft],
  )
  const setRoute = useCallback(
    (key: string, value: unknown) => updateSbDraft(d => ({ ...d, route: { ...(d.route as WireRow), [key]: value } as never })),
    [updateSbDraft],
  )

  const ruleSetTags = useMemo(() => (Array.isArray(route.rule_set) ? (route.rule_set as WireRow[]).map(rs => String(rs.tag ?? '')).filter(Boolean) : []), [route.rule_set])
  const actionTargets = useMemo(() => [{ value: '' }, ...outboundTags.map(v => ({ value: v }))], [outboundTags])

  const columns = useMemo<ColumnDef<WireRow, unknown>[]>(
    () => [
      { id: 'index', header: '#', cell: ({ row }) => row.index + 1 },
      { id: 'match', header: () => t('coreEditor.col.match', { defaultValue: 'Match' }), cell: ({ row }) => <span className="text-muted-foreground truncate font-mono text-[11px]">{ruleMatchSummary(row.original)}</span>, width: 'minmax(0, 2fr)' },
      { id: 'action', header: () => t('coreEditor.col.outbound', { defaultValue: 'Outbound' }), cell: ({ row }) => <span className="font-mono text-xs">{ruleAction(row.original)}</span> },
    ],
    [t],
  )

  return (
    <div className="space-y-6">
      <Card>
        <CardContent className="grid grid-cols-1 gap-4 pt-5 sm:grid-cols-3">
          <SbSelectField label={t('coreEditor.singbox.finalOutbound', { defaultValue: 'Final outbound' })} value={route.final} onChange={v => setRoute('final', v || undefined)} options={actionTargets} hint={t('coreEditor.singbox.finalHint', { defaultValue: 'Default when no rule matches.' })} />
          <div className="sm:col-span-2 sm:pt-6">
            <SbSwitchRow label={t('coreEditor.singbox.autoDetectInterface', { defaultValue: 'Auto detect interface' })} checked={route.auto_detect_interface === true} onChange={v => setRoute('auto_detect_interface', v || undefined)} />
          </div>
        </CardContent>
      </Card>

      <SbListSection<WireRow>
        headerAddPulse={headerAddPulse}
        headerAddEpoch={headerAddEpoch}
        sectionId="route"
        rows={rules}
        onRowsChange={setRules}
        columns={columns}
        getSearchableText={r => JSON.stringify(r)}
        searchPlaceholder={t('coreEditor.singbox.searchRules', { defaultValue: 'Search rules…' })}
        emptyLabel={t('coreEditor.singbox.noRules', { defaultValue: 'No route rules yet.' })}
        reorder
        createRow={() => createDefaultSingBoxRouteRule() as WireRow}
        dialogTitleAdd={t('coreEditor.singbox.addRule', { defaultValue: 'Add route rule' })}
        dialogTitleEdit={t('coreEditor.singbox.editRule', { defaultValue: 'Edit route rule' })}
        dialogSize="lg"
        renderBody={(row, patch) => <RouteRuleBody row={row} patch={patch} outboundTags={outboundTags} ruleSetTags={ruleSetTags} />}
      />
    </div>
  )
}

const RULE_ACTIONS = ['route', 'reject', 'hijack-dns', 'sniff', 'resolve'] as const

function RouteRuleBody({ row, patch, outboundTags, ruleSetTags }: { row: WireRow; patch: (n: WireRow) => void; outboundTags: string[]; ruleSetTags: string[] }) {
  const { t } = useTranslation()
  const set = (key: string, value: unknown) => {
    const next = { ...row }
    if (value === undefined || (Array.isArray(value) && value.length === 0) || value === '') delete (next as Record<string, unknown>)[key]
    else (next as Record<string, unknown>)[key] = value
    patch(next)
  }
  const action = typeof row.action === 'string' ? row.action : row.outbound ? 'route' : ''

  return (
    <>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <SbField label={t('coreEditor.singbox.inbound', { defaultValue: 'Inbound tags' })}>
          <StringArrayPopoverInput value={firstList(row.inbound)} onChange={v => set('inbound', v)} placeholder="tag" />
        </SbField>
        <SbField label={t('coreEditor.singbox.protocol', { defaultValue: 'Protocol' })}>
          <StringArrayPopoverInput value={firstList(row.protocol)} onChange={v => set('protocol', v)} placeholder="tls, http, quic, dns" />
        </SbField>
        <SbField label="Domain">
          <StringArrayPopoverInput value={firstList(row.domain)} onChange={v => set('domain', v)} placeholder="example.com" />
        </SbField>
        <SbField label="Domain suffix">
          <StringArrayPopoverInput value={firstList(row.domain_suffix)} onChange={v => set('domain_suffix', v)} placeholder=".ir" />
        </SbField>
        <SbField label="IP CIDR">
          <StringArrayPopoverInput value={firstList(row.ip_cidr)} onChange={v => set('ip_cidr', v)} placeholder="10.0.0.0/8" />
        </SbField>
        <SbField label={t('coreEditor.singbox.ruleSet', { defaultValue: 'Rule set' })}>
          <StringArrayPopoverInput value={firstList(row.rule_set)} onChange={v => set('rule_set', v)} placeholder={ruleSetTags[0] ?? 'rule-set tag'} />
        </SbField>
        <SbField label={t('coreEditor.singbox.port', { defaultValue: 'Port' })}>
          <StringArrayPopoverInput value={firstList(row.port).map(String)} onChange={v => set('port', v.map(x => Number(x)).filter(n => !Number.isNaN(n)))} placeholder="443" />
        </SbField>
        <SbSelectField label={t('coreEditor.singbox.network', { defaultValue: 'Network' })} value={row.network} onChange={v => set('network', v || undefined)} options={[{ value: '' }, { value: 'tcp' }, { value: 'udp' }]} />
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <SbSelectField label={t('coreEditor.singbox.action', { defaultValue: 'Action' })} value={action} onChange={v => (v === 'route' ? set('action', undefined) : set('action', v))} options={[{ value: 'route' }, ...RULE_ACTIONS.filter(a => a !== 'route').map(v => ({ value: v }))]} hint={t('coreEditor.singbox.actionHint', { defaultValue: 'route uses the outbound below.' })} />
        {(action === 'route' || action === '') && <SbSelectField label={t('coreEditor.col.outbound', { defaultValue: 'Outbound' })} value={row.outbound} onChange={v => set('outbound', v || undefined)} options={[{ value: '' }, ...outboundTags.map(v => ({ value: v }))]} />}
      </div>

      <SbRawJsonField row={row} patch={patch} />
    </>
  )
}
