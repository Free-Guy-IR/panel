import { StringArrayPopoverInput } from '@/components/common/string-array-popover-input'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PasswordInput } from '@/components/ui/password-input'
import { Separator } from '@/components/ui/separator'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import useDirDetection from '@/hooks/use-dir-detection'
import { cn } from '@/lib/utils'
import type { HysteriaInboundDraft, SingBoxCertMode, SingBoxValidationIssue } from '@pasarguard/singbox-config-kit'
import { RefreshCcw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface SingBoxInboundFormProps {
  inbound: HysteriaInboundDraft
  issues: SingBoxValidationIssue[]
  onChange: (updater: (draft: HysteriaInboundDraft) => HysteriaInboundDraft) => void
}

function issueFor(issues: SingBoxValidationIssue[], suffix: string): string | undefined {
  return issues.find(i => i.path.endsWith(`/${suffix}`))?.message
}

function randomPort(): number {
  return Math.floor(Math.random() * (65535 - 10000 + 1)) + 10000
}

function randomObfsPassword(): string {
  const bytes = new Uint8Array(16)
  globalThis.crypto.getRandomValues(bytes)
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
}

export function SingBoxInboundForm({ inbound, issues, onChange }: SingBoxInboundFormProps) {
  const { t } = useTranslation()
  const dir = useDirDetection()

  const set = <K extends keyof HysteriaInboundDraft>(key: K, value: HysteriaInboundDraft[K]) => {
    onChange(d => ({ ...d, [key]: value }))
  }

  const tagError = issueFor(issues, 'tag')
  const portError = issueFor(issues, 'listenPort')
  const upMbpsError = issueFor(issues, 'upMbps')
  const downMbpsError = issueFor(issues, 'downMbps')
  const obfsPasswordError = issueFor(issues, 'obfsPassword')

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 gap-x-4 gap-y-5 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.fields.tag', { defaultValue: 'Tag' })}</Label>
          <Input value={inbound.tag} dir="ltr" className="text-xs" isError={!!tagError} onChange={e => set('tag', e.target.value)} placeholder="Hysteria2" />
          {tagError && <p className="text-destructive text-[0.8rem] font-medium">{tagError}</p>}
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.fields.listen', { defaultValue: 'Listen address' })}</Label>
          <Input value={inbound.listen} dir="ltr" className="text-xs" onChange={e => set('listen', e.target.value)} placeholder="::" />
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.fields.listenPort', { defaultValue: 'Listen port' })}</Label>
          <div dir="ltr" className={cn('flex items-center gap-2', dir === 'rtl' ? 'flex-row-reverse' : 'flex-row')}>
            <div className="min-w-0 flex-1">
              <Input
                type="text"
                inputMode="numeric"
                value={String(inbound.listenPort)}
                className="text-xs"
                isError={!!portError}
                onChange={e => set('listenPort', e.target.value)}
                placeholder="8443"
              />
            </div>
            <Button type="button" size="icon" variant="ghost" className="h-9 w-9 shrink-0" onClick={() => set('listenPort', randomPort())} title={t('coreEditor.inbound.randomPort', { defaultValue: 'Generate random port' })}>
              <RefreshCcw className="h-3 w-3" />
            </Button>
          </div>
          {portError && <p className="text-destructive text-[0.8rem] font-medium">{portError}</p>}
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.fields.masquerade', { defaultValue: 'Masquerade URL' })}</Label>
          <Input value={inbound.masquerade} dir="ltr" className="text-xs" onChange={e => set('masquerade', e.target.value)} placeholder="https://example.com" />
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.fields.upMbps', { defaultValue: 'Up mbps' })}</Label>
          <Input type="text" inputMode="numeric" value={inbound.upMbps} dir="ltr" className="text-xs" isError={!!upMbpsError} onChange={e => set('upMbps', e.target.value)} placeholder="100" />
          {upMbpsError && <p className="text-destructive text-[0.8rem] font-medium">{upMbpsError}</p>}
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.fields.downMbps', { defaultValue: 'Down mbps' })}</Label>
          <Input type="text" inputMode="numeric" value={inbound.downMbps} dir="ltr" className="text-xs" isError={!!downMbpsError} onChange={e => set('downMbps', e.target.value)} placeholder="100" />
          {downMbpsError && <p className="text-destructive text-[0.8rem] font-medium">{downMbpsError}</p>}
        </div>

        <div className="flex items-start gap-2 rounded-md border border-dashed px-3 py-2 sm:col-span-2">
          <Switch id={`sb-ignore-bandwidth-${inbound.tag}`} checked={inbound.ignoreClientBandwidth} onCheckedChange={checked => set('ignoreClientBandwidth', checked === true)} />
          <div className="grid gap-0.5 leading-tight">
            <label htmlFor={`sb-ignore-bandwidth-${inbound.tag}`} className="cursor-pointer text-xs font-medium">
              {t('coreEditor.singbox.fields.ignoreClientBandwidth', { defaultValue: 'Ignore client bandwidth' })}
            </label>
            <p className="text-muted-foreground text-[11px]">
              {t('coreEditor.singbox.fields.ignoreClientBandwidthHint', { defaultValue: 'Ignore the bandwidth values reported by the client and always use the values above.' })}
            </p>
          </div>
        </div>
      </div>

      <Separator />

      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h4 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">{t('coreEditor.singbox.obfs.title', { defaultValue: 'Obfuscation' })}</h4>
          <div className="flex items-center gap-2">
            <Label htmlFor={`sb-obfs-enabled-${inbound.tag}`} className="text-muted-foreground text-xs font-normal">
              {t('coreEditor.singbox.obfs.enable', { defaultValue: 'Salamander obfuscation' })}
            </Label>
            <Switch id={`sb-obfs-enabled-${inbound.tag}`} checked={inbound.obfsEnabled} onCheckedChange={checked => set('obfsEnabled', checked === true)} />
          </div>
        </div>
        {inbound.obfsEnabled && (
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.obfs.password', { defaultValue: 'Obfuscation password' })}</Label>
            <div dir="ltr" className={cn('flex items-center gap-2', dir === 'rtl' ? 'flex-row-reverse' : 'flex-row')}>
              <div className="min-w-0 flex-1">
                <PasswordInput className="text-xs" isError={!!obfsPasswordError} value={inbound.obfsPassword} onChange={e => set('obfsPassword', e.target.value)} />
              </div>
              <Button
                type="button"
                size="icon"
                variant="ghost"
                className="h-9 w-9 shrink-0"
                onClick={() => set('obfsPassword', randomObfsPassword())}
                title={t('coreEditor.singbox.obfs.generate', { defaultValue: 'Generate password' })}
              >
                <RefreshCcw className="h-3 w-3" />
              </Button>
            </div>
            {obfsPasswordError && <p className="text-destructive text-[0.8rem] font-medium">{obfsPasswordError}</p>}
          </div>
        )}
      </div>

      <Separator />

      <div className="space-y-3">
        <h4 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">{t('coreEditor.singbox.tls.title', { defaultValue: 'TLS' })}</h4>
        <p className="text-muted-foreground text-[11px]">{t('coreEditor.singbox.tls.alwaysEnabledHint', { defaultValue: 'Hysteria2 always requires TLS; it cannot be disabled for this inbound.' })}</p>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.tls.serverName', { defaultValue: 'Server name (SNI)' })}</Label>
            <Input value={inbound.tlsServerName} dir="ltr" className="text-xs" onChange={e => set('tlsServerName', e.target.value)} placeholder="example.com" />
          </div>
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.tls.alpn', { defaultValue: 'ALPN' })}</Label>
            <StringArrayPopoverInput
              value={[...inbound.tlsAlpn]}
              onChange={next => set('tlsAlpn', next)}
              placeholder={t('coreEditor.singbox.tls.alpnPlaceholder', { defaultValue: 'h3' })}
              addPlaceholder="h3"
              itemsLabel={t('coreEditor.singbox.tls.alpn', { defaultValue: 'ALPN' })}
            />
          </div>
        </div>

        <div className="space-y-2 rounded-md border p-3">
          <Label className="text-xs font-medium">{t('coreEditor.singbox.tls.certificate', { defaultValue: 'Certificate' })}</Label>
          <Tabs
            value={inbound.certMode}
            onValueChange={v => {
              const mode = (v === 'content' ? 'content' : 'path') as SingBoxCertMode
              if (mode === inbound.certMode) return
              set('certMode', mode)
            }}
          >
            <TabsList className="grid w-full grid-cols-2">
              <TabsTrigger value="path">{t('coreEditor.inbound.tlsCertificates.filePathTab', { defaultValue: 'File path' })}</TabsTrigger>
              <TabsTrigger value="content">{t('coreEditor.inbound.tlsCertificates.fileContentTab', { defaultValue: 'File content' })}</TabsTrigger>
            </TabsList>
          </Tabs>

          {inbound.certMode === 'path' ? (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <div className="space-y-1">
                <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.inbound.tlsCertificates.certificateFile', { defaultValue: 'Certificate file' })}</Label>
                <Input dir="ltr" className="text-xs" placeholder="/path/fullchain.pem" value={inbound.certificateFile} onChange={e => set('certificateFile', e.target.value)} />
              </div>
              <div className="space-y-1">
                <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.inbound.tlsCertificates.keyFile', { defaultValue: 'Key file' })}</Label>
                <Input dir="ltr" className="text-xs" placeholder="/path/key.pem" value={inbound.keyFile} onChange={e => set('keyFile', e.target.value)} />
              </div>
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <div className="space-y-1">
                <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.inbound.tlsCertificates.certificateContent', { defaultValue: 'Certificate content' })}</Label>
                <Textarea dir="ltr" rows={5} className="text-xs" placeholder="-----BEGIN CERTIFICATE-----" value={inbound.certificate} onChange={e => set('certificate', e.target.value)} />
              </div>
              <div className="space-y-1">
                <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.inbound.tlsCertificates.keyContent', { defaultValue: 'Key content' })}</Label>
                <Textarea dir="ltr" rows={5} className="text-xs" placeholder="-----BEGIN PRIVATE KEY-----" value={inbound.key} onChange={e => set('key', e.target.value)} />
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
