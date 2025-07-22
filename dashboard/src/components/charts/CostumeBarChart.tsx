import { useEffect, useState } from 'react'
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from 'recharts'
import { DateRange } from 'react-day-picker'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { type ChartConfig, ChartContainer, ChartTooltip } from '@/components/ui/chart'
import { useTranslation } from 'react-i18next'
import useDirDetection from '@/hooks/use-dir-detection'
import { getUsage, Period, type NodeUsageStat } from '@/service/api'
import { formatBytes } from '@/utils/formatByte'
import { Skeleton } from '@/components/ui/skeleton'
import { TimeRangeSelector } from '@/components/common/TimeRangeSelector'
import { EmptyState } from './EmptyState'
import { TrendingUp } from 'lucide-react'
import { dateUtils } from '@/utils/dateFormatter'
import { TooltipProps } from 'recharts'

type DataPoint = {
  time: string
  usage: number
}

const chartConfig = {
  usage: {
    label: 'Traffic Usage (GB)',
    color: 'hsl(var(--primary))',
  },
} satisfies ChartConfig

// Define props interface
interface CostumeBarChartProps {
  nodeId?: number
}

// Helper function to determine period (copied from AreaCostumeChart)
const getPeriodFromDateRange = (range?: DateRange): Period => {
  if (!range?.from || !range?.to) {
    return Period.hour // Default to hour if no range
  }
  const diffTime = Math.abs(range.to.getTime() - range.from.getTime())
  const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24))

  if (diffDays <= 2) {
    // Up to 2 days, use hourly data
    return Period.hour
  }
  return Period.day // More than 2 days, use daily data
}

function CustomBarTooltip({ active, payload }: TooltipProps<any, any>) {
  const { t, i18n } = useTranslation()
  if (!active || !payload || !payload.length) return null
  const data = payload[0].payload
  const d = dateUtils.toDayjs(data._period_start)
  let formattedDate = d.format('YYYY-MM-DD HH:mm')
  // For fa locale, show only Persian time (Jalali) without (شمسی)
  if (i18n.language === 'fa' && typeof window !== 'undefined' && (window as any).Intl && (window as any).Intl.DateTimeFormat) {
    try {
      formattedDate = new Intl.DateTimeFormat('fa-IR-u-ca-persian', {
        year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
      }).format(new Date(data._period_start))
    } catch {}
  }
  return (
    <div
      className={`rounded border border-border bg-gradient-to-br from-background to-muted/80 shadow min-w-[160px] text-xs p-2 ${i18n.language === 'fa' ? 'text-right' : 'text-left'}`}
      dir={i18n.language === 'fa' ? 'rtl' : 'ltr'}
    >
      <div className={`mb-1 font-semibold text-xs ${i18n.language === 'fa' ? 'text-right' : 'text-center'}`}>
        {t('statistics.date', { defaultValue: 'Date' })}: {formattedDate}
      </div>
      <div className="flex flex-col gap-0.5 text-xs">
        <div>
          <span className="font-medium text-foreground">{t('statistics.totalUsage', { defaultValue: 'Total Usage' })}:</span>
          <span className={i18n.language === 'fa' ? 'mr-1' : 'ml-1'}>{data.usage} GB</span>
        </div>
        <div>
          <span className="font-medium text-foreground">{t('statistics.uplink', { defaultValue: 'Uplink' })}:</span>
          <span className={i18n.language === 'fa' ? 'mr-1' : 'ml-1'}>{formatBytes(data._uplink)}</span>
        </div>
        <div>
          <span className="font-medium text-foreground">{t('statistics.downlink', { defaultValue: 'Downlink' })}:</span>
          <span className={i18n.language === 'fa' ? 'mr-1' : 'ml-1'}>{formatBytes(data._downlink)}</span>
        </div>
      </div>
    </div>
  )
}

