import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
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
              <Input
                type="text"
                inputMode="numeric"
                value={String(instance.port)}
                className="text-xs"
                isError={!!portError}
                onChange={e => set('port', e.target.value)}
                placeholder="443"
              />
            </div>
            <Button type="button" size="icon" variant="ghost" className="h-9 w-9 shrink-0" onClick={() => set('port', randomPort())} title={t('coreEditor.inbound.randomPort', { defaultValue: 'Generate random port' })}>
              <RefreshCcw className="h-3 w-3" />
            </Button>
          </div>
          {portError && <p className="text-destructive text-[0.8rem] font-medium">{portError}</p>}
        </div>

        <div className="space-y-1.5 sm:col-span-2">
          <Label>{t('coreEditor.mtproto.fields.fakeTlsDomain', { defaultValue: 'Fake-TLS domain' })}</Label>
          <Input
            value={instance.fakeTlsDomain}
            dir="ltr"
            className="text-xs"
            isError={!!domainError}
            onChange={e => set('fakeTlsDomain', e.target.value)}
            placeholder="www.example.com"
          />
          {domainError && <p className="text-destructive text-[0.8rem] font-medium">{domainError}</p>}
          <p className="text-muted-foreground text-[11px]">
            {t('coreEditor.mtproto.fields.fakeTlsDomainHint', {
              defaultValue:
                'Any real, publicly reachable domain works - it does not need to be owned by you or have any TLS certificate of its own. Every connecting client is validated against the secret alone; this domain is only used to disguise unauthenticated probes as ordinary HTTPS traffic to that site.',
            })}
          </p>
        </div>
      </div>
    </div>
  )
}
