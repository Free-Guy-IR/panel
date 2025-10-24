import { useState, useMemo, useRef, useEffect, useCallback } from 'react'
import { Dialog, DialogContent } from '../ui/dialog'
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from '../ui/card'
import { ChartContainer, ChartTooltip, ChartConfig } from '../ui/chart'
import { PieChart, TrendingUp, Calendar, Info } from 'lucide-react'
import TimeSelector from '../charts/TimeSelector'
import { useTranslation } from 'react-i18next'
import { Period, useGetUserUsage, useGetNodes, useGetCurrentAdmin, NodeResponse } from '@/service/api'
import { DateRange } from 'react-day-picker'
import { TimeRangeSelector } from '@/components/common/TimeRangeSelector'
import { Button } from '../ui/button'
import { ResponsiveContainer } from 'recharts'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '../ui/select'
import { dateUtils } from '@/utils/dateFormatter'
import { TooltipProps } from 'recharts'
import { Bar, BarChart, CartesianGrid, XAxis, YAxis, Cell } from 'recharts'
import useDirDetection from '@/hooks/use-dir-detection'
import { useTheme } from '@/components/theme-provider'
import NodeStatsModal from './NodeStatsModal'

// Helper function to determine period (copied from AllNodesStackedBarChart and CostumeBarChart)
const getPeriodFromDateRange = (range?: DateRange): Period => {
  if (!range?.from || !range?.to) {
    return Period.hour // Default to hour if no range
  }
  const diffTime = Math.abs(range.to.getTime() - range.from.getTime())
  const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24))
  if (diffDays <= 2) {
    return Period.hour
  }
  return Period.day
}

// Define allowed period keys
const PERIOD_KEYS = ['1h', '12h', '24h', '3d', '1w'] as const
type PeriodKey = (typeof PERIOD_KEYS)[number]

const getPeriodMap = (now: number) => ({
  '1h': { period: Period.minute, start: new Date(now - 60 * 60 * 1000) },
  '12h': { period: Period.hour, start: new Date(now - 12 * 60 * 60 * 1000) },
  '24h': { period: Period.hour, start: new Date(now - 24 * 60 * 60 * 1000) },
  '3d': { period: Period.day, start: new Date(now - 3 * 24 * 60 * 60 * 1000) },
  '1w': { period: Period.day, start: new Date(now - 7 * 24 * 60 * 60 * 1000) },
})

interface UsageModalProps {
  open: boolean
  onClose: () => void
  username: string
}