export function CostumeBarChart({ nodeId }: CostumeBarChartProps) {
  const [dateRange, setDateRange] = useState<DateRange | undefined>(undefined)
  const [chartData, setChartData] = useState<DataPoint[] | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const [totalUsage, setTotalUsage] = useState('0')

  const { t } = useTranslation()
  const dir = useDirDetection()

  useEffect(() => {
    const fetchUsageData = async () => {
      if (!dateRange?.from || !dateRange?.to) {
        setChartData(null)
        setTotalUsage('0')
        return
      }

      setIsLoading(true)
      setError(null)

      try {
        const startDate = dateRange.from
        const endDate = dateRange.to
        // Determine period based on range
        const period = getPeriodFromDateRange(dateRange)

        // Prepare API parameters
        const params: Parameters<typeof getUsage>[0] = {
          period: period,
          start: startDate.toISOString(),
          end: dateUtils.toDayjs(endDate).endOf('day').toISOString(),
          ...(nodeId !== undefined && { node_id: nodeId }),
        }

        const response = await getUsage(params)

        let statsArr: NodeUsageStat[] = []
        if (response && response.stats) {
          if (typeof response.stats === 'object' && !Array.isArray(response.stats)) {
            // If nodeId is provided, use that key
            const key = nodeId !== undefined ? String(nodeId) : '-1'
            if (response.stats[key] && Array.isArray(response.stats[key])) {
              statsArr = response.stats[key]
            } else {
              // fallback: use first available key
              const firstKey = Object.keys(response.stats)[0]
              if (firstKey && Array.isArray(response.stats[firstKey])) {
                statsArr = response.stats[firstKey]
              }
            }
          } else if (Array.isArray(response.stats)) {
            // fallback: old format
            statsArr = response.stats
          }
        }

        if (statsArr.length > 0) {
          const formattedData = statsArr.map((point: NodeUsageStat) => {
            const d = dateUtils.toDayjs(point.period_start)
            let timeFormat
            if (period === Period.hour) {
              timeFormat = d.format('HH:mm')
            } else {
              timeFormat = d.format('MM/DD')
            }
            const usageInGB = (point.uplink + point.downlink) / (1024 * 1024 * 1024)
            return {
              time: timeFormat,
              usage: parseFloat(usageInGB.toFixed(2)),
              _uplink: point.uplink,
              _downlink: point.downlink,
              _period_start: point.period_start,
            }
          })

          setChartData(formattedData)

          const total = statsArr.reduce((sum: number, point: NodeUsageStat) => sum + point.uplink + point.downlink, 0)
          const formattedTotal = formatBytes(total, 2)
          if (typeof formattedTotal === 'string') {
            setTotalUsage(formattedTotal)
          }
        } else {
          setChartData(null)
          setTotalUsage('0')
        }
      } catch (err) {
        setError(err as Error)
        setChartData(null)
        setTotalUsage('0')
        console.error('Error fetching usage data:', err)
      } finally {
        setIsLoading(false)
      }
    }

    fetchUsageData()
  }, [dateRange, nodeId])

  return (
    <Card>
      <CardHeader className="flex flex-col items-stretch space-y-0 border-b p-0 sm:flex-row">
        <div className="flex flex-1 flex-col sm:flex-row gap-1 px-6 py-6 sm:py-6 border-b">
          <div className="flex flex-1 flex-col justify-center align-middle gap-1 px-1 py-1">
            <CardTitle>{t('statistics.trafficUsage')}</CardTitle>
            <CardDescription>{t('statistics.trafficUsageDescription')}</CardDescription>
          </div>
          <div className="px-1 py-1 flex justify-center align-middle flex-col">
            <TimeRangeSelector onRangeChange={setDateRange} />
          </div>
        </div>
        <div className="sm:border-l p-6 m-0 flex flex-col justify-center px-4 ">
          <span className="text-muted-foreground text-xs sm:text-sm">{t('statistics.usageDuringPeriod')}</span>
          <span dir='ltr' className="text-foreground text-lg flex justify-center">{isLoading ? <Skeleton className="h-5 w-20" /> : totalUsage}</span>
        </div>
      </CardHeader>
      <CardContent dir={dir} className="pt-8">
        {isLoading ? (
          <div className="max-h-[400px] min-h-[200px] w-full flex items-center justify-center">
            <Skeleton className="h-[300px] w-full" />
          </div>
        ) : error ? (
          <EmptyState type="error" className="max-h-[400px] min-h-[200px]" />
        ) : !dateRange ? (
          <EmptyState 
            type="no-data" 
            title={t('statistics.selectTimeRange')}
            description={t('statistics.selectTimeRangeDescription')}
            icon={<TrendingUp className="h-12 w-12 text-muted-foreground/50" />}
            className="max-h-[400px] min-h-[200px]" 
          />
        ) : (
          <ChartContainer dir={'ltr'} config={chartConfig} className="max-h-[400px] min-h-[200px] w-full">
            {chartData && chartData.length > 0 ? (
              <BarChart accessibilityLayer data={chartData}>
                <CartesianGrid direction={'ltr'} vertical={false} />
                <XAxis direction={'ltr'} dataKey="time" tickLine={false} tickMargin={10} axisLine={false} />
                <YAxis direction={'ltr'} tickLine={false} axisLine={false} tickFormatter={value => `${value.toFixed(2)} GB`} />
                <ChartTooltip cursor={false} content={<CustomBarTooltip />} />
                <Bar dataKey="usage" fill="var(--color-usage)" radius={8} />
              </BarChart>
            ) : (
              <EmptyState 
                type="no-data" 
                title={t('statistics.noDataInRange')}
                description={t('statistics.noDataInRangeDescription')}
                className="max-h-[400px] min-h-[200px]" 
              />
            )}
          </ChartContainer>
        )}
      </CardContent>
    </Card>
  )
}
