import { useEffect, useState, useMemo, useCallback, useRef } from 'react'
import type { ComponentProps } from 'react'
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Pie, PieChart, XAxis, YAxis, TooltipProps } from 'recharts'
import { DateRange } from 'react-day-picker'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { type ChartConfig, ChartContainer, ChartTooltip } from '@/components/ui/chart'
import { useTranslation } from 'react-i18next'
import useDirDetection from '@/hooks/use-dir-detection'
import { useChartViewType } from '@/hooks/use-chart-view-type'
import { Period, type InboundUsageStat, useGetInboundsUsageStats } from '@/service/api'
import { formatBytes, formatGigabytes } from '@/utils/formatByte'
import { Skeleton } from '@/components/ui/skeleton'
import ChartBrush from '@/components/charts/chart-brush'
import DenseChartAreaHint from '@/components/charts/dense-chart-area-hint'
import { EmptyState } from '@/components/charts/empty-state'
import { BarChart3, Calendar, Download, Info, PieChart as PieChartIcon, Upload } from 'lucide-react'
import { useTheme } from '@/app/providers/theme-provider'
import NodeStatsModal from '@/features/nodes/dialogs/node-stats-modal'
import TimeSelector, { TRAFFIC_TIME_SELECTOR_SHORTCUTS } from '@/components/charts/time-selector'
import PeriodSelector from '@/components/charts/period-selector'
import { TimeRangeSelector } from '@/components/common/time-range-selector'
import {
  CHART_PERIOD_OVERRIDE_AUTO,
  type ChartPeriodOverride,
  formatTooltipDate,
  getChartQueryRangeFromDateRange,
  getChartQueryRangeFromShortcut,
  formatPeriodLabelForPeriod,
  getChartXAxisInterval,
  resolvePeriodOverride,
  toStatsRecord,
  TrafficShortcutKey,
} from '@/utils/chart-period-utils'
import { getChartBrushWindow, getChartRenderFlags } from '@/utils/chart-performance'

type NodeChartDataPoint = {
  time: string
  _period_start: string
  [key: string]: string | number
}

type NodePieChartDataPoint = {
  name: string
  usage: number
  bytes: number
  percentage: number
  fill: string
}

const getTrafficBytes = (point: InboundUsageStat) => Number(point.uplink || 0) + Number(point.downlink || 0)

const getDirectionalTraffic = (point: InboundUsageStat) => ({
  uplink: Number(point.uplink || 0),
  downlink: Number(point.downlink || 0),
})

const STACKED_BAR_RADIUS = 4
type InboundSeries = { id: string; name: string }
type StackBarRadius = [number, number, number, number]
type CellRadiusProps = Partial<ComponentProps<typeof Cell>>
const SQUARE_STACK_RADIUS: StackBarRadius = [0, 0, 0, 0]

const getCellRadiusProps = (radius: StackBarRadius) => ({ radius }) as unknown as CellRadiusProps

const getStackedNodeRadius = (row: NodeChartDataPoint, nodeName: string, nodeList: InboundSeries[]): StackBarRadius => {
  const visibleNodes = nodeList.filter(node => Number(row[node.name] || 0) > 0)
  const visibleIndex = visibleNodes.findIndex(node => node.name === nodeName)

  if (visibleIndex < 0) return SQUARE_STACK_RADIUS
  if (visibleNodes.length === 1) return [STACKED_BAR_RADIUS, STACKED_BAR_RADIUS, STACKED_BAR_RADIUS, STACKED_BAR_RADIUS]

  const isBottomSegment = visibleIndex === 0
  const isTopSegment = visibleIndex === visibleNodes.length - 1

  return [isTopSegment ? STACKED_BAR_RADIUS : 0, isTopSegment ? STACKED_BAR_RADIUS : 0, isBottomSegment ? STACKED_BAR_RADIUS : 0, isBottomSegment ? STACKED_BAR_RADIUS : 0]
}

