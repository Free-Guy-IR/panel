import { useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { USAGE_MAX_BUCKETS } from '@/fork/charts/usage-bucket-cap'
import type { Period } from '@/service/api'
import { getChartPeriodLabel } from '@/utils/chart-period-utils'

const USAGE_PERIOD_CAP_TOAST_ID = 'usage-period-capped'

export const useUsagePeriodCapNotice = (cappedFrom?: Period) => {
  const { t, i18n } = useTranslation()
  const message = cappedFrom
    ? t('statistics.periodCapped', {
        period: getChartPeriodLabel(cappedFrom, t),
        max: USAGE_MAX_BUCKETS.toLocaleString(i18n.language),
        defaultValue: '{{period}} would give more than {{max}} points for this range, so the chart uses Auto.',
      })
    : undefined

  useEffect(() => {
    if (message) toast.warning(message, { id: USAGE_PERIOD_CAP_TOAST_ID })
  }, [message])

  return message
}
