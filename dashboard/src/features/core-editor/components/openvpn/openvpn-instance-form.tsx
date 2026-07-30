import { StringArrayPopoverInput } from '@/components/common/string-array-popover-input'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import { Switch } from '@/components/ui/switch'
import useDirDetection from '@/hooks/use-dir-detection'
import { cn } from '@/lib/utils'
import type { OpenVPNInstanceDraft, OpenVPNValidationIssue } from '@pasarguard/openvpn-config-kit'
import { RefreshCcw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface OpenVPNInstanceFormProps {
  instance: OpenVPNInstanceDraft
  issues: OpenVPNValidationIssue[]
  onChange: (updater: (draft: OpenVPNInstanceDraft) => OpenVPNInstanceDraft) => void
}

const CIPHER_OPTIONS = ['AES-256-GCM', 'AES-128-GCM', 'CHACHA20-POLY1305'] as const
const AUTH_OPTIONS = ['SHA256', 'SHA384', 'SHA512'] as const

function issueFor(issues: OpenVPNValidationIssue[], suffix: string): string | undefined {
  return issues.find(i => i.path.endsWith(`/${suffix}`))?.message
}

function randomPort(): number {
  return Math.floor(Math.random() * (65535 - 10000 + 1)) + 10000
}

export function OpenVPNInstanceForm({ instance, issues, onChange }: OpenVPNInstanceFormProps) {
  const { t } = useTranslation()
  const dir = useDirDetection()

  const set = <K extends keyof OpenVPNInstanceDraft>(key: K, value: OpenVPNInstanceDraft[K]) => {
    onChange(d => ({ ...d, [key]: value }))
  }

  const tagError = issueFor(issues, 'tag')
  const portError = issueFor(issues, 'port')
  const networkError = issueFor(issues, 'network')
  const maxClientsError = issueFor(issues, 'maxClients')
  const verbError = issueFor(issues, 'verb')

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 gap-x-4 gap-y-5 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label>{t('coreEditor.openvpn.fields.tag', { defaultValue: 'Tag' })}</Label>
          <Input value={instance.tag} dir="ltr" className="text-xs" isError={!!tagError} onChange={e => set('tag', e.target.value)} placeholder="OpenVPN" />
          {tagError && <p className="text-destructive text-[0.8rem] font-medium">{tagError}</p>}
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.openvpn.fields.protocol', { defaultValue: 'Protocol' })}</Label>
          <Select value={instance.protocol} onValueChange={v => set('protocol', v === 'tcp' ? 'tcp' : 'udp')}>
            <SelectTrigger className="text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="udp">UDP</SelectItem>
              <SelectItem value="tcp">TCP</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.openvpn.fields.port', { defaultValue: 'Port' })}</Label>
          <div dir="ltr" className={cn('flex items-center gap-2', dir === 'rtl' ? 'flex-row-reverse' : 'flex-row')}>
            <div className="min-w-0 flex-1">
              <Input
                type="text"
                inputMode="numeric"
                value={String(instance.port)}
                className="text-xs"
                isError={!!portError}
                onChange={e => set('port', e.target.value)}
                placeholder="1194"
              />
            </div>
            <Button type="button" size="icon" variant="ghost" className="h-9 w-9 shrink-0" onClick={() => set('port', randomPort())} title={t('coreEditor.inbound.randomPort', { defaultValue: 'Generate random port' })}>
              <RefreshCcw className="h-3 w-3" />
            </Button>
          </div>
          {portError && <p className="text-destructive text-[0.8rem] font-medium">{portError}</p>}
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.openvpn.fields.network', { defaultValue: 'Network (CIDR)' })}</Label>
          <Input value={instance.network} dir="ltr" className="text-xs" isError={!!networkError} onChange={e => set('network', e.target.value)} placeholder="10.8.0.0/24" />
          {networkError && <p className="text-destructive text-[0.8rem] font-medium">{networkError}</p>}
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.openvpn.fields.cipher', { defaultValue: 'Cipher' })}</Label>
          <Select value={instance.cipher} onValueChange={v => set('cipher', v)}>
            <SelectTrigger className="text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {CIPHER_OPTIONS.map(c => (
                <SelectItem key={c} value={c}>
                  {c}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.openvpn.fields.auth', { defaultValue: 'Auth digest' })}</Label>
          <Select value={instance.auth} onValueChange={v => set('auth', v)}>
            <SelectTrigger className="text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {AUTH_OPTIONS.map(a => (
                <SelectItem key={a} value={a}>
                  {a}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.openvpn.fields.keepalive', { defaultValue: 'Keepalive' })}</Label>
          <Input value={instance.keepalive} dir="ltr" className="text-xs" onChange={e => set('keepalive', e.target.value)} placeholder="10 60" />
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.openvpn.fields.maxClients', { defaultValue: 'Max clients' })}</Label>
          <Input
            type="text"
            inputMode="numeric"
            value={instance.maxClients}
            dir="ltr"
            className="text-xs"
            isError={!!maxClientsError}
            onChange={e => set('maxClients', e.target.value)}
            placeholder={t('coreEditor.openvpn.fields.maxClientsPlaceholder', { defaultValue: 'Unlimited' })}
          />
          {maxClientsError && <p className="text-destructive text-[0.8rem] font-medium">{maxClientsError}</p>}
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.openvpn.fields.verb', { defaultValue: 'Log verbosity (verb)' })}</Label>
          <Input
            type="text"
            inputMode="numeric"
            value={instance.verb}
            dir="ltr"
            className="text-xs"
            isError={!!verbError}
            onChange={e => set('verb', e.target.value)}
            placeholder="3"
          />
          {verbError && <p className="text-destructive text-[0.8rem] font-medium">{verbError}</p>}
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.openvpn.fields.dnsServers', { defaultValue: 'DNS servers' })}</Label>
          <StringArrayPopoverInput
            value={[...instance.dnsServers]}
            onChange={next => set('dnsServers', next)}
            placeholder={t('coreEditor.openvpn.fields.dnsServersPlaceholder', { defaultValue: '1.1.1.1' })}
            addPlaceholder="1.1.1.1"
            itemsLabel={t('coreEditor.openvpn.fields.dnsServers', { defaultValue: 'DNS servers' })}
          />
        </div>
      </div>

      <Separator />

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="flex items-start gap-2 rounded-md border border-dashed px-3 py-2">
          <Switch id={`ov-redirect-gateway-${instance.tag}`} checked={instance.redirectGateway} onCheckedChange={checked => set('redirectGateway', checked === true)} />
          <div className="grid gap-0.5 leading-tight">
            <label htmlFor={`ov-redirect-gateway-${instance.tag}`} className="cursor-pointer text-xs font-medium">
              {t('coreEditor.openvpn.fields.redirectGateway', { defaultValue: 'Redirect gateway' })}
            </label>
            <p className="text-muted-foreground text-[11px]">
              {t('coreEditor.openvpn.fields.redirectGatewayHint', { defaultValue: 'Route all client traffic through this tunnel.' })}
            </p>
          </div>
        </div>

        <div className="flex items-start gap-2 rounded-md border border-dashed px-3 py-2">
          <Switch id={`ov-duplicate-cn-${instance.tag}`} checked={instance.duplicateCN} onCheckedChange={checked => set('duplicateCN', checked === true)} />
          <div className="grid gap-0.5 leading-tight">
            <label htmlFor={`ov-duplicate-cn-${instance.tag}`} className="cursor-pointer text-xs font-medium">
              {t('coreEditor.openvpn.fields.duplicateCn', { defaultValue: 'Allow duplicate CN' })}
            </label>
            <p className="text-muted-foreground text-[11px]">
              {t('coreEditor.openvpn.fields.duplicateCnHint', { defaultValue: 'Allow the same user to connect from multiple devices at once.' })}
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
