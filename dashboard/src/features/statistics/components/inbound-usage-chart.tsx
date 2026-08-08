import DenseChartAreaHint from '@/components/charts/dense-chart-area-hint'
import PeriodSelector from '@/components/charts/period-selector'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ChartConfig, ChartContainer, ChartTooltip } from '@/components/ui/chart'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { useChartViewType } from '@/hooks/use-chart-view-type'
import useDirDetection from '@/hooks/use-dir-detection'
import { InboundUsageStat, Period, useGetInboundsUsageStats } from '@/service/api'
import {
  CHART_PERIOD_OVERRIDE_AUTO,
  type ChartPeriodOverride,
  formatPeriodLabelForPeriod,
  formatTooltipDate,
  getChartQueryRangeFromShortcut,
  getChartXAxisInterval,
  resolvePeriodOverride,
} from '@/utils/chart-period-utils'
import { getChartRenderFlags } from '@/utils/chart-performance'
import { formatBytes } from '@/utils/formatByte'
import { SearchXIcon } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Area, AreaChart, Bar, BarChart, CartesianGrid, XAxis, YAxis } from 'recharts'

type PeriodOption = {
  label: string
  value: string
  period: Period
  hours?: number
  days?: number
  months?: number
  allTime?: boolean
}

// Same shortcuts the traffic usage chart offers, so switching between the two
// views does not change what "7d" means.
const PERIOD_KEYS = [
  { key: '1h', period: 'minute' as Period, amount: 1, unit: 'hour' },
  { key: '2h', period: 'hour' as Period, amount: 2, unit: 'hour' },
  { key: '4h', period: 'hour' as Period, amount: 4, unit: 'hour' },
  { key: '6h', period: 'hour' as Period, amount: 6, unit: 'hour' },
  { key: '12h', period: 'hour' as Period, amount: 12, unit: 'hour' },
  { key: '24h', period: 'hour' as Period, amount: 24, unit: 'hour' },
  { key: '2d', period: 'day' as Period, amount: 2, unit: 'day' },
  { key: '3d', period: 'day' as Period, amount: 3, unit: 'day' },
  { key: '5d', period: 'day' as Period, amount: 5, unit: 'day' },
  { key: '7d', period: 'day' as Period, amount: 7, unit: 'day' },
  { key: '14d', period: 'day' as Period, amount: 14, unit: 'day' },
  { key: '1m', period: 'day' as Period, amount: 1, unit: 'month' },
  { key: '3m', period: 'day' as Period, amount: 3, unit: 'month' },
  { key: 'all', period: 'day' as Period, allTime: true },
]

const ALL_INBOUNDS = '__all__'

const chartConfig = {
  traffic: { label: 'Traffic', color: 'hsl(var(--primary))' },
} satisfies ChartConfig

/**
 * Total traffic per inbound over a chosen window.
 *
 * Deliberately mirrors the traffic usage chart - same period shortcuts, same
 * auto/period override, same area/bar switch from theme settings - so the two
 * read as one feature rather than two similar-looking ones.
 */
