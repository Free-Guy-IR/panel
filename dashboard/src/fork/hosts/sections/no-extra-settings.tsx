import { Info } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ExtraSectionProps } from '../types'

export function createNoExtraSection(messageKey: string, defaultValue: string) {
  return function NoExtraSection(_props: ExtraSectionProps) {
    const { t } = useTranslation()
    return (
      <div className="mb-6 flex items-start gap-2 rounded-sm border px-4 py-4">
        <Info className="text-muted-foreground mt-0.5 h-4 w-4 shrink-0" />
        <p className="text-muted-foreground text-sm">{t(messageKey, { defaultValue })}</p>
      </div>
    )
  }
}

export const MTProtoExtraSection = createNoExtraSection(
  'hostsDialog.mtproto.noExtraSettings',
  'MTProto hosts need no additional settings here — the fake-TLS domain and secrets are configured on the core instance and per user.',
)

export const L2TPExtraSection = createNoExtraSection(
  'hostsDialog.l2tp.noExtraSettings',
  'L2TP/IPsec hosts need no additional settings here — the pre-shared key and IPsec parameters are configured on the core instance, and credentials are per user.',
)
