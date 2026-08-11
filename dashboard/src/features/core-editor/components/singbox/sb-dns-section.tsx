import { StringArrayPopoverInput } from '@/components/common/string-array-popover-input'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import type { SectionHeaderAddPulse } from '@/features/core-editor/hooks/use-section-header-add-pulse'
import { SbField, SbListSection, SbRawJsonField, SbSelectField, SbTextField, useSbDraft, useSbOutboundTags, type WireRow } from '@/features/core-editor/components/singbox/sb-section-kit'
import { createDefaultSingBoxDnsServer, createDefaultSingBoxDnsRule } from '@pasarguard/singbox-config-kit'
import type { ColumnDef } from '@tanstack/react-table'
import { useCallback, useMemo } from 'react'
import { useTranslation } from 'react-i18next'

const DNS_SERVER_TYPES_112 = ['udp', 'tcp', 'tls', 'https', 'quic', 'h3', 'local', 'hosts', 'dhcp', 'fakeip']
const DNS_STRATEGIES = ['', 'prefer_ipv4', 'prefer_ipv6', 'ipv4_only', 'ipv6_only']

function firstList(v: unknown): string[] {
  if (Array.isArray(v)) return v.map(String)
  if (typeof v === 'string' && v) return [v]
  return []
}

export function SbDnsSection({ headerAddPulse, headerAddEpoch }: { headerAddPulse?: SectionHeaderAddPulse; headerAddEpoch?: number }) {
  const { t } = useTranslation()
  const { draft, updateSbDraft } = useSbDraft()
  const outboundTags = useSbOutboundTags(draft)
  const version = draft?.singboxVersion ?? '1.12'
  const dns = (draft?.dns ?? {}) as WireRow
  const servers = (Array.isArray(dns.servers) ? dns.servers : []) as WireRow[]
  const rules = (Array.isArray(dns.rules) ? dns.rules : []) as WireRow[]

  const setDns = useCallback((key: string, value: unknown) => updateSbDraft(d => ({ ...d, dns: { ...(d.dns as WireRow), [key]: value } as never })), [updateSbDraft])
  const setServers = useCallback((next: WireRow[]) => updateSbDraft(d => ({ ...d, dns: { ...(d.dns as WireRow), servers: next } as never })), [updateSbDraft])
  const setRules = useCallback((next: WireRow[]) => updateSbDraft(d => ({ ...d, dns: { ...(d.dns as WireRow), rules: next } as never })), [updateSbDraft])

  const serverTags = useMemo(() => servers.map(s => String(s.tag ?? '')).filter(Boolean), [servers])

  const serverColumns = useMemo<ColumnDef<WireRow, unknown>[]>(
    () => [
      { id: 'index', header: '#', cell: ({ row }) => row.index + 1 },
      { accessorKey: 'tag', header: () => t('coreEditor.col.tag', { defaultValue: 'Tag' }), cell: ({ row }) => <span className="font-mono text-xs">{String(row.original.tag ?? '')}</span> },
      { id: 'server', header: () => t('coreEditor.col.server', { defaultValue: 'Server' }), cell: ({ row }) => <span className="text-muted-foreground truncate font-mono text-[11px]">{String(row.original.server ?? row.original.address ?? '')}{row.original.type ? ` (${row.original.type})` : ''}</span>, width: 'minmax(0, 2fr)' },
      { accessorKey: 'detour', header: () => t('coreEditor.singbox.detour', { defaultValue: 'Detour' }), cell: ({ row }) => <span className="text-muted-foreground text-xs">{String(row.original.detour ?? '—')}</span>, hideOnMobile: true },
    ],
    [t],
  )

  const ruleColumns = useMemo<ColumnDef<WireRow, unknown>[]>(
    () => [
      { id: 'index', header: '#', cell: ({ row }) => row.index + 1 },
      { id: 'match', header: () => t('coreEditor.col.match', { defaultValue: 'Match' }), cell: ({ row }) => <span className="text-muted-foreground truncate font-mono text-[11px]">{JSON.stringify(Object.fromEntries(Object.entries(row.original).filter(([k]) => k !== 'server' && k !== 'action'))).slice(0, 60)}</span>, width: 'minmax(0, 2fr)' },
      { accessorKey: 'server', header: () => t('coreEditor.singbox.dnsServer', { defaultValue: 'Server' }), cell: ({ row }) => <span className="font-mono text-xs">{String(row.original.server ?? '—')}</span> },
    ],
    [t],
  )

  return (
    <div className="space-y-6">
      <Card>
        <CardContent className="grid grid-cols-1 gap-4 pt-5 sm:grid-cols-2">
          <SbSelectField label={t('coreEditor.singbox.finalServer', { defaultValue: 'Final server' })} value={dns.final} onChange={v => setDns('final', v || undefined)} options={[{ value: '' }, ...serverTags.map(v => ({ value: v }))]} hint={t('coreEditor.singbox.dnsFinalHint', { defaultValue: 'Default DNS server tag.' })} />
          <SbSelectField label={t('coreEditor.singbox.strategy', { defaultValue: 'Strategy' })} value={dns.strategy} onChange={v => setDns('strategy', v || undefined)} options={DNS_STRATEGIES.map(v => ({ value: v }))} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">{t('coreEditor.singbox.dnsServers', { defaultValue: 'Servers' })}</CardTitle>
        </CardHeader>
        <CardContent>
          <SbListSection<WireRow>
            headerAddPulse={headerAddPulse}
            headerAddEpoch={headerAddEpoch}
            sectionId="dns"
            rows={servers}
            onRowsChange={setServers}
            columns={serverColumns}
            getSearchableText={s => `${s.tag ?? ''} ${s.server ?? ''} ${s.address ?? ''} ${s.type ?? ''}`}
            emptyLabel={t('coreEditor.singbox.noDnsServers', { defaultValue: 'No DNS servers yet.' })}
            reorder
            createRow={() => createDefaultSingBoxDnsServer(version, serverTags) as WireRow}
            singleAddLabel={t('coreEditor.singbox.addServer', { defaultValue: 'Add server' })}
            dialogTitleAdd={t('coreEditor.singbox.addServer', { defaultValue: 'Add DNS server' })}
            dialogTitleEdit={t('coreEditor.singbox.editServer', { defaultValue: 'Edit DNS server' })}
            renderBody={(row, patch) => {
              const set = (k: string, v: unknown) => patch({ ...row, [k]: v })
              return (
                <>
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <SbTextField label={t('coreEditor.col.tag', { defaultValue: 'Tag' })} value={row.tag} onChange={v => set('tag', v)} />
                    {version === '1.12' ? (
                      <>
                        <SbSelectField label={t('coreEditor.col.type', { defaultValue: 'Type' })} value={row.type} onChange={v => set('type', v)} options={DNS_SERVER_TYPES_112.map(v => ({ value: v }))} />
                        <SbTextField label={t('coreEditor.col.server', { defaultValue: 'Server' })} value={row.server} onChange={v => set('server', v)} placeholder="8.8.8.8" />
                      </>
                    ) : (
                      <SbTextField label={t('coreEditor.singbox.address', { defaultValue: 'Address' })} value={row.address} onChange={v => set('address', v)} placeholder="tls://1.1.1.1" className="sm:col-span-2" />
                    )}
                    <SbSelectField label={t('coreEditor.singbox.detour', { defaultValue: 'Detour' })} value={row.detour} onChange={v => set('detour', v || undefined)} options={[{ value: '' }, ...outboundTags.map(v => ({ value: v }))]} />
                  </div>
                  <SbRawJsonField row={row} patch={patch} />
                </>
              )
            }}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">{t('coreEditor.singbox.dnsRules', { defaultValue: 'Rules' })}</CardTitle>
        </CardHeader>
        <CardContent>
          <SbListSection<WireRow>
            sectionId="dns"
            rows={rules}
            onRowsChange={setRules}
            columns={ruleColumns}
            getSearchableText={r => JSON.stringify(r)}
            emptyLabel={t('coreEditor.singbox.noDnsRules', { defaultValue: 'No DNS rules yet.' })}
            reorder
            createRow={() => createDefaultSingBoxDnsRule() as WireRow}
            singleAddLabel={t('coreEditor.singbox.addRule', { defaultValue: 'Add rule' })}
            dialogTitleAdd={t('coreEditor.singbox.addDnsRule', { defaultValue: 'Add DNS rule' })}
            dialogTitleEdit={t('coreEditor.singbox.editDnsRule', { defaultValue: 'Edit DNS rule' })}
            renderBody={(row, patch) => {
              const set = (k: string, v: unknown) => {
                const next = { ...row }
                if (v === undefined || (Array.isArray(v) && v.length === 0) || v === '') delete (next as Record<string, unknown>)[k]
                else (next as Record<string, unknown>)[k] = v
                patch(next)
              }
              return (
                <>
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <SbField label="Domain">
                      <StringArrayPopoverInput value={firstList(row.domain)} onChange={v => set('domain', v)} placeholder="example.com" />
                    </SbField>
                    <SbField label="Domain suffix">
                      <StringArrayPopoverInput value={firstList(row.domain_suffix)} onChange={v => set('domain_suffix', v)} placeholder=".ir" />
                    </SbField>
                    <SbField label={t('coreEditor.singbox.ruleSet', { defaultValue: 'Rule set' })}>
                      <StringArrayPopoverInput value={firstList(row.rule_set)} onChange={v => set('rule_set', v)} placeholder="rule-set tag" />
                    </SbField>
                    <SbSelectField label={t('coreEditor.singbox.dnsServer', { defaultValue: 'Server' })} value={row.server} onChange={v => set('server', v || undefined)} options={[{ value: '' }, ...serverTags.map(v => ({ value: v }))]} />
                  </div>
                  <SbRawJsonField row={row} patch={patch} />
                </>
              )
            }}
          />
        </CardContent>
      </Card>
    </div>
  )
}