export default function InboundUsageChart() {
  const { t, i18n } = useTranslation()
  const dir = useDirDetection()
  const chartViewType = useChartViewType()

  const PERIOD_OPTIONS: PeriodOption[] = useMemo(
    () => [
      ...PERIOD_KEYS.filter(opt => !opt.allTime).map(opt => ({
        label: typeof opt.amount === 'number' ? `${opt.amount} ${t(`time.${opt.unit}${opt.amount > 1 ? 's' : ''}`)}` : '',
        value: opt.key,
        period: opt.period,
        hours: opt.unit === 'hour' && typeof opt.amount === 'number' ? opt.amount : undefined,
        days: opt.unit === 'day' && typeof opt.amount === 'number' ? opt.amount : undefined,
        months: opt.unit === 'month' && typeof opt.amount === 'number' ? opt.amount : undefined,
      })),
      { label: t('alltime', { defaultValue: 'All Time' }), value: 'all', period: 'day' as Period, allTime: true },
    ],
    [t],
  )

  const [periodOption, setPeriodOption] = useState<PeriodOption>(() => PERIOD_OPTIONS.find(opt => opt.value === '7d') ?? PERIOD_OPTIONS[0])
  const [periodOverride, setPeriodOverride] = useState<ChartPeriodOverride>(CHART_PERIOD_OVERRIDE_AUTO)
  const [selectedInbound, setSelectedInbound] = useState<string>(ALL_INBOUNDS)

  useEffect(() => {
    setPeriodOption(prev => PERIOD_OPTIONS.find(opt => opt.value === prev.value) ?? PERIOD_OPTIONS.find(opt => opt.value === '7d') ?? PERIOD_OPTIONS[0])
  }, [PERIOD_OPTIONS])

  const queryRange = useMemo(
    () => getChartQueryRangeFromShortcut(periodOption.value, new Date(), { minuteForOneHour: true, periodOverride: resolvePeriodOverride(periodOverride) }),
    [periodOption.value, periodOverride],
  )
  const activePeriod = queryRange.period

  const { data, isLoading } = useGetInboundsUsageStats(
    { period: activePeriod, start: queryRange.startDate, end: queryRange.endDate },
    { query: { refetchInterval: 1000 * 60 * 5 } },
  )

  const statsByTag = useMemo(() => (data?.stats ?? {}) as Record<string, InboundUsageStat[]>, [data])
  const inboundTags = useMemo(() => Object.keys(statsByTag).sort((a, b) => a.localeCompare(b)), [statsByTag])

  // Keep the picker honest: if the selected inbound stops appearing in the
  // window the user just switched to, fall back to the combined view rather
  // than showing an empty chart for a tag that is no longer in the data.
  useEffect(() => {
    if (selectedInbound !== ALL_INBOUNDS && inboundTags.length > 0 && !inboundTags.includes(selectedInbound)) {
      setSelectedInbound(ALL_INBOUNDS)
    }
  }, [inboundTags, selectedInbound])

  const chartData = useMemo(() => {
    const buckets = new Map<string, number>()

    const consume = (rows: InboundUsageStat[]) => {
      for (const row of rows) {
        const key = String(row.period_start)
        buckets.set(key, (buckets.get(key) ?? 0) + Number(row.uplink ?? 0) + Number(row.downlink ?? 0))
      }
    }

    if (selectedInbound === ALL_INBOUNDS) {
      Object.values(statsByTag).forEach(consume)
    } else {
      consume(statsByTag[selectedInbound] ?? [])
    }

    return Array.from(buckets.entries())
      .sort((a, b) => new Date(a[0]).getTime() - new Date(b[0]).getTime())
      .map(([periodStart, traffic]) => ({
        date: formatPeriodLabelForPeriod(new Date(periodStart), activePeriod, i18n.language),
        rawDate: periodStart,
        traffic,
      }))
  }, [statsByTag, selectedInbound, activePeriod, i18n.language])

  // Per-inbound totals for the window, biggest first - the "which inbound is
  // actually carrying the traffic" question the chart alone does not answer.
  const totals = useMemo(
    () =>
      inboundTags
        .map(tag => ({
          tag,
          total: (statsByTag[tag] ?? []).reduce((sum, row) => sum + Number(row.uplink ?? 0) + Number(row.downlink ?? 0), 0),
        }))
        .filter(entry => entry.total > 0)
        .sort((a, b) => b.total - a.total),
    [inboundTags, statsByTag],
  )

  const grandTotal = useMemo(() => totals.reduce((sum, entry) => sum + entry.total, 0), [totals])

  const { isAnimationActive, areaCurveType } = useMemo(() => getChartRenderFlags(chartData.length), [chartData.length])
  const xAxisInterval = useMemo(() => getChartXAxisInterval(chartData.length), [chartData.length])

  return (
    <Card className="flex h-full flex-col justify-between overflow-hidden">
      <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <CardTitle>{t('statistics.inboundUsage', { defaultValue: 'Inbound Usage' })}</CardTitle>
          <CardDescription className="mt-1.5">{t('statistics.inboundUsageDescription', { defaultValue: 'Total traffic per inbound over time' })}</CardDescription>
        </div>
        <div className="flex w-full flex-col gap-2 sm:w-auto sm:shrink-0 sm:flex-row sm:items-center">
          <Select value={selectedInbound} onValueChange={setSelectedInbound}>
            <SelectTrigger className={`h-9 min-w-0 flex-1 px-2 py-0 text-xs sm:w-44 sm:flex-none ${dir === 'rtl' ? 'text-right' : ''}`} dir={dir}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent dir={dir}>
              <SelectItem value={ALL_INBOUNDS} className={dir === 'rtl' ? 'text-right' : ''}>
                {t('statistics.allInbounds', { defaultValue: 'All inbounds' })}
              </SelectItem>
              {inboundTags.map(tag => (
                <SelectItem key={tag} value={tag} className={dir === 'rtl' ? 'text-right' : ''}>
                  {tag}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <div className="flex items-center gap-2">
            <Select
              value={periodOption.value}
              onValueChange={val => {
                const found = PERIOD_OPTIONS.find(opt => opt.value === val)
                if (found) setPeriodOption(found)
              }}
            >
              <SelectTrigger className={`h-9 min-w-0 flex-1 px-2 py-0 text-xs sm:w-32 sm:flex-none ${dir === 'rtl' ? 'text-right' : ''}`} dir={dir}>
                <SelectValue>{periodOption.label}</SelectValue>
              </SelectTrigger>
              <SelectContent dir={dir}>
                {PERIOD_OPTIONS.map(opt => (
                  <SelectItem key={opt.value} value={opt.value} className={dir === 'rtl' ? 'text-right' : ''}>
                    {opt.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <PeriodSelector value={periodOverride} onValueChange={setPeriodOverride} />
          </div>
        </div>
      </CardHeader>

      <CardContent className="flex flex-1 flex-col justify-center overflow-hidden p-2 sm:p-6">
        {isLoading ? (
          <div className="mx-auto w-full max-w-7xl">
            <div className="flex h-48 items-end justify-center gap-2">
              {[1, 2, 3, 4, 5, 6, 7].map(i => (
                <Skeleton key={i} className={`w-8 rounded-t-lg ${i === 4 ? 'h-32' : i === 3 || i === 5 ? 'h-24' : i === 2 || i === 6 ? 'h-16' : 'h-20'}`} />
              ))}
            </div>
          </div>
        ) : chartData.length === 0 ? (
          <div className="text-muted-foreground mt-8 flex min-h-[200px] flex-col items-center justify-center gap-4 text-center">
            <SearchXIcon className="size-16" strokeWidth={1} />
            <div className="flex flex-col gap-1">
              <span>{t('statistics.noInboundData', { defaultValue: 'No inbound traffic recorded yet' })}</span>
              <span className="text-xs">{t('statistics.inboundDataHint', { defaultValue: 'Inbound history builds up from the moment it is enabled.' })}</span>
            </div>
          </div>
        ) : (
          <>
            <DenseChartAreaHint pointCount={chartData.length} />
            <ChartContainer config={chartConfig} dir="ltr" className="h-[240px] w-full overflow-x-auto sm:h-[320px]">
              {chartViewType === 'area' ? (
                <AreaChart data={chartData} margin={{ top: 16, right: 4, left: 4, bottom: 8 }}>
                  <defs>
                    <linearGradient id="inboundAreaGradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity={0.35} />
                      <stop offset="100%" stopColor="hsl(var(--primary))" stopOpacity={0.05} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid vertical={false} strokeDasharray="3 3" />
                  <XAxis dataKey="date" tickLine={false} axisLine={false} tickMargin={8} interval={xAxisInterval} tick={{ fontSize: 11 }} />
                  <YAxis tickLine={false} axisLine={false} width={64} tick={{ fontSize: 11 }} tickFormatter={value => formatBytes(Number(value))} />
                  <ChartTooltip
                    content={({ active, payload }) => {
                      if (!active || !payload?.length) return null
                      const point = payload[0].payload
                      return (
                        <div className="bg-background rounded-lg border p-2 shadow-sm">
                          <p className="text-muted-foreground text-xs">{formatTooltipDate(new Date(point.rawDate), activePeriod, i18n.language)}</p>
                          <p className="text-sm font-semibold">{formatBytes(point.traffic)}</p>
                        </div>
                      )
                    }}
                  />
                  <Area dataKey="traffic" type={areaCurveType} fill="url(#inboundAreaGradient)" stroke="hsl(var(--primary))" strokeWidth={2} isAnimationActive={isAnimationActive} />
                </AreaChart>
              ) : (
                <BarChart data={chartData} margin={{ top: 16, right: 4, left: 4, bottom: 8 }}>
                  <CartesianGrid vertical={false} strokeDasharray="3 3" />
                  <XAxis dataKey="date" tickLine={false} axisLine={false} tickMargin={8} interval={xAxisInterval} tick={{ fontSize: 11 }} />
                  <YAxis tickLine={false} axisLine={false} width={64} tick={{ fontSize: 11 }} tickFormatter={value => formatBytes(Number(value))} />
                  <ChartTooltip
                    content={({ active, payload }) => {
                      if (!active || !payload?.length) return null
                      const point = payload[0].payload
                      return (
                        <div className="bg-background rounded-lg border p-2 shadow-sm">
                          <p className="text-muted-foreground text-xs">{formatTooltipDate(new Date(point.rawDate), activePeriod, i18n.language)}</p>
                          <p className="text-sm font-semibold">{formatBytes(point.traffic)}</p>
                        </div>
                      )
                    }}
                  />
                  <Bar dataKey="traffic" fill="hsl(var(--primary))" radius={[6, 6, 0, 0]} isAnimationActive={isAnimationActive} />
                </BarChart>
              )}
            </ChartContainer>

            {totals.length > 0 && (
              <div className="mt-4 flex flex-col gap-2 border-t pt-4">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground text-xs font-medium sm:text-sm">{t('statistics.perInboundTotals', { defaultValue: 'Total per inbound' })}</span>
                  <span dir="ltr" className="text-xs font-semibold sm:text-sm">
                    {formatBytes(grandTotal)}
                  </span>
                </div>
                <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                  {totals.map(({ tag, total }) => (
                    <div key={tag} className="bg-muted/30 flex items-center justify-between gap-2 rounded-md px-2.5 py-1.5">
                      <span className="min-w-0 truncate text-xs">{tag}</span>
                      <span dir="ltr" className="shrink-0 text-xs font-semibold">
                        {formatBytes(total)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
