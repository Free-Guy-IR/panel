import { useTranslation } from 'react-i18next'

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useUsagePeriodCapNotice } from '@/fork/charts/use-usage-period-cap-notice'
import useDirDetection from '@/hooks/use-dir-detection'
import { cn } from '@/lib/utils'
import type { Period } from '@/service/api'
import {
  CHART_PERIOD_OVERRIDE_AUTO,
  CHART_PERIOD_VALUES,
  type ChartPeriodOverride,
  getChartPeriodLabel,
} from '@/utils/chart-period-utils'

type PeriodSelectorProps = {
  value: ChartPeriodOverride
  onValueChange: (value: ChartPeriodOverride) => void
  className?: string
  cappedFrom?: Period
}

export default function PeriodSelector({ value, onValueChange, className, cappedFrom }: PeriodSelectorProps) {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const capNotice = useUsagePeriodCapNotice(cappedFrom)

  return (
    <Select value={value} onValueChange={next => onValueChange(next as ChartPeriodOverride)}>
      <SelectTrigger
        aria-label={t('statistics.period', { defaultValue: 'Period' })}
        title={capNotice}
        className={cn('h-9 w-[5.75rem] shrink-0 px-2 py-0 text-xs sm:w-[7rem] [&>span]:truncate', capNotice && 'border-amber-500 text-amber-600 dark:text-amber-400', className)}
        dir={dir}
      >
        <SelectValue>{getChartPeriodLabel(value, t)}</SelectValue>
      </SelectTrigger>
      <SelectContent dir={dir}>
        <SelectItem value={CHART_PERIOD_OVERRIDE_AUTO}>{getChartPeriodLabel(CHART_PERIOD_OVERRIDE_AUTO, t)}</SelectItem>
        {CHART_PERIOD_VALUES.map(period => (
          <SelectItem key={period} value={period}>
            {getChartPeriodLabel(period, t)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
