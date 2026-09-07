import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import useDirDetection from '@/hooks/use-dir-detection'
import { cn } from '@/lib/utils'
import type { MTProtoInstanceDraft, MTProtoValidationIssue } from '@pasarguard/mtproto-config-kit'
import { Check, Copy, RefreshCcw } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { fetcher } from '@/service/http'

interface MTProtoInstanceFormProps {
  instance: MTProtoInstanceDraft
  issues: MTProtoValidationIssue[]
  onChange: (updater: (draft: MTProtoInstanceDraft) => MTProtoInstanceDraft) => void
  coreId?: number
}

function RegistrationSecret({ coreId, tag }: { coreId?: number; tag: string }) {
  const { t } = useTranslation()
  const [secret, setSecret] = useState('')
  const [state, setState] = useState<'idle' | 'loading' | 'copied' | 'copyFailed' | 'error'>('idle')

  const load = async () => {
    if (coreId === undefined || !tag.trim()) return
    setState('loading')
    try {
      const body = await fetcher<{ secret?: string }>(`/api/core/${coreId}/mtproto/${encodeURIComponent(tag.trim())}/registration-secret`)
      if (!body?.secret) throw new Error('empty')
      setSecret(body.secret)
      try {
        await navigator.clipboard.writeText(body.secret)
        setState('copied')
      } catch {
        setState('copyFailed')
      }
    } catch {
      setState('error')
    }
  }

  return (
    <div className="space-y-1.5">
      <div dir="ltr" className="flex items-center gap-2">
        <Input value={secret} dir="ltr" readOnly className="text-xs" placeholder="ee…" />
        <Button type="button" size="icon" variant="outline" className="h-9 w-9 shrink-0" onClick={load} disabled={coreId === undefined || state === 'loading'}>
          {state === 'copied' ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
        </Button>
      </div>
      <p className="text-muted-foreground text-[11px]">
        {state === 'copyFailed'
          ? t('coreEditor.mtproto.fields.registrationSecretCopyFailed', {
              defaultValue: 'The secret is in the box above, but the browser refused to copy it. Select it and copy by hand.',
            })
          : state === 'error'
            ? t('coreEditor.mtproto.fields.registrationSecretError', {
                defaultValue: 'Could not build a secret. Save the instance first, and make sure at least one active user has MTProto access.',
              })
            : t('coreEditor.mtproto.fields.registrationSecretHint', {
                defaultValue:
                  'Press copy to fetch a working client secret for this instance and put it on the clipboard. Send this exact value to @MTProxybot as the secret, together with your server address and the port above.',
              })}
      </p>
    </div>
  )
}

function issueFor(issues: MTProtoValidationIssue[], suffix: string): string | undefined {
  return issues.find(i => i.path.endsWith(`/${suffix}`))?.message
}

function randomPort(): number {
  return Math.floor(Math.random() * (65535 - 10000 + 1)) + 10000
}

export function MTProtoInstanceForm({ instance, issues, onChange, coreId }: MTProtoInstanceFormProps) {
  const { t } = useTranslation()
  const dir = useDirDetection()

  const set = <K extends keyof MTProtoInstanceDraft>(key: K, value: MTProtoInstanceDraft[K]) => {
    onChange(d => ({ ...d, [key]: value }))
  }

  const tagError = issueFor(issues, 'tag')
  const portError = issueFor(issues, 'port')
  const domainError = issueFor(issues, 'fakeTlsDomains')
  const domains = instance.fakeTlsDomains.split(/[\s,;]+/).filter(Boolean)
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
            <Label>
              {t('coreEditor.mtproto.fields.fakeTlsDomains', { defaultValue: 'Fake-TLS domains' })}
              {domains.length > 0 && <span className="text-muted-foreground ms-1 text-[11px]">({domains.length})</span>}
            </Label>
            <Textarea
              value={instance.fakeTlsDomains}
              dir="ltr"
              rows={6}
              className={cn('text-xs', domainError && 'border-destructive focus-visible:ring-destructive')}
              onChange={e => set('fakeTlsDomains', e.target.value)}
              placeholder={'example.org\nanother-site.com\nthird-one.net'}
            />
            {domainError && <p className="text-destructive text-[0.8rem] font-medium">{domainError}</p>}
            <p className="text-muted-foreground text-[11px]">
              {t('coreEditor.mtproto.fields.fakeTlsDomainsHint', {
                defaultValue:
                  'One domain per line. Any real, publicly reachable domain works - it need not be yours and needs no TLS certificate, because clients are authenticated by their secret alone. Listing many domains spreads users over different cover names, so one blocked name does not take everyone down: each user is assigned one automatically and keeps it.',
              })}
            </p>
          </div>
        )}

        {instance.mode === 'faketls' && (
          <div className="space-y-1.5 sm:col-span-2">
            <Label>{t('coreEditor.mtproto.fields.registrationSecret', { defaultValue: 'Client secret for @MTProxybot' })}</Label>
            <RegistrationSecret coreId={coreId} tag={instance.tag} />
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
