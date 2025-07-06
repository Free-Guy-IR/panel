import { Card, CardContent } from '@/components/ui/card'
import { SystemStats, NodeRealtimeStats } from '@/service/api'
import { useTranslation } from 'react-i18next'
import { Cpu, MemoryStick, Database, TrendingUp, TrendingDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import useDirDetection from '@/hooks/use-dir-detection'
import { formatBytes } from '@/utils/formatByte'
import { useEffect, useState } from 'react'

interface SystemStatisticsSectionProps {
  currentStats?: SystemStats | NodeRealtimeStats | null
}

const CountUp = ({ end, duration = 1500, suffix = '' }: { end: number; duration?: number; suffix?: string }) => {
  const [count, setCount] = useState(0)

  useEffect(() => {
    if (!end && end !== 0) return

    let startTimestamp: number | null = null
    const startValue = count
    const step = (timestamp: number) => {
      if (!startTimestamp) startTimestamp = timestamp
      const progress = Math.min((timestamp - startTimestamp) / duration, 1)
      // Using easeOutQuad for a softer animation
      const eased = progress < 0.5 ? 2 * progress * progress : 1 - Math.pow(-2 * progress + 2, 2) / 2
      const currentCount = Math.floor(eased * (end - startValue) + startValue)

      setCount(currentCount)

      if (progress < 1) {
        window.requestAnimationFrame(step)
      } else {
        setCount(end)
      }
    }

    window.requestAnimationFrame(step)
  }, [end, duration])

  return <>{count}{suffix}</>
}

export default function SystemStatisticsSection({ currentStats }: SystemStatisticsSectionProps) {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const [prevData, setPrevData] = useState<any>(null)
  const [isIncreased, setIsIncreased] = useState<Record<string, boolean>>({})

  const getTotalTrafficValue = () => {
    if (!currentStats) return 0
    
    if ('incoming_bandwidth' in currentStats && 'outgoing_bandwidth' in currentStats) {
      // Master server stats - use total traffic
      const stats = currentStats as SystemStats
      return Number(stats.incoming_bandwidth) + Number(stats.outgoing_bandwidth)
    } else if ('incoming_bandwidth_speed' in currentStats && 'outgoing_bandwidth_speed' in currentStats) {
      // Node stats - for now, use speed values as proxy
      // Note: In a real implementation, you might want to accumulate these values over time
      const stats = currentStats as NodeRealtimeStats
      return Number(stats.incoming_bandwidth_speed) + Number(stats.outgoing_bandwidth_speed)
    }
    
    return 0
  }

  const getMemoryUsage = () => {
    if (!currentStats) return { used: 0, total: 0, percentage: 0 }
    
    const memUsed = Number(currentStats.mem_used) || 0
    const memTotal = Number(currentStats.mem_total) || 0
    const percentage = memTotal > 0 ? (memUsed / memTotal) * 100 : 0
    
    return { used: memUsed, total: memTotal, percentage }
  }

  const getCpuInfo = () => {
    if (!currentStats) return { usage: 0, cores: 0 }
    
    let cpuUsage = Number(currentStats.cpu_usage) || 0
    const cpuCores = Number(currentStats.cpu_cores) || 0
    
    // Fix potential decimal issue - if usage is between 0-1, it's likely a decimal representation
    if (cpuUsage > 0 && cpuUsage <= 1) {
      cpuUsage = cpuUsage * 100
    }
    
    // Ensure CPU usage doesn't exceed 100% and is reasonable
    cpuUsage = Math.min(Math.max(cpuUsage, 0), 100)
    
    return { usage: Math.round(cpuUsage * 10) / 10, cores: cpuCores } // Round to 1 decimal place
  }

  const memory = getMemoryUsage()
  const cpu = getCpuInfo()

  useEffect(() => {
    if (prevData && currentStats) {
      setIsIncreased({
        cpu_usage: cpu.usage > prevData.cpu_usage,
        mem_usage: (currentStats.mem_used ?? 0) > prevData.mem_used,
        total_traffic: getTotalTrafficValue() > (prevData.total_traffic || 0),
      })
    }
    setPrevData({
      cpu_usage: cpu.usage,
      mem_used: currentStats?.mem_used ?? 0,
      total_traffic: getTotalTrafficValue(),
    })
  }, [currentStats])

  return (
    <div className={cn('flex flex-col items-center justify-between gap-x-4 gap-y-4 lg:flex-row', dir === 'rtl' && 'lg:flex-row-reverse')}>
      {/* CPU Usage */}
      <div className="w-full animate-fade-in" style={{ animationDuration: '600ms', animationDelay: '50ms' }}>
        <Card dir={dir} className="group relative w-full overflow-hidden rounded-lg border transition-all duration-300 hover:shadow-lg">
          <div
            className={cn(
              'absolute inset-0 bg-gradient-to-r from-primary/10 to-transparent opacity-0 transition-opacity duration-500',
              'dark:from-primary/5 dark:to-transparent',
              'group-hover:opacity-100',
            )}
          />
          <CardContent className="relative z-10 p-6">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-primary/10">
                  <Cpu className="h-5 w-5 text-primary" />
                </div>
                <div className="flex-1">
                  <p className="text-sm font-medium text-muted-foreground">{t('statistics.cpuUsage')}</p>
                  <div className="flex items-center justify-between gap-4">
                    <div className="flex items-center gap-2">
                      <span dir="ltr" className={cn('text-2xl font-bold transition-all duration-500', isIncreased.cpu_usage ? 'animate-zoom-out' : '')} style={{ animationDuration: '400ms' }}>
                        <CountUp end={cpu.usage} suffix="%" />
                      </span>
                      {isIncreased.cpu_usage !== undefined && (
                        <div className={cn('flex items-center text-xs', isIncreased.cpu_usage ? 'text-red-500' : 'text-green-500')}>
                          {isIncreased.cpu_usage ? <TrendingUp className="h-3 w-3" /> : <TrendingDown className="h-3 w-3" />}
                        </div>
                      )}
                    </div>
                    {cpu.cores > 0 && (
                      <div className="flex items-center gap-1 text-sm text-muted-foreground bg-muted/50 px-2 py-1 rounded-md">
                        <Cpu className="h-3 w-3" />
                        <span className="font-medium">
                          <CountUp end={cpu.cores} /> {t('statistics.cores')}
                        </span>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Memory Usage */}
      <div className="w-full animate-fade-in" style={{ animationDuration: '600ms', animationDelay: '150ms' }}>
        <Card dir={dir} className="group relative w-full overflow-hidden rounded-lg border transition-all duration-300 hover:shadow-lg">
          <div
            className={cn(
              'absolute inset-0 bg-gradient-to-r from-primary/10 to-transparent opacity-0 transition-opacity duration-500',
              'dark:from-primary/5 dark:to-transparent',
              'group-hover:opacity-100',
            )}
          />
          <CardContent className="relative z-10 p-6">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-primary/10">
                  <MemoryStick className="h-5 w-5 text-primary" />
                </div>
                <div>
                  <p className="text-sm font-medium text-muted-foreground">{t('statistics.ramUsage')}</p>
                  <div className="flex items-center gap-2">
                    <span dir="ltr" className={cn('text-2xl font-bold transition-all duration-500', isIncreased.mem_usage ? 'animate-zoom-out' : '')} style={{ animationDuration: '400ms' }}>
                      {currentStats ? (
                        <span>
                          <CountUp end={Number(formatBytes(memory.used, 1, false)) ?? 0} />/{formatBytes(memory.total, 1, true)}
                        </span>
                      ) : (
                        0
                      )}
                    </span>
                    {isIncreased.mem_usage !== undefined && (
                      <div className={cn('flex items-center text-xs', isIncreased.mem_usage ? 'text-red-500' : 'text-green-500')}>
                        {isIncreased.mem_usage ? <TrendingUp className="h-3 w-3" /> : <TrendingDown className="h-3 w-3" />}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Total Traffic */}
      <div className="w-full animate-fade-in" style={{ animationDuration: '600ms', animationDelay: '250ms' }}>
        <Card dir={dir} className="group relative w-full overflow-hidden rounded-lg border transition-all duration-300 hover:shadow-lg">
          <div
            className={cn(
              'absolute inset-0 bg-gradient-to-r from-primary/10 to-transparent opacity-0 transition-opacity duration-500',
              'dark:from-primary/5 dark:to-transparent',
              'group-hover:opacity-100',
            )}
          />
          <CardContent className="relative z-10 p-6">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-primary/10">
                  <Database className="h-5 w-5 text-primary" />
                </div>
                <div>
                  <p className="text-sm font-medium text-muted-foreground">{t('statistics.totalTraffic')}</p>
                  <div className="flex items-center gap-2">
                    <span dir="ltr" className={cn('text-2xl font-bold transition-all duration-500', isIncreased.total_traffic ? 'animate-zoom-out' : '')} style={{ animationDuration: '400ms' }}>
                      {formatBytes(getTotalTrafficValue() || 0, 1)}
                    </span>
                    {isIncreased.total_traffic !== undefined && (
                      <div className={cn('flex items-center text-xs', isIncreased.total_traffic ? 'text-green-500' : 'text-red-500')}>
                        {isIncreased.total_traffic ? <TrendingUp className="h-3 w-3" /> : <TrendingDown className="h-3 w-3" />}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
} 