// Move this hook to a separate file if reused elsewhere
const useWindowSize = () => {
  const [windowSize, setWindowSize] = useState({
    width: window.innerWidth,
    height: window.innerHeight,
  })

  useEffect(() => {
    const handleResize = () => {
      setWindowSize({
        width: window.innerWidth,
        height: window.innerHeight,
      })
    }

    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  return windowSize
}

function CustomBarTooltip({ active, payload, chartConfig, dir, period }: TooltipProps<any, any> & { chartConfig?: ChartConfig; dir: string; period?: string }) {
  const { t, i18n } = useTranslation()
  const [isMobile, setIsMobile] = useState(false)

  useEffect(() => {
    const checkMobile = () => {
      setIsMobile(window.innerWidth < 768) // md breakpoint
    }
    checkMobile()
    window.addEventListener('resize', checkMobile)
    return () => window.removeEventListener('resize', checkMobile)
  }, [])
  if (!active || !payload || !payload.length) return null

  const data = payload[0].payload
  const d = dateUtils.toDayjs(data._period_start)

  // Check if this is today's data
  const today = dateUtils.toDayjs(new Date())
  const isToday = d.isSame(today, 'day')

  let formattedDate
  if (i18n.language === 'fa') {
    // Use Persian (Jalali) calendar and Persian locale
    try {
      // If you have dayjs with jalali plugin, use it:
      // formattedDate = d.locale('fa').format('YYYY/MM/DD HH:mm')
      // Otherwise, fallback to toLocaleString
      if (period === 'day' && isToday) {
        formattedDate = new Date()
          .toLocaleString('fa-IR', {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            hour12: false,
          })
          .replace(',', '')
      } else if (period === 'day') {
        const localDate = new Date(d.year(), d.month(), d.date(), 0, 0, 0)
        formattedDate = localDate
          .toLocaleString('fa-IR', {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            hour12: false,
          })
          .replace(',', '')
      } else {
        // hourly or other: use actual time from data
        formattedDate = d
          .toDate()
          .toLocaleString('fa-IR', {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            hour12: false,
          })
          .replace(',', '')
      }
    } catch {
      formattedDate = d.format('YYYY/MM/DD HH:mm')
    }
  } else {
    if (period === 'day' && isToday) {
      const now = new Date()
      formattedDate = now
        .toLocaleString('en-US', {
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          hour12: false,
        })
        .replace(',', '')
    } else if (period === 'day') {
      const localDate = new Date(d.year(), d.month(), d.date(), 0, 0, 0)
      formattedDate = localDate
        .toLocaleString('en-US', {
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          hour12: false,
        })
        .replace(',', '')
    } else {
      // hourly or other: use actual time from data
      formattedDate = d
        .toDate()
        .toLocaleString('en-US', {
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          hour12: false,
        })
        .replace(',', '')
    }
  }

  // Get node color from chart config
  const getNodeColor = (nodeName: string) => {
    return chartConfig?.[nodeName]?.color || 'hsl(var(--chart-1))'
  }

  const isRTL = dir === 'rtl'

  // Get active nodes with usage > 0, sorted by usage descending
  const activeNodes = Object.keys(data)
    .filter(key => !key.startsWith('_') && key !== 'time' && key !== '_period_start' && key !== 'usage' && (data[key] || 0) > 0)
    .map(nodeName => ({
      name: nodeName,
      usage: data[nodeName] || 0
    }))
    .sort((a, b) => b.usage - a.usage)

  // Determine how many nodes to show based on screen size
  const maxNodesToShow = isMobile ? 3 : 6
  const nodesToShow = activeNodes.slice(0, maxNodesToShow)
  const hasMoreNodes = activeNodes.length > maxNodesToShow

  // For user usage data, we typically don't have node breakdowns
  // Check if this is aggregated user data (has usage field but no individual nodes)
  const isUserUsageData = (data.usage !== undefined && activeNodes.length === 0) || (activeNodes.length === 0 && Object.keys(data).includes('usage'))

  return (
    <div className={`min-w-[120px] max-w-[280px] sm:min-w-[140px] sm:max-w-[300px] rounded border border-border bg-background p-1.5 sm:p-2 text-[10px] sm:text-xs shadow ${isRTL ? 'text-right' : 'text-left'} ${isMobile ? 'max-h-[200px] overflow-y-auto' : ''}`} dir={isRTL ? 'rtl' : 'ltr'}>
      <div className={`mb-1 text-[10px] sm:text-xs font-semibold opacity-70 text-center`}>
        <span dir="ltr" className="inline-block truncate">
          {formattedDate}
        </span>
      </div>
      <div className={`mb-1.5 text-[10px] flex items-center justify-center gap-1.5 sm:text-xs text-muted-foreground text-center`}>
        <span>{t('statistics.totalUsage', { defaultValue: 'Total' })}: </span>
        <span dir="ltr" className="inline-block font-mono truncate">
          {isUserUsageData 
            ? data.usage.toFixed(2)
            : nodesToShow.reduce((sum, node) => sum + node.usage, 0).toFixed(2)
          } GB
        </span>
      </div>
      
      {isUserUsageData ? (
        // User usage data - show simple node label
        <div className={`flex flex-col gap-1`}>
          <div className={`flex items-center gap-1 text-[10px] text-muted-foreground ${isRTL ? 'flex-row-reverse' : 'flex-row'}`}>
            <div className="h-1.5 w-1.5 sm:h-2 sm:w-2 flex-shrink-0 rounded-full" style={{ backgroundColor: 'hsl(var(--primary))' }} />
            <span className="truncate max-w-[60px] sm:max-w-[80px] overflow-hidden text-ellipsis">
              {t('statistics.allNodes', { defaultValue: 'All Nodes' })}
            </span>
          </div>
        </div>
      ) : (
        // Node breakdown data
        <div className={`grid gap-1 sm:gap-1.5 ${nodesToShow.length > (isMobile ? 2 : 3) ? 'grid-cols-2' : 'grid-cols-1'}`}>
          {nodesToShow.map(node => (
            <div key={node.name} className={`flex flex-col gap-0.5 ${isRTL ? 'items-end' : 'items-start'}`}>
              <span className={`flex items-center gap-0.5 text-[10px] sm:text-xs font-semibold ${isRTL ? 'flex-row-reverse' : 'flex-row'}`}>
                <div className="h-1.5 w-1.5 sm:h-2 sm:w-2 flex-shrink-0 rounded-full" style={{ backgroundColor: getNodeColor(node.name) }} />
                <span className="truncate max-w-[60px] sm:max-w-[80px] overflow-hidden text-ellipsis" title={node.name}>{node.name}</span>
              </span>
              <span className={`flex items-center gap-0.5 text-[9px] sm:text-[10px] text-muted-foreground ${isRTL ? 'flex-row-reverse' : 'flex-row'}`}>
                <span className="font-mono">
                  {node.usage.toFixed(2)} GB
                </span>
              </span>
            </div>
          ))}
          {hasMoreNodes && (
            <div className={`flex items-center gap-0.5 text-[9px] sm:text-[10px] text-muted-foreground mt-1 ${isRTL ? 'flex-row-reverse justify-end' : 'flex-row justify-center'} col-span-full`}>
              <Info className="h-2.5 w-2.5 sm:h-3 sm:w-3 flex-shrink-0" />
              <span className="truncate max-w-[100px] overflow-hidden text-ellipsis">{t('statistics.clickForMore', { defaultValue: 'Click for more details' })}</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

const UsageModal = ({ open, onClose, username }: UsageModalProps) => {
  // Memoize now only once per modal open
  const nowRef = useRef<number>(Date.now())
  useEffect(() => {
    if (open) nowRef.current = Date.now()
  }, [open])

  const [period, setPeriod] = useState<PeriodKey>('1w')
  const [customRange, setCustomRange] = useState<DateRange | undefined>(undefined)
  const [showCustomRange, setShowCustomRange] = useState(false)
  const { t } = useTranslation()
  const { width } = useWindowSize()
  const [selectedNodeId, setSelectedNodeId] = useState<number | undefined>(undefined)
  const [modalOpen, setModalOpen] = useState(false)
  const [selectedData, setSelectedData] = useState<any>(null)
  const [currentDataIndex, setCurrentDataIndex] = useState(0)
  const [chartData, setChartData] = useState<any[] | null>(null)
  const [currentPeriod, setCurrentPeriod] = useState<Period>(Period.hour)

  // Get current admin to check permissions
  const { data: currentAdmin } = useGetCurrentAdmin()
  const is_sudo = currentAdmin?.is_sudo || false
  const dir = useDirDetection()
  const { resolvedTheme } = useTheme()

  // Reset node selection for non-sudo admins
  useEffect(() => {
    if (!is_sudo) {
      setSelectedNodeId(undefined) // Non-sudo admins see all nodes (master server data)
    }
  }, [is_sudo])

  // Fetch nodes list - only for sudo admins
  const { data: nodes, isLoading: isLoadingNodes } = useGetNodes(undefined, {
    query: {
      enabled: open && is_sudo, // Only fetch nodes for sudo admins when modal is open
    },
  })

  // Navigation handler for modal
  const handleModalNavigate = (index: number) => {
    if (chartData && chartData[index]) {
      setCurrentDataIndex(index)
      setSelectedData(chartData[index])
    }
  }

  // Build color palette for nodes
  const nodeList: NodeResponse[] = useMemo(() => (Array.isArray(nodes) ? nodes : []), [nodes])

  // Function to generate distinct colors based on theme
  const generateDistinctColor = useCallback((index: number, _totalNodes: number, isDark: boolean): string => {
    // Define a more distinct color palette with better contrast
    const distinctHues = [
      0, // Red
      30, // Orange
      60, // Yellow
      120, // Green
      180, // Cyan
      210, // Blue
      240, // Indigo
      270, // Purple
      300, // Magenta
      330, // Pink
      15, // Red-orange
      45, // Yellow-orange
      75, // Yellow-green
      150, // Green-cyan
      200, // Cyan-blue
      225, // Blue-indigo
      255, // Indigo-purple
      285, // Purple-magenta
      315, // Magenta-pink
      345, // Pink-red
    ]

    const hue = distinctHues[index % distinctHues.length]

    // Create more distinct saturation and lightness values
    const saturationVariations = [65, 75, 85, 70, 80, 60, 90, 55, 95, 50]
    const lightnessVariations = isDark ? [45, 55, 35, 50, 40, 60, 30, 65, 25, 70] : [40, 50, 30, 45, 35, 55, 25, 60, 20, 65]

    const saturation = saturationVariations[index % saturationVariations.length]
    const lightness = lightnessVariations[index % lightnessVariations.length]

    return `hsl(${hue}, ${saturation}%, ${lightness}%)`
  }, [])

  // Build chart config dynamically based on nodes
  const chartConfig = useMemo(() => {
    const config: ChartConfig = {}
    const isDark = resolvedTheme === 'dark'
    nodeList.forEach((node, idx) => {
      let color
      if (idx === 0) {
        // First node uses primary color like CostumeBarChart
        color = 'hsl(var(--primary))'
      } else if (idx < 5) {
        // Use palette colors for nodes 2-5: --chart-2, --chart-3, ...
        color = `hsl(var(--chart-${idx + 1}))`
      } else {
        // Generate distinct colors for nodes beyond palette
        color = generateDistinctColor(idx, nodeList.length, isDark)
      }
      config[node.name] = {
        label: node.name,
        color: color,
      }
    })
    return config
  }, [nodeList, resolvedTheme, generateDistinctColor])

  // Memoize periodMap only when modal opens
  const periodMap = useMemo(() => getPeriodMap(nowRef.current), [open])
  let backendPeriod: Period
  let start: Date
  let end: Date | undefined = undefined

  if (showCustomRange && customRange?.from && customRange?.to) {
    // Use the same period logic as other charts
    backendPeriod = getPeriodFromDateRange(customRange)
    start = customRange.from
    end = customRange.to
  } else {
    const map = periodMap[period]
    backendPeriod = map.period
    start = map.start
  }

  // Update current period for tooltip
  useEffect(() => {
    setCurrentPeriod(backendPeriod)
  }, [backendPeriod])

  const userUsageParams = useMemo(() => {
    if (showCustomRange && customRange?.from && customRange?.to) {
      return {
        period: backendPeriod,
        start: start.toISOString(),
        end: dateUtils.toDayjs(customRange.to).endOf('day').toISOString(),
        node_id: selectedNodeId,
      }
    }
    return { period: backendPeriod, start: start.toISOString(), node_id: selectedNodeId }
  }, [backendPeriod, start, end, period, customRange, showCustomRange, selectedNodeId])

  // Only fetch when modal is open
  const { data, isLoading } = useGetUserUsage(username, userUsageParams, { query: { enabled: open } })

  // Prepare chart data for BarChart with node grouping
  const processedChartData = useMemo(() => {
    if (!data?.stats) return []
    let flatStats: any[] = []
    if (data.stats) {
      if (typeof data.stats === 'object' && !Array.isArray(data.stats)) {
        // Dict format: use nodeId if provided, else '-1', else first key
        const key = selectedNodeId !== undefined ? String(selectedNodeId) : '-1'
        if (data.stats[key] && Array.isArray(data.stats[key])) {
          flatStats = data.stats[key]
        } else {
          const firstKey = Object.keys(data.stats)[0]
          if (firstKey && Array.isArray(data.stats[firstKey])) {
            flatStats = data.stats[firstKey]
          } else {
            flatStats = []
          }
        }
      } else if (Array.isArray(data.stats)) {
        // List format: use node_id === -1, then 0, else first
        let selectedStats = data.stats.find((s: any) => s.node_id === -1)
        if (!selectedStats) selectedStats = data.stats.find((s: any) => s.node_id === 0)
        if (!selectedStats) selectedStats = data.stats[0]
        flatStats = selectedStats?.stats || []
        if (!Array.isArray(flatStats)) flatStats = []
      }
    }
    let filtered = flatStats
    if ((period === '12h' || period === '24h') && !showCustomRange) {
      if (!start || !end)
        return flatStats.map((point: any) => {
          const dateObj = dateUtils.toDayjs(point.period_start)
          let timeFormat
          if (period === '12h' || period === '24h' || (showCustomRange && backendPeriod === Period.hour)) {
            timeFormat = dateObj.format('HH:mm')
          } else {
            timeFormat = dateObj.format('MM/DD')
          }
          const usageInGB = point.total_traffic / (1024 * 1024 * 1024)
          return {
            time: timeFormat,
            usage: parseFloat(usageInGB.toFixed(2)),
            _period_start: point.period_start,
            local_period_start: dateObj.toISOString(),
          }
        })
      const from = dateUtils.toDayjs((start as Date) || new Date(0))
      const to = dateUtils.toDayjs((end as Date) || new Date(0))
      filtered = filtered.filter((point: any) => {
        const pointTime = dateUtils.toDayjs(point.period_start)
        return (pointTime.isSame(from) || pointTime.isAfter(from)) && (pointTime.isSame(to) || pointTime.isBefore(to))
      })
    } else if (showCustomRange && customRange?.from && customRange?.to) {
      filtered = filtered.filter((point: any) => {
        if (!customRange.from || !customRange.to) return false
        const dateObj = dateUtils.toDayjs(point.period_start)
        return dateObj.isAfter(dateUtils.toDayjs(customRange.from).subtract(1, 'minute')) && dateObj.isBefore(dateUtils.toDayjs(customRange.to).add(1, 'minute'))
      })
    }
    return filtered.map((point: any) => {
      const dateObj = dateUtils.toDayjs(point.period_start)
      let timeFormat
      if (period === '12h' || period === '24h' || (showCustomRange && backendPeriod === Period.hour)) {
        timeFormat = dateObj.format('HH:mm')
      } else {
        timeFormat = dateObj.format('MM/DD')
      }
      const usageInGB = point.total_traffic / (1024 * 1024 * 1024)
      return {
        time: timeFormat,
        usage: parseFloat(usageInGB.toFixed(2)),
        _period_start: point.period_start,
        local_period_start: dateObj.toISOString(),
      }
    })
  }, [data, period, showCustomRange, customRange, backendPeriod, start, end, selectedNodeId])

  // Update chartData state when processedChartData changes
  useEffect(() => {
    setChartData(processedChartData)
  }, [processedChartData])

  // Calculate trend (simple: compare last and previous usage)
  const trend = useMemo(() => {
    if (!processedChartData || processedChartData.length < 2) return null
    const last = processedChartData[processedChartData.length - 1].usage
    const prev = processedChartData[processedChartData.length - 2].usage
    if (prev === 0) return null
    const percent = ((last - prev) / prev) * 100
    return percent
  }, [processedChartData])


  // Handlers
  const handleCustomRangeChange = useCallback((range: DateRange | undefined) => {
    setCustomRange(range)
    if (range?.from && range?.to) {
      setShowCustomRange(true)
      const diffHours = (range.to.getTime() - range.from.getTime()) / (1000 * 60 * 60)
      if (diffHours <= 1) setPeriod('1h')
      else if (diffHours <= 12) setPeriod('12h')
      else if (diffHours <= 24) setPeriod('24h')
      else if (diffHours <= 72) setPeriod('3d')
      else setPeriod('1w')
    }
  }, [])

  const handleTimeSelect = useCallback((newPeriod: PeriodKey) => {
    setPeriod(newPeriod)
    setShowCustomRange(false)
    setCustomRange(undefined)
  }, [])

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="max-w-2xl p-0.5">
        <Card className="w-full border-none shadow-none">
          <CardHeader className="pb-2">
            <CardTitle className="text-center text-lg sm:text-xl">{t('usersTable.usageChart', { defaultValue: 'Usage Chart' })}</CardTitle>
            <CardDescription className="flex flex-col items-center gap-4 pt-4">
              <div className="flex w-full items-center justify-center gap-2">
                <TimeSelector selectedTime={period} setSelectedTime={handleTimeSelect as any} />
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={t('usersTable.selectCustomRange', { defaultValue: 'Select custom range' })}
                  className={showCustomRange ? 'text-primary' : ''}
                  onClick={() => {
                    setShowCustomRange(!showCustomRange)
                    if (!showCustomRange) {
                      setCustomRange(undefined)
                    }
                  }}
                >
                  <Calendar className="h-4 w-4" />
                </Button>
              </div>
              {/* Node selector - only show for sudo admins */}
              {is_sudo && (
                <div className="flex w-full items-center justify-center gap-2">
                  <Select value={selectedNodeId?.toString() || 'all'} onValueChange={value => setSelectedNodeId(value === 'all' ? undefined : Number(value))} disabled={isLoadingNodes}>
                    <SelectTrigger className="w-full sm:w-[180px]">
                      <SelectValue placeholder={t('userDialog.selectNode', { defaultValue: 'Select Node' })} />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="all">{t('userDialog.allNodes', { defaultValue: 'All Nodes' })}</SelectItem>
                      {nodes?.map(node => (
                        <SelectItem key={node.id} value={node.id.toString()}>
                          {node.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}
              {showCustomRange && (
                <div className="flex w-full justify-center">
                  <TimeRangeSelector onRangeChange={handleCustomRangeChange} initialRange={customRange} className="w-full sm:w-auto" />
                </div>
              )}
            </CardDescription>
          </CardHeader>
          <CardContent dir="ltr" className="mb-0 p-0">
            <div className="w-full">
              {isLoading ? (
                <div className="flex h-60 w-full items-center justify-center">
                  <div className="h-40 w-full animate-pulse rounded-lg" />
                </div>
              ) : processedChartData.length === 0 ? (
                <div className="flex h-60 flex-col items-center justify-center gap-2 text-muted-foreground">
                  <PieChart className="h-12 w-12 opacity-30" />
                  <div className="text-lg font-medium">{t('usersTable.noUsageData', { defaultValue: 'No usage data available for this period.' })}</div>
                  <div className="text-sm">{t('usersTable.tryDifferentRange', { defaultValue: 'Try a different time range.' })}</div>
                </div>
              ) : (
                <ChartContainer config={chartConfig} dir={'ltr'}>
                  <ResponsiveContainer width="100%" height={width < 500 ? 200 : 320}>
                    <BarChart
                      data={processedChartData}
                      margin={{ top: 16, right: processedChartData.length > 7 ? 0 : 8, left: processedChartData.length > 7 ? 0 : 8, bottom: 8 }}
                      barSize={Math.max(16, Math.min(40, Math.floor(width / (processedChartData.length * 1.5))))}
                      onClick={(data) => {
                        if (data && data.activePayload && data.activePayload.length > 0 && processedChartData) {
                          const clickedData = data.activePayload[0].payload
                          const activeNodesCount = Object.keys(clickedData).filter(key =>
                            !key.startsWith('_') && key !== 'time' && key !== '_period_start' && (clickedData[key] || 0) > 0
                          ).length
                          // Open modal if there are more nodes than shown in tooltip
                          const maxShown = window.innerWidth < 768 ? 3 : 6
                          if (activeNodesCount > maxShown) {
                            // Find the index of the clicked data point
                            const clickedIndex = processedChartData.findIndex(item => item._period_start === clickedData._period_start)
                            setCurrentDataIndex(clickedIndex >= 0 ? clickedIndex : 0)
                            setSelectedData(clickedData)
                            setModalOpen(true)
                          }
                        }
                      }}
                    >
                      <CartesianGrid vertical={false} strokeDasharray="3 3" />
                      <XAxis dataKey="time" tickLine={false} tickMargin={10} axisLine={false} />
                      <YAxis tick={{ fontSize: 12 }} unit="GB" />
                      <ChartTooltip cursor={false} content={<CustomBarTooltip chartConfig={chartConfig} dir={dir} period={currentPeriod} />} />
                      <Bar dataKey="usage" radius={6} cursor="pointer">
                        {processedChartData.map((_: any, index: number) => (
                          <Cell key={`cell-${index}`} fill={'hsl(var(--primary))'} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </ChartContainer>
              )}
            </div>
          </CardContent>
          <CardFooter className="mt-0 flex-col items-start gap-2 text-xs sm:text-sm">
            {trend !== null && trend > 0 && (
              <div className="flex gap-2 font-medium leading-none text-green-600 dark:text-green-400">
                {t('usersTable.trendingUp', { defaultValue: 'Trending up by' })} {trend.toFixed(1)}% <TrendingUp className="h-4 w-4" />
              </div>
            )}
            {trend !== null && trend < 0 && (
              <div className="flex gap-2 font-medium leading-none text-red-600 dark:text-red-400">
                {t('usersTable.trendingDown', { defaultValue: 'Trending down by' })} {Math.abs(trend).toFixed(1)}%
              </div>
            )}
            <div className="leading-none text-muted-foreground">{t('usersTable.usageSummary', { defaultValue: 'Showing total usage for the selected period.' })}</div>
          </CardFooter>
        </Card>
      </DialogContent>

      {/* Node Stats Modal */}
      <NodeStatsModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        data={selectedData}
        chartConfig={chartConfig}
        period={currentPeriod}
        allChartData={processedChartData || []}
        currentIndex={currentDataIndex}
        onNavigate={handleModalNavigate}
      />
    </Dialog>
  )
}

export default UsageModal
