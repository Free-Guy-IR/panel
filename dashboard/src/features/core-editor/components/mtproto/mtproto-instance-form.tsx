import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import useDirDetection from '@/hooks/use-dir-detection'
import { cn } from '@/lib/utils'
import type { MTProtoInstanceDraft, MTProtoValidationIssue } from '@pasarguard/mtproto-config-kit'
import { RefreshCcw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface MTProtoInstanceFormProps {
  instance: MTProtoInstanceDraft
  issues: MTProtoValidationIssue[]
  onChange: (updater: (draft: MTProtoInstanceDraft) => MTProtoInstanceDraft) => void
}

function issueFor(issues: MTProtoValidationIssue[], suffix: string): string | undefined {
  return issues.find(i => i.path.endsWith(`/${suffix}`))?.message
}

function randomPort(): number {
  return Math.floor(Math.random() * (65535 - 10000 + 1)) + 10000
}

export function MTProtoInstanceForm({ instance, issues, onChange }: MTProtoInstanceFormProps) {
  const { t } = useTranslation()
  const dir = useDirDetection()

  const set = <K extends keyof MTProtoInstanceDraft>(key: K, value: MTProtoInstanceDraft[K]) => {
    onChange(d => ({ ...d, [key]: value }))
  }

  const tagError = issueFor(issues, 'tag')
  const portError = issueFor(issues, 'port')
  const domainError = issueFor(issues, 'fakeTlsDomain')
  const adTagError = issueFor(issues, 'adTag')

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 gap-x-4 gap-y-5 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label>{t('coreEditor.mtproto.fields.tag', { defaultValue: 'Tag' })}</Label>
          <Input value={instance.tag} dir="ltr" className="text-xs" isError={!!tagError} onChange={e => set('tag', e.target.value)} placeholder="MTProto" />
          {tagError && <p className="text-destructive text-[0.8rem] font-medium">{tagError}</p>}
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.mtproto.fields.port', { defaultValue: 'Port' })}</Label>
          <div dir="ltr" className={cn('flex items-center gap-2', dir === 'rtl' ? 'flex-row-reverse' : 'flex-row')}>
            <div className="min-w-0 flex-1">
              <Input type="text" inputMode="numeric" value={String(instance.port)} className="text-xs" isError={!!portError} onChange={e => set('port', e.target.value)} placeholder="443" />
            </div>
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="h-9 w-9 shrink-0"
              onClick={() => set('port', randomPort())}
              title={t('coreEditor.inbound.randomPort', { defaultValue: 'Generate random port' })}
            >
              <RefreshCcw className="h-3 w-3" />
            </Button>
          </div>
          {portError && <p className="text-destructive text-[0.8rem] font-medium">{portError}</p>}
        </div>

        <div className="space-y-1.5 sm:col-span-2">
          <Label>{t('coreEditor.mtproto.fields.mode', { defaultValue: 'Mode' })}</Label>
          <Select value={instance.mode} onValueChange={v => set('mode', v === 'plain' ? 'plain' : 'faketls')}>
            <SelectTrigger className="text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="faketls">{t('coreEditor.mtproto.mode.faketlsLong', { defaultValue: 'Fake-TLS (ee secret, disguised as HTTPS)' })}</SelectItem>
              <SelectItem value="plain">{t('coreEditor.mtproto.mode.plainLong', { defaultValue: 'Plain (dd secret, no TLS, no SNI)' })}</SelectItem>
            </SelectContent>
          </Select>
          <p className="text-muted-foreground text-[11px]">
            {t('coreEditor.mtproto.fields.modeHint', {
              defaultValue:
                'Fake-TLS wraps the proxy in a TLS-looking handshake and needs a cover domain. Plain uses the classic obfuscated2 transport with no TLS layer; networks that fingerprint Fake-TLS often let plain traffic through. Each mode needs its own instance and port; users automatically get a matching ee- or dd-prefixed secret.',
            })}
          </p>
        </div>

        {instance.mode === 'faketls' && (
          <div className="space-y-1.5 sm:col-span-2">
            <Label>{t('coreEditor.mtproto.fields.fakeTlsDomain', { defaultValue: 'Fake-TLS domain' })}</Label>
            <Input value={instance.fakeTlsDomain} dir="ltr" className="text-xs" isError={!!domainError} onChange={e => set('fakeTlsDomain', e.target.value)} placeholder="www.example.com" />
            {domainError && <p className="text-destructive text-[0.8rem] font-medium">{domainError}</p>}
            <p className="text-muted-foreground text-[11px]">
              {t('coreEditor.mtproto.fields.fakeTlsDomainHint', {
                defaultValue:
                  'Any real, publicly reachable domain works - it does not need to be owned by you or have any TLS certificate of its own. Every connecting client is validated against the secret alone; this domain is only used to disguise unauthenticated probes as ordinary HTTPS traffic to that site.',
              })}
            </p>
          </div>
        )}

        {instance.mode === 'faketls' && (
          <div className="space-y-1.5 sm:col-span-2">
            <Label>{t('coreEditor.mtproto.fields.adTag', { defaultValue: 'Sponsor channel ad tag (optional)' })}</Label>
            <Input value={instance.adTag} dir="ltr" className="text-xs" isError={!!adTagError} onChange={e => set('adTag', e.target.value)} placeholder="dcabcdef0123456789abcdef01234567" />
            {adTagError && <p className="text-destructive text-[0.8rem] font-medium">{adTagError}</p>}
            <p className="text-muted-foreground text-[11px]">
              {t('coreEditor.mtproto.fields.adTagHint', {
                defaultValue:
                  "Optional. Leave empty for a plain, un-promoted proxy. To show a sponsor channel, register this proxy with Telegram's @MTProxybot: send it /newproxy, then the host and port your users connect to, then one client secret. The secret it wants is the ee-prefixed value from any active user's proxy link, the part after secret= in tg://proxy?...&secret=ee... which you can copy from that user's subscription page. The bot replies with a tag; paste it above. Setting a tag routes this instance through Telegram's middle-proxy servers rather than connecting directly, which adds one network hop, and an invalid tag makes clients connect but carry no traffic.",
              })}
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
