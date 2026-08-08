import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import type { ConnectionStateResponse } from '@/service/api'
import dayjs from 'dayjs'
import { Users } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useDeviceCounts } from './device-counts-provider'

const toneFor = (verdict: string | undefined) => {
  switch (verdict) {
    case 'over_limit':
      return 'border-destructive/40 bg-destructive/10 text-destructive hover:bg-destructive/20'
    case 'at_limit':
      return 'border-amber-500/40 bg-amber-500/10 text-amber-600 hover:bg-amber-500/20 dark:text-amber-400'
    default:
      return 'border-border bg-muted/50 text-muted-foreground hover:bg-muted'
  }
}

/**
 * How many devices were last seen on one subscription.
 *
 * Nothing is shown for a user with no recent reading rather than a zero: the
 * job only looks at users who were recently online, so a zero would say
 * "nobody is connected" when it means "not measured".
 */
export default function DeviceCountBadge({ userId }: { userId: number }) {
  const { t } = useTranslation()
  const { byUser, deviceLimit, enabled } = useDeviceCounts()

  if (!enabled) return null

  const state: ConnectionStateResponse | undefined = byUser.get(userId)
  if (!state || !state.devices) return null

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          onClick={event => event.stopPropagation()}
          aria-label={t('connectionLimit.badge.aria', { count: state.devices, defaultValue: '{{count}} devices' })}
          className={cn(
            'inline-flex shrink-0 items-center gap-0.5 rounded-full border px-1.5 py-px text-[10px] font-medium transition-colors',
            toneFor(state.verdict),
          )}
        >
          <Users className="h-2.5 w-2.5" />
          {state.devices}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-72 p-3" onClick={event => event.stopPropagation()}>
        <div className="flex items-center justify-between gap-2">
          <span className="text-sm font-semibold">
            {t('connectionLimit.badge.title', { count: state.devices, defaultValue: '{{count}} device(s)' })}
          </span>
          {deviceLimit > 0 && (
            <span className="text-muted-foreground text-[10px]">
              {t('connectionLimit.badge.limit', { limit: deviceLimit, defaultValue: 'limit {{limit}}' })}
            </span>
          )}
        </div>

        <ul className="mt-2 space-y-1">
          {(state.reasons ?? []).map((reason, index) => (
            <li key={index} className="text-muted-foreground text-xs leading-relaxed" dir="auto">
              {reason}
            </li>
          ))}
        </ul>

        <div className="text-muted-foreground/70 mt-2 flex items-center justify-between gap-2 border-t pt-2 text-[10px]">
          <span>
            {t('connectionLimit.badge.streak', { count: state.streak, defaultValue: 'seen {{count}} cycle(s) running' })}
          </span>
          {state.checked_at && <span dir="ltr">{dayjs(state.checked_at).format('HH:mm')}</span>}
        </div>
      </PopoverContent>
    </Popover>
  )
}