function CustomTooltip({ active, payload, chartConfig, dir, period }: TooltipProps<number, string> & { chartConfig?: ChartConfig; dir: string; period: Period }) {
  const { t, i18n } = useTranslation()
  const [isMobile, setIsMobile] = useState(false)

  useEffect(() => {
    const checkMobile = () => {
      setIsMobile(window.innerWidth < 768)
    }
    checkMobile()
    window.addEventListener('resize', checkMobile)
    return () => window.removeEventListener('resize', checkMobile)
  }, [])

  if (!active || !payload || !payload.length) return null

  const data = payload[0].payload as NodeChartDataPoint
  const formattedDate = data._period_start ? formatTooltipDate(data._period_start, period, i18n.language) : String(data.time || '')

  const getNodeColor = (nodeName: string) => chartConfig?.[nodeName]?.color || 'hsl(var(--chart-1))'
  const isRTL = dir === 'rtl'

  const activeNodes = Object.keys(data)
    .filter(key => !key.startsWith('_') && key !== 'time' && key !== '_period_start' && Number(data[key] || 0) > 0)
    .map(nodeName => ({
      name: nodeName,
      usage: Number(data[nodeName] || 0),
      uplink: Number(data[`_uplink_${nodeName}`] || 0),
      downlink: Number(data[`_downlink_${nodeName}`] || 0),
    }))
    .sort((a, b) => b.usage - a.usage)

  const hasDirectionalTraffic = activeNodes.some(node => (node.uplink || 0) > 0 || (node.downlink || 0) > 0)
  const maxNodesToShow = isMobile ? 3 : 6
  const nodesToShow = activeNodes.slice(0, maxNodesToShow)
  const hasMoreNodes = activeNodes.length > maxNodesToShow

  return (
    <div
      className={`border-border bg-background max-w-[280px] min-w-[120px] rounded border p-1.5 text-[10px] shadow sm:max-w-[300px] sm:min-w-[140px] sm:p-2 sm:text-xs ${isRTL ? 'text-right' : 'text-left'} ${isMobile ? 'max-h-[200px] overflow-y-auto' : ''}`}
      dir={isRTL ? 'rtl' : 'ltr'}
    >
      <div className="mb-1 text-center text-[10px] font-semibold opacity-70 sm:text-xs">
        <span dir="ltr" className="inline-block truncate">
          {formattedDate}
        </span>
      </div>
      <div className="text-muted-foreground mb-1.5 flex items-center justify-center gap-1.5 text-center text-[10px] sm:text-xs">
        <span>{t('statistics.totalUsage', { defaultValue: 'Total' })}: </span>
        <span dir="ltr" className="inline-block truncate font-mono">
          {formatGigabytes(activeNodes.reduce((sum, node) => sum + node.usage, 0))}
        </span>
      </div>
      <div className={`grid gap-1 sm:gap-1.5 ${nodesToShow.length > (isMobile ? 2 : 3) ? 'grid-cols-2' : 'grid-cols-1'}`}>
        {nodesToShow.map(node => (
          <div key={node.name} className={`flex flex-col gap-0.5 ${isRTL ? 'items-end' : 'items-start'}`}>
            <span className={`flex items-center gap-0.5 text-[10px] font-semibold sm:text-xs ${isRTL ? 'flex-row-reverse' : 'flex-row'}`}>
              <div className="h-1.5 w-1.5 flex-shrink-0 rounded-full sm:h-2 sm:w-2" style={{ backgroundColor: getNodeColor(node.name) }} />
              <span className="max-w-[60px] truncate overflow-hidden text-ellipsis sm:max-w-[80px]" title={node.name}>
                {node.name}
              </span>
            </span>
            <span dir="ltr" className="text-muted-foreground font-mono text-[9px] sm:text-[10px]">
              {formatGigabytes(node.usage)}
            </span>
            {hasDirectionalTraffic && (
              <span className={`text-muted-foreground flex items-center gap-0.5 text-[9px] sm:text-[10px] ${isRTL ? 'flex-row-reverse' : 'flex-row'}`}>
                <Upload className="h-2.5 w-2.5 flex-shrink-0 sm:h-3 sm:w-3" />
                <span dir="ltr" className="inline-block max-w-[40px] truncate overflow-hidden font-mono text-ellipsis sm:max-w-[50px]" title={String(formatBytes(node.uplink))}>
                  {formatBytes(node.uplink)}
                </span>
                <span className="mx-0.5 text-[8px] opacity-60 sm:text-[10px]">|</span>
                <Download className="h-2.5 w-2.5 flex-shrink-0 sm:h-3 sm:w-3" />
                <span dir="ltr" className="inline-block max-w-[40px] truncate overflow-hidden font-mono text-ellipsis sm:max-w-[50px]" title={String(formatBytes(node.downlink))}>
                  {formatBytes(node.downlink)}
                </span>
              </span>
            )}
          </div>
        ))}
        {hasMoreNodes && (
          <div className="text-muted-foreground col-span-full mt-1 flex w-full items-center justify-center gap-0.5 text-[9px] sm:text-[10px]">
            <Info className="h-2.5 w-2.5 flex-shrink-0 sm:h-3 sm:w-3" />
            <span className="text-center">{t('statistics.clickForMore', { defaultValue: 'Click for more details' })}</span>
          </div>
        )}
      </div>
    </div>
  )
}

