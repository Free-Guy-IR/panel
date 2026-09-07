import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PasswordInput } from '@/components/ui/password-input'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { l2tpDraftIssues, l2tpPskLeftToPanel } from '@/features/core-editor/kit/l2tp-adapter'
import { useCoreEditorStore } from '@/features/core-editor/state/core-editor-store'
import useDirDetection from '@/hooks/use-dir-detection'
import { cn } from '@/lib/utils'
import type { L2TPCoreDraft, L2TPValidationIssue } from '@pasarguard/l2tp-config-kit'
import { generateL2TPPsk, L2TP_PORT, PSK_FORBIDDEN_CHARACTERS, PSK_MAX_LENGTH, PSK_MIN_LENGTH } from '@pasarguard/l2tp-config-kit'
import { Check, Copy, RefreshCcw } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

function issueFor(issues: L2TPValidationIssue[], field: keyof L2TPCoreDraft): string | undefined {
  return issues.find(i => i.path === `/${field}`)?.message
}

export function L2TPCoreForm({ className }: { className?: string }) {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const draft = useCoreEditorStore(s => s.l2tpDraft)
  const updateL2tpDraft = useCoreEditorStore(s => s.updateL2tpDraft)
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')

  const issues = useMemo(() => (draft ? l2tpDraftIssues(draft) : []), [draft])

  if (!draft) return null

  const set = <K extends keyof L2TPCoreDraft>(key: K, value: L2TPCoreDraft[K]) => {
    updateL2tpDraft(d => ({ ...d, [key]: value }))
  }

  const pskFromPanel = l2tpPskLeftToPanel(draft)

  const copyPsk = async () => {
    if (!draft.psk) return
    try {
      await navigator.clipboard.writeText(draft.psk)
      setCopyState('copied')
    } catch {
      setCopyState('failed')
    }
  }

  const inboundTagError = issueFor(issues, 'inboundTag')
  const serverAddrError = issueFor(issues, 'serverAddr')
  const pskError = issueFor(issues, 'psk')
  const poolError = issueFor(issues, 'pool')
  const localIpError = issueFor(issues, 'localIp')
  const egressInterfaceError = issueFor(issues, 'egressInterface')
  const dnsError = issueFor(issues, 'dns')
  const ikeProposalsError = issueFor(issues, 'ikeProposals')
  const espProposalsError = issueFor(issues, 'espProposals')

  return (
    <div className={cn('space-y-5', className)}>
      <p className="text-muted-foreground text-sm">
        {t('coreEditor.l2tp.blurb', {
          port: L2TP_PORT,
          defaultValue: `One L2TP/IPsec listener shared by every authorized user. The L2TP port is fixed at ${L2TP_PORT} and is not configurable.`,
        })}
      </p>

      <div className="grid grid-cols-1 gap-x-4 gap-y-5 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label>{t('coreEditor.l2tp.fields.inboundTag', { defaultValue: 'Inbound tag' })}</Label>
          <Input value={draft.inboundTag} dir="ltr" className="text-xs" isError={!!inboundTagError} onChange={e => set('inboundTag', e.target.value)} placeholder="L2TP" />
          {inboundTagError && <p className="text-destructive text-[0.8rem] font-medium">{inboundTagError}</p>}
          <p className="text-muted-foreground text-[11px]">
            {t('coreEditor.l2tp.fields.inboundTagHint', { defaultValue: 'The tag groups reference to give users access to this core. Letters, digits, "_", "." and "-", up to 64 characters.' })}
          </p>
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.l2tp.fields.serverAddr', { defaultValue: 'Server address' })}</Label>
          <Input value={draft.serverAddr} dir="ltr" className="text-xs" isError={!!serverAddrError} onChange={e => set('serverAddr', e.target.value)} placeholder="vpn.example.com" />
          {serverAddrError && <p className="text-destructive text-[0.8rem] font-medium">{serverAddrError}</p>}
          <p className="text-muted-foreground text-[11px]">{t('coreEditor.l2tp.fields.serverAddrHint', { defaultValue: 'The public IP address or hostname clients connect to.' })}</p>
        </div>

        <div className="space-y-1.5 sm:col-span-2">
          <Label>{t('coreEditor.l2tp.fields.psk', { defaultValue: 'Pre-shared key' })}</Label>
          <div dir="ltr" className={cn('flex items-center gap-2', dir === 'rtl' ? 'flex-row-reverse' : 'flex-row')}>
            <div className="min-w-0 flex-1">
              <PasswordInput
                value={draft.psk}
                className="text-xs"
                isError={!!pskError}
                placeholder={t('coreEditor.l2tp.fields.pskPlaceholder', { defaultValue: 'Leave empty and the panel generates a random key' })}
                onChange={e => {
                  setCopyState('idle')
                  set('psk', e.target.value)
                }}
              />
            </div>
            <Button
              type="button"
              size="icon"
              variant="outline"
              className="h-9 w-9 shrink-0"
              onClick={copyPsk}
              disabled={!draft.psk}
              title={t('coreEditor.l2tp.copyPsk', { defaultValue: 'Copy pre-shared key' })}
            >
              {copyState === 'copied' ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
            </Button>
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="h-9 w-9 shrink-0"
              onClick={() => {
                setCopyState('idle')
                set('psk', generateL2TPPsk())
              }}
              title={t('coreEditor.l2tp.generatePsk', { defaultValue: 'Generate pre-shared key' })}
            >
              <RefreshCcw className="h-3 w-3" />
            </Button>
          </div>
          {pskError && <p className="text-destructive text-[0.8rem] font-medium">{pskError}</p>}
          {copyState === 'failed' && (
            <p className="text-destructive text-[0.8rem] font-medium">
              {t('coreEditor.l2tp.fields.pskCopyFailed', { defaultValue: 'The browser refused to copy. Reveal the key with the eye button and copy it by hand.' })}
            </p>
          )}
          <p className="text-muted-foreground text-[11px]">
            {pskFromPanel
              ? t('coreEditor.l2tp.fields.pskEmptyHint', {
                  defaultValue: 'Left empty: the panel generates a random key when you save. On an existing core that replaces the current key, so every already-configured client has to be updated.',
                })
              : t('coreEditor.l2tp.fields.pskHint', {
                  min: PSK_MIN_LENGTH,
                  max: PSK_MAX_LENGTH,
                  forbidden: PSK_FORBIDDEN_CHARACTERS.join(' '),
                  defaultValue: `Shared by every L2TP client. Leave the field empty and the panel generates a random key when you save. ${PSK_MIN_LENGTH}-${PSK_MAX_LENGTH} printable ASCII characters, no spaces, and none of ${PSK_FORBIDDEN_CHARACTERS.join(' ')}`,
                })}
          </p>
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.l2tp.fields.pool', { defaultValue: 'Address pool' })}</Label>
          <Input value={draft.pool} dir="ltr" className="text-xs" isError={!!poolError} onChange={e => set('pool', e.target.value)} placeholder="10.10.10.0/24" />
          {poolError && <p className="text-destructive text-[0.8rem] font-medium">{poolError}</p>}
          <p className="text-muted-foreground text-[11px]">{t('coreEditor.l2tp.fields.poolHint', { defaultValue: 'IPv4 CIDR handed out to connecting clients. Prefix between /8 and /29.' })}</p>
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.l2tp.fields.localIp', { defaultValue: 'Local IP (optional)' })}</Label>
          <Input value={draft.localIp} dir="ltr" className="text-xs" isError={!!localIpError} onChange={e => set('localIp', e.target.value)} placeholder="10.10.10.1" />
          {localIpError && <p className="text-destructive text-[0.8rem] font-medium">{localIpError}</p>}
          <p className="text-muted-foreground text-[11px]">{t('coreEditor.l2tp.fields.localIpHint', { defaultValue: 'The server address inside the tunnel. Leave empty to use the first host of the pool.' })}</p>
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.l2tp.fields.egressInterface', { defaultValue: 'Egress interface (optional)' })}</Label>
          <Input value={draft.egressInterface} dir="ltr" className="text-xs" isError={!!egressInterfaceError} onChange={e => set('egressInterface', e.target.value)} placeholder="eth0" />
          {egressInterfaceError && <p className="text-destructive text-[0.8rem] font-medium">{egressInterfaceError}</p>}
          <p className="text-muted-foreground text-[11px]">{t('coreEditor.l2tp.fields.egressInterfaceHint', { defaultValue: 'Interface client traffic is NATed out of. Leave empty to let the node pick.' })}</p>
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.l2tp.fields.dns', { defaultValue: 'DNS servers' })}</Label>
          <Textarea rows={3} value={draft.dns} dir="ltr" className={cn('text-xs', dnsError && 'border-destructive')} aria-invalid={!!dnsError} onChange={e => set('dns', e.target.value)} placeholder={'1.1.1.1\n8.8.8.8'} />
          {dnsError && <p className="text-destructive text-[0.8rem] font-medium">{dnsError}</p>}
          <p className="text-muted-foreground text-[11px]">{t('coreEditor.l2tp.fields.dnsHint', { defaultValue: 'One IPv4 address per line, pushed to clients. Leave empty for 1.1.1.1 and 8.8.8.8.' })}</p>
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.l2tp.fields.ikeProposals', { defaultValue: 'IKE proposals (optional)' })}</Label>
          <Textarea rows={3} value={draft.ikeProposals} dir="ltr" className={cn('text-xs', ikeProposalsError && 'border-destructive')} aria-invalid={!!ikeProposalsError} onChange={e => set('ikeProposals', e.target.value)} placeholder="aes256-sha256-modp2048" />
          {ikeProposalsError && <p className="text-destructive text-[0.8rem] font-medium">{ikeProposalsError}</p>}
          <p className="text-muted-foreground text-[11px]">{t('coreEditor.l2tp.fields.ikeProposalsHint', { defaultValue: 'One strongSwan proposal per line. Leave empty to use the node defaults.' })}</p>
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.l2tp.fields.espProposals', { defaultValue: 'ESP proposals (optional)' })}</Label>
          <Textarea rows={3} value={draft.espProposals} dir="ltr" className={cn('text-xs', espProposalsError && 'border-destructive')} aria-invalid={!!espProposalsError} onChange={e => set('espProposals', e.target.value)} placeholder="aes256-sha256" />
          {espProposalsError && <p className="text-destructive text-[0.8rem] font-medium">{espProposalsError}</p>}
          <p className="text-muted-foreground text-[11px]">{t('coreEditor.l2tp.fields.espProposalsHint', { defaultValue: 'One strongSwan proposal per line. Leave empty to use the node defaults.' })}</p>
        </div>

        <div className="space-y-1.5 sm:col-span-2">
          <div className={cn('flex items-center gap-3', dir === 'rtl' ? 'flex-row-reverse' : 'flex-row')}>
            <Switch checked={draft.legacyClients} onCheckedChange={checked => set('legacyClients', checked)} id="l2tp-legacy-clients" />
            <Label htmlFor="l2tp-legacy-clients">{t('coreEditor.l2tp.fields.legacyClients', { defaultValue: 'Allow legacy clients' })}</Label>
          </div>
          <p className="text-muted-foreground text-[11px]">
            {t('coreEditor.l2tp.fields.legacyClientsHint', { defaultValue: 'Adds older, weaker IKE and ESP algorithms so out-of-date clients can connect. Leave off unless you need them.' })}
          </p>
        </div>
      </div>
    </div>
  )
}