function NodePieTooltip({ active, payload }: TooltipProps<number, string>) {
  const { t } = useTranslation()

  if (!active || !payload || !payload.length) return null

  const data = payload[0].payload as NodePieChartDataPoint

  return (
    <div className="border-border bg-background/95 rounded-lg border p-2 text-xs shadow-sm backdrop-blur-sm">
      <div className="mb-1 flex items-center gap-1.5">
        <div className="border-border/20 h-2.5 w-2.5 rounded-full border" style={{ backgroundColor: data.fill }} />
        <span className="text-foreground font-medium">{data.name}</span>
      </div>
      <div className="flex items-center justify-between gap-3">
        <span className="text-muted-foreground">{t('statistics.totalUsage', { defaultValue: 'Total Usage' })}</span>
        <span dir="ltr" className="text-foreground font-mono font-semibold">
          {formatBytes(data.bytes)}
        </span>
      </div>
      <div className="mt-1 flex items-center justify-between gap-3">
        <span className="text-muted-foreground">{t('statistics.percentage', { defaultValue: 'Percentage' })}</span>
        <span dir="ltr" className="text-foreground font-mono">{`${data.percentage.toFixed(1)}%`}</span>
      </div>
    </div>
  )
}

export default function InboundUsageChart() {
  const [chartView, setChartView] = useState<'bar' | 'pie'>('bar')
  const [selectedTime, setSelectedTime] = useState<TrafficShortcutKey>('1w')
  const [periodOverride, setPeriodOverride] = useState<ChartPeriodOverride>(CHART_PERIOD_OVERRIDE_AUTO)
  const [showCustomRange, setShowCustomRange] = useState(false)
  const [customRange, setCustomRange] = useState<DateRange | undefined>(undefined)
  const [windowWidth, setWindowWidth] = useState<number>(() => (typeof window !== 'undefined' ? window.innerWidth : 1024))
  const [modalOpen, setModalOpen] = useState(false)
  const [selectedData, setSelectedData] = useState<NodeChartDataPoint | null>(null)
  const [currentDataIndex, setCurrentDataIndex] = useState(0)
  const chartContainerRef = useRef<HTMLDivElement>(null)

  const { t, i18n } = useTranslation()
  const dir = useDirDetection()
  const chartViewType = useChartViewType()
  const { resolvedTheme } = useTheme()

  const generateDistinctColor = useCallback((index: number, isDark: boolean): string => {
    const distinctHues = [0, 30, 60, 120, 180, 210, 240, 270, 300, 330, 15, 45, 75, 150, 200, 225, 255, 285, 315, 345]
    const saturationVariations = [65, 75, 85, 70, 80, 60, 90, 55, 95, 50]
    const lightnessVariations = isDark ? [45, 55, 35, 50, 40, 60, 30, 65, 25, 70] : [40, 50, 30, 45, 35, 55, 25, 60, 20, 65]
    const hue = distinctHues[index % distinctHues.length]
    const saturation = saturationVariations[index % saturationVariations.length]
    const lightness = lightnessVariations[index % lightnessVariations.length]
    return `hsl(${hue}, ${saturation}%, ${lightness}%)`
  }, [])

  const activeQueryRange = useMemo(() => {
    const periodOptions = { minuteForOneHour: true, periodOverride: resolvePeriodOverride(periodOverride) }

    if (showCustomRange && customRange?.from && customRange?.to) {
      return getChartQueryRangeFromDateRange(customRange, selectedTime, periodOptions)
    }

    return getChartQueryRangeFromShortcut(selectedTime, new Date(), periodOptions)
  }, [showCustomRange, customRange, selectedTime, periodOverride])

  const activePeriod = activeQueryRange.period

  const usageParams = useMemo(
    () => ({
      period: activePeriod,
      start: activeQueryRange.startDate,
      end: activeQueryRange.endDate,
    }),
    [activePeriod, activeQueryRange.startDate, activeQueryRange.endDate],
  )

  const {
    data: usageData,
    isLoading,
    error,
  } = useGetInboundsUsageStats(usageParams, {
    query: {
      // The usage job writes every 30s into a 10-minute bucket, so the newest
      // bar keeps growing until the bucket closes. Refetching on the job's own
      // cadence keeps that bar live instead of frozen for minutes at a time.
      refetchInterval: 1000 * 30,
      refetchOnWindowFocus: true,
    },
  })

  const statsByNode = useMemo(() => toStatsRecord<InboundUsageStat>(usageData?.stats), [usageData?.stats])

  // Shape the inbound tags into the same {id, name} records the node list used,
  // so every series/colour/pie path below carries over unchanged.
  const nodeList = useMemo(() => Object.keys(statsByNode).sort((a, b) => a.localeCompare(b)).map(tag => ({ id: tag, name: tag })), [statsByNode])

  const chartConfig = useMemo(() => {
    const config: ChartConfig = {}
    const isDark = resolvedTheme === 'dark'
    nodeList.forEach((node, index) => {
      if (index === 0) {
        config[node.name] = { label: node.name, color: 'hsl(var(--primary))' }
        return
      }

      if (index < 5) {
        config[node.name] = { label: node.name, color: `hsl(var(--chart-${index + 1}))` }
        return
      }

      config[node.name] = { label: node.name, color: generateDistinctColor(index, isDark) }
    })
    return config
  }, [generateDistinctColor, nodeList, resolvedTheme])

  const inboundTotals = useMemo(
    () =>
      nodeList
        .map(({ name }) => {
          const rows = statsByNode[name] ?? []
          return {
            tag: name,
            total: rows.reduce((sum, row) => sum + getTrafficBytes(row), 0),
            uplink: rows.reduce((sum, row) => sum + Number(row.uplink || 0), 0),
            downlink: rows.reduce((sum, row) => sum + Number(row.downlink || 0), 0),
          }
        })
        .filter(entry => entry.total > 0)
        .sort((a, b) => b.total - a.total),
    [nodeList, statsByNode],
  )

  const inboundGrandTotal = useMemo(() => inboundTotals.reduce((sum, entry) => sum + entry.total, 0), [inboundTotals])

  const labelRangeHint = useMemo(
    () => ({
      shortcut: showCustomRange ? undefined : selectedTime,
      customRange: showCustomRange ? customRange : undefined,
    }),
    [customRange, selectedTime, showCustomRange],
  )

  const { chartData, totalUsage } = useMemo(() => {
    const statsKeys = Object.keys(statsByNode)
    if (statsKeys.length === 0) {
      return { chartData: [] as NodeChartDataPoint[], totalUsage: null }
    }

    const allPeriods = new Set<string>()
    Object.values(statsByNode).forEach(statsArray => {
      statsArray.forEach(stat => allPeriods.add(stat.period_start))
    })

    const sortedPeriods = Array.from(allPeriods).sort()
    const chartRows = sortedPeriods.map(periodStart => {
      const row: NodeChartDataPoint = {
        time: formatPeriodLabelForPeriod(periodStart, activePeriod, i18n.language, labelRangeHint),
        _period_start: periodStart,
      }

      nodeList.forEach(node => {
        const nodeStats = statsByNode[String(node.id)]?.find(stat => stat.period_start === periodStart)
        if (!nodeStats) {
          row[node.name] = 0
          row[`_uplink_${node.name}`] = 0
          row[`_downlink_${node.name}`] = 0
          return
        }

        const usageBytes = getTrafficBytes(nodeStats)
        const directionalTraffic = getDirectionalTraffic(nodeStats)
        row[node.name] = usageBytes / (1024 * 1024 * 1024)
        row[`_uplink_${node.name}`] = directionalTraffic.uplink
        row[`_downlink_${node.name}`] = directionalTraffic.downlink
      })

      return row
    })

    let totalBytes = 0
    Object.values(statsByNode).forEach(statsArray => {
      statsArray.forEach(stat => {
        totalBytes += getTrafficBytes(stat)
      })
    })

    return {
      chartData: chartRows,
      totalUsage: totalBytes > 0 ? String(formatBytes(totalBytes, 2)) : null,
    }
  }, [statsByNode, nodeList, activePeriod, i18n.language, labelRangeHint])

  const xAxisInterval = useMemo(
    () =>
      getChartXAxisInterval({
        dataLength: chartData.length,
        period: activePeriod,
        shortcut: selectedTime,
        windowWidth,
        customRange: showCustomRange ? customRange : undefined,
        periodOverride: resolvePeriodOverride(periodOverride),
      }),
    [showCustomRange, customRange, activePeriod, selectedTime, chartData.length, windowWidth, periodOverride],
  )

  const { isAnimationActive, usePerBarRadius, useAccessibilityLayer, areaCurveType } = useMemo(
    () => getChartRenderFlags(chartData.length, nodeList.length),
    [chartData.length, nodeList.length],
  )
  const brushWindow = useMemo(() => getChartBrushWindow(chartData.length, nodeList.length), [chartData.length, nodeList.length])

  const pieData = useMemo<NodePieChartDataPoint[]>(() => {
    if (chartData.length === 0 || nodeList.length === 0) return []

    const nodesWithUsage = nodeList
      .map((node, index) => {
        const usageInGb = chartData.reduce((sum, row) => sum + Number(row[node.name] || 0), 0)
        const bytes = usageInGb * 1024 * 1024 * 1024
        return {
          name: node.name,
          usage: usageInGb,
          bytes,
          fill: chartConfig[node.name]?.color || `hsl(var(--chart-${(index % 5) + 1}))`,
        }
      })
      .filter(node => node.bytes > 0)

    const totalBytes = nodesWithUsage.reduce((sum, node) => sum + node.bytes, 0)

    return nodesWithUsage
      .map(node => ({
        ...node,
        percentage: totalBytes > 0 ? (node.bytes * 100) / totalBytes : 0,
      }))
      .sort((a, b) => b.bytes - a.bytes)
  }, [chartData, nodeList, chartConfig])

  const pieChartConfig = useMemo<ChartConfig>(
    () =>
      pieData.reduce<ChartConfig>((config, point) => {
        config[point.name] = { label: point.name, color: point.fill }
        return config
      }, {}),
    [pieData],
  )

  const piePaddingAngle = pieData.length > 1 ? 1 : 0

  useEffect(() => {
    const handleResize = () => {
      setWindowWidth(window.innerWidth)
    }

    handleResize()
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  const handleTimeSelect = useCallback((value: string) => {
    setSelectedTime(value as TrafficShortcutKey)
    setShowCustomRange(false)
    setCustomRange(undefined)
  }, [])

  const handleCustomRangeChange = useCallback((range: DateRange | undefined) => {
    setCustomRange(range)
    if (range?.from && range?.to) {
      setShowCustomRange(true)
    }
  }, [])

  const handleModalNavigate = useCallback(
    (index: number) => {
      if (!chartData[index]) return
      setCurrentDataIndex(index)
      setSelectedData(chartData[index])
    },
    [chartData],
  )

  const handleChartPointClick = useCallback(
    (data: unknown) => {
      const chartClick = data as { activeTooltipIndex?: unknown; activePayload?: Array<{ payload?: unknown }> } | null
      const clickedIndex = typeof chartClick?.activeTooltipIndex === 'number' ? chartClick.activeTooltipIndex : -1
      const clickedData = (chartClick?.activePayload?.[0]?.payload ?? (clickedIndex >= 0 ? chartData[clickedIndex] : undefined)) as NodeChartDataPoint | undefined
      if (!clickedData) return

      const activeNodesCount = Object.keys(clickedData).filter(key => {
        if (key.startsWith('_') || key === 'time' || key === '_period_start') return false
        const usageValue = Number(clickedData[key] || 0)
        const uplinkValue = Number(clickedData[`_uplink_${key}`] || 0)
        const downlinkValue = Number(clickedData[`_downlink_${key}`] || 0)
        return usageValue > 0 || uplinkValue > 0 || downlinkValue > 0
      }).length

      if (activeNodesCount > 0) {
        const resolvedIndex = clickedIndex >= 0 ? clickedIndex : chartData.findIndex(item => item._period_start === clickedData._period_start)
        setCurrentDataIndex(resolvedIndex >= 0 ? resolvedIndex : 0)
        setSelectedData(clickedData)
        setModalOpen(true)
      }
    },
    [chartData],
  )

  return (
    <>
      <Card>
        <CardHeader className="flex flex-col items-stretch space-y-0 border-b p-0 xl:flex-row">
          <div className="flex flex-1 flex-col gap-2 border-b px-4 py-3 xl:px-6 xl:py-4">
            <div className="flex min-w-0 flex-col justify-center gap-1 pt-2">
              <CardTitle className="mb-0.5 flex min-w-0 items-center gap-2">
                <BarChart3 className="text-muted-foreground h-4 w-4 shrink-0" />
                <span className="truncate">{t('statistics.inboundUsage', { defaultValue: 'Inbound usage' })}</span>
              </CardTitle>
              <CardDescription className="text-pretty">{t('statistics.inboundUsageDescription', { defaultValue: 'Total traffic per inbound over time' })}</CardDescription>
            </div>
            <div className="flex w-full min-w-0 flex-col gap-2">
              <div className="flex w-full min-w-0 flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
                <TimeSelector selectedTime={selectedTime} setSelectedTime={handleTimeSelect} shortcuts={TRAFFIC_TIME_SELECTOR_SHORTCUTS} maxVisible={5} className="w-full sm:w-fit" />
                <div className="flex w-full items-center gap-2 sm:w-auto">
                  <PeriodSelector value={periodOverride} onValueChange={setPeriodOverride} className="min-w-0 flex-1 sm:w-[7rem] sm:flex-none" />
                  <button
                    type="button"
                    aria-label="Custom Range"
                    className={`inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md border ${showCustomRange ? 'bg-muted' : ''}`}
                    onClick={() => {
                      const next = !showCustomRange
                      setShowCustomRange(next)
                      if (!next) {
                        setCustomRange(undefined)
                      }
                    }}
                  >
                    <Calendar className="h-4 w-4" />
                  </button>
                </div>
              </div>
              <div className="flex w-full items-center gap-2">
                <div className="bg-muted/30 inline-flex h-8 shrink-0 items-center gap-1 rounded-md border p-1">
                  <button
                    type="button"
                    aria-label={chartViewType === 'area' ? t('theme.chartViewArea', { defaultValue: 'Area chart' }) : t('statistics.barChart', { defaultValue: 'Bar chart' })}
                    className={`inline-flex h-6 w-6 items-center justify-center rounded ${chartView === 'bar' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground'}`}
                    onClick={() => setChartView('bar')}
                  >
                    <BarChart3 className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    aria-label={t('statistics.pieChart', { defaultValue: 'Pie chart' })}
                    className={`inline-flex h-6 w-6 items-center justify-center rounded ${chartView === 'pie' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground'}`}
                    onClick={() => setChartView('pie')}
                  >
                    <PieChartIcon className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            </div>
            {showCustomRange && (
              <div className="flex w-full">
                <TimeRangeSelector onRangeChange={handleCustomRangeChange} initialRange={customRange} className="w-full" />
              </div>
            )}
          </div>
          <div className="m-0 flex flex-col justify-center p-4 xl:border-l xl:p-5 xl:px-6">
            <span className="text-muted-foreground text-xs sm:text-sm">{t('statistics.usageDuringPeriod')}</span>
            <span dir="ltr" className="text-foreground flex items-center justify-center gap-2 text-lg">
              <BarChart3 className="text-muted-foreground h-4 w-4" />
              {isLoading ? <Skeleton className="h-5 w-20" /> : totalUsage ? totalUsage : <span className="text-muted-foreground">—</span>}
            </span>
          </div>
        </CardHeader>
        <CardContent ref={chartContainerRef} dir={dir} className="pt-8">
          {isLoading ? (
            <div className="flex max-h-[400px] min-h-[200px] w-full items-center justify-center">
              <Skeleton className="h-[300px] w-full" />
            </div>
          ) : error ? (
            <EmptyState type="error" className="max-h-[400px] min-h-[200px]" />
          ) : nodeList.length === 0 ? (
            <EmptyState type="no-nodes" className="max-h-[400px] min-h-[200px]" />
          ) : (
            <div className="mx-auto w-full">
              {chartView === 'bar' && <DenseChartAreaHint pointCount={chartData.length} />}
              <ChartContainer dir="ltr" config={chartView === 'pie' ? pieChartConfig : chartConfig} className="h-[200px] w-full sm:h-[320px] lg:h-[400px]">
                {chartData.length > 0 && chartView === 'bar' ? (
                  chartViewType === 'area' ? (
                    <AreaChart {...(useAccessibilityLayer ? { accessibilityLayer: true } : {})} data={chartData} margin={{ top: 5, right: 10, left: 10, bottom: 5 }} onClick={handleChartPointClick}>
                      <defs>
                        {nodeList.map((node, index) => {
                          const color = chartConfig[node.name]?.color || `hsl(var(--chart-${(index % 5) + 1}))`
                          return (
                            <linearGradient key={node.id} id={`node-area-gradient-${node.id}`} x1="0" y1="0" x2="0" y2="1">
                              <stop offset="0%" stopColor={color} stopOpacity={0.45} />
                              <stop offset="100%" stopColor={color} stopOpacity={0.05} />
                            </linearGradient>
                          )
                        })}
                      </defs>
                      <CartesianGrid direction="ltr" vertical={false} />
                      <XAxis direction="ltr" dataKey="time" tickLine={false} tickMargin={10} axisLine={false} minTickGap={28} interval={xAxisInterval} />
                      <YAxis
                        direction="ltr"
                        tickLine={false}
                        axisLine={false}
                        domain={[0, 'auto']}
                        tickFormatter={value => formatGigabytes(Number(value || 0))}
                        tick={{
                          fill: 'hsl(var(--muted-foreground))',
                          fontSize: 9,
                          fontWeight: 500,
                        }}
                        width={32}
                        tickMargin={2}
                      />
                      <ChartTooltip cursor={false} content={props => <CustomTooltip {...(props as TooltipProps<number, string>)} chartConfig={chartConfig} dir={dir} period={activePeriod} />} />
                      {nodeList.map((node, index) => (
                        <Area
                          key={node.id}
                          type={areaCurveType}
                          dataKey={node.name}
                          stackId="a"
                          fill={`url(#node-area-gradient-${node.id})`}
                          stroke={chartConfig[node.name]?.color || `hsl(var(--chart-${(index % 5) + 1}))`}
                          strokeWidth={1.5}
                          dot={false}
                          activeDot={false}
                          isAnimationActive={isAnimationActive}
                          cursor="pointer"
                        />
                      ))}
                      {brushWindow && (
                        <ChartBrush startIndex={brushWindow.startIndex} endIndex={brushWindow.endIndex} />
                      )}
                    </AreaChart>
                  ) : (
                    <BarChart {...(useAccessibilityLayer ? { accessibilityLayer: true } : {})} data={chartData} margin={{ top: 5, right: 10, left: 10, bottom: 5 }} onClick={handleChartPointClick}>
                      <CartesianGrid direction="ltr" vertical={false} />
                      <XAxis direction="ltr" dataKey="time" tickLine={false} tickMargin={10} axisLine={false} minTickGap={28} interval={xAxisInterval} />
                      <YAxis
                        direction="ltr"
                        tickLine={false}
                        axisLine={false}
                        tickFormatter={value => formatGigabytes(Number(value || 0))}
                        tick={{
                          fill: 'hsl(var(--muted-foreground))',
                          fontSize: 9,
                          fontWeight: 500,
                        }}
                        width={32}
                        tickMargin={2}
                      />
                      <ChartTooltip cursor={false} content={props => <CustomTooltip {...(props as TooltipProps<number, string>)} chartConfig={chartConfig} dir={dir} period={activePeriod} />} />
                      {nodeList.map((node, index) => (
                        <Bar
                          key={node.id}
                          dataKey={node.name}
                          stackId="a"
                          fill={chartConfig[node.name]?.color || `hsl(var(--chart-${(index % 5) + 1}))`}
                          radius={SQUARE_STACK_RADIUS}
                          cursor="pointer"
                          isAnimationActive={isAnimationActive}
                        >
                          {usePerBarRadius &&
                            chartData.map(row => (
                              <Cell key={`${node.id}-${row._period_start}`} {...getCellRadiusProps(getStackedNodeRadius(row, node.name, nodeList))} />
                            ))}
                        </Bar>
                      ))}
                      {brushWindow && (
                        <ChartBrush startIndex={brushWindow.startIndex} endIndex={brushWindow.endIndex} />
                      )}
                    </BarChart>
                  )
                ) : chartData.length > 0 && chartView === 'pie' ? (
                  pieData.length > 0 ? (
                    <PieChart>
                      <ChartTooltip cursor={false} content={props => <NodePieTooltip {...(props as TooltipProps<number, string>)} />} />
                      <Pie data={pieData} dataKey="bytes" nameKey="name" innerRadius="45%" outerRadius="88%" paddingAngle={piePaddingAngle} strokeWidth={1.5}>
                        {pieData.map(point => (
                          <Cell key={point.name} fill={point.fill} />
                        ))}
                      </Pie>
                    </PieChart>
                  ) : (
                    <EmptyState type="no-data" title={t('statistics.noDataInRange')} description={t('statistics.noDataInRangeDescription')} className="max-h-[400px] min-h-[200px]" />
                  )
                ) : (
                  <EmptyState type="no-data" title={t('statistics.noDataInRange')} description={t('statistics.noDataInRangeDescription')} className="max-h-[400px] min-h-[200px]" />
                )}
              </ChartContainer>
              {chartData.length > 0 && (
                <div className="overflow-x-auto pt-3">
                  <div className="flex min-w-max items-center justify-center gap-4">
                    {(chartView === 'pie' ? pieData : nodeList).map(item => {
                      const nodeName = typeof item === 'object' && 'name' in item ? item.name : ''
                      const itemConfig = chartConfig[nodeName]
                      const percentage = typeof item === 'object' && 'percentage' in item ? item.percentage : undefined
                      return (
                        <div dir="ltr" key={nodeName} className="[&>svg]:text-muted-foreground flex items-center gap-1.5 [&>svg]:h-3 [&>svg]:w-3">
                          <div
                            className="h-2 w-2 shrink-0 rounded-[2px]"
                            style={{
                              backgroundColor: itemConfig?.color || (typeof item === 'object' && 'fill' in item ? item.fill : 'hsl(var(--chart-1))'),
                            }}
                          />
                          <span className="text-xs whitespace-nowrap">
                            {nodeName}
                            {typeof percentage === 'number' ? ` (${percentage.toFixed(1)}%)` : ''}
                          </span>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}
              {inboundTotals.length > 0 && (
                <div className="mt-4 flex flex-col gap-3 border-t pt-4">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-muted-foreground text-xs font-medium sm:text-sm">{t('statistics.perInboundTotals', { defaultValue: 'Total per inbound' })}</span>
                    <span dir="ltr" className="text-xs font-semibold sm:text-sm">
                      {formatBytes(inboundGrandTotal)}
                    </span>
                  </div>
                  <div className="grid w-full grid-cols-1 gap-3 sm:grid-cols-2 sm:gap-4">
                    {inboundTotals.map(({ tag, total, uplink, downlink }) => (
                      <Card key={tag} dir={dir} className="group relative h-full w-full overflow-hidden rounded-lg border transition-all duration-300 hover:shadow-lg">
                        <div className="from-primary/10 dark:from-primary/5 absolute inset-0 bg-gradient-to-r to-transparent opacity-0 transition-opacity duration-500 group-hover:opacity-100 dark:to-transparent" />
                        <CardContent className="relative z-10 flex h-full flex-col justify-between p-4 sm:p-5">
                          <div className="mb-3 flex items-center justify-between gap-2">
                            <span className="flex min-w-0 items-center gap-2">
                              <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: chartConfig[tag]?.color || 'hsl(var(--chart-1))' }} />
                              <p className="min-w-0 truncate text-sm font-semibold sm:text-base">{tag}</p>
                            </span>
                            <span dir="ltr" className="shrink-0 text-sm font-bold whitespace-nowrap sm:text-base">
                              {formatBytes(total)}
                            </span>
                          </div>

                          <div className="flex flex-col gap-2 sm:gap-3">
                            <div className="flex items-center justify-between gap-2">
                              <span className="flex min-w-0 items-center gap-2">
                                <span className="bg-primary/10 rounded-md p-1">
                                  <Upload className="text-primary h-3.5 w-3.5" />
                                </span>
                                <span className="text-muted-foreground truncate text-xs font-medium">{t('statistics.uplink')}</span>
                              </span>
                              <span dir="ltr" className="shrink-0 text-xs font-semibold whitespace-nowrap sm:text-sm">
                                {formatBytes(uplink)}
                              </span>
                            </div>

                            <div className="flex items-center justify-between gap-2">
                              <span className="flex min-w-0 items-center gap-2">
                                <span className="bg-primary/10 rounded-md p-1">
                                  <Download className="text-primary h-3.5 w-3.5" />
                                </span>
                                <span className="text-muted-foreground truncate text-xs font-medium">{t('statistics.downlink')}</span>
                              </span>
                              <span dir="ltr" className="shrink-0 text-xs font-semibold whitespace-nowrap sm:text-sm">
                                {formatBytes(downlink)}
                              </span>
                            </div>
                          </div>
                        </CardContent>
                      </Card>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <NodeStatsModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        data={selectedData}
        chartConfig={chartConfig}
        period={activePeriod}
        allChartData={chartData}
        currentIndex={currentDataIndex}
        onNavigate={handleModalNavigate}
        hideUplinkDownlink={false}
      />
    </>
  )
}
