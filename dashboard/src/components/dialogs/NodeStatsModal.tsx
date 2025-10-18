import { Dialog, DialogContent, DialogHeader, DialogTitle } from '../ui/dialog'
import { Card, CardContent } from '../ui/card'
import { Badge } from '../ui/badge'
import { useTranslation } from 'react-i18next'
import { formatBytes } from '@/utils/formatByte'
import { Upload, Download, Calendar, Activity } from 'lucide-react'
import { dateUtils } from '@/utils/dateFormatter'
import useDirDetection from '@/hooks/use-dir-detection'

interface NodeStatsModalProps {
    open: boolean
    onClose: () => void
    data: any
    chartConfig: any
    period: string
}

const NodeStatsModal = ({ open, onClose, data, chartConfig, period }: NodeStatsModalProps) => {
    const { t, i18n } = useTranslation()
    const dir = useDirDetection()

    if (!data) return null

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
                })
                .replace(',', '')
        }
    }

    const isRTL = dir === 'rtl'

    // Calculate total usage
    const totalUsage = Object.keys(data)
        .reduce((sum, key) => {
            if (key.startsWith('_uplink_') || key.startsWith('_downlink_') || key === 'time' || key === '_period_start') return sum
            return sum + (data[key] || 0)
        }, 0)

    // Get nodes with usage > 0
    const activeNodes = Object.keys(data)
        .filter(key => !key.startsWith('_') && key !== 'time' && key !== '_period_start' && (data[key] || 0) > 0)
        .map(nodeName => ({
            name: nodeName,
            usage: data[nodeName] || 0,
            uplink: data[`_uplink_${nodeName}`] || 0,
            downlink: data[`_downlink_${nodeName}`] || 0,
            color: chartConfig?.[nodeName]?.color || 'hsl(var(--chart-1))'
        }))
        .sort((a, b) => b.usage - a.usage) // Sort by usage descending

    return (
        <Dialog open={open} onOpenChange={onClose}>
            <DialogContent className="max-w-md sm:max-w-lg md:max-w-xl" dir={dir}>
                <DialogHeader>
                    <DialogTitle className={`flex items-center gap-2 text-sm sm:text-base`}>
                        <Activity className="h-4 w-4 sm:h-5 sm:w-5" />
                        {t('statistics.nodeStats', { defaultValue: 'Node Statistics' })}
                    </DialogTitle>
                </DialogHeader>

                <Card className="border-none shadow-none">
                    <CardContent className="space-y-4 p-0">
                        {/* Date and Total Usage */}
                        <div className={`flex items-center justify-between ${isRTL ? 'flex-row-reverse' : 'flex-row'}`}>
                            <div className={`flex items-center gap-2 ${isRTL ? 'flex-row-reverse' : 'flex-row'}`}>
                                <Calendar className="h-3 w-3 sm:h-4 sm:w-4 text-muted-foreground" />
                                <span className="text-xs sm:text-sm font-medium truncate max-w-[120px] sm:max-w-none" dir="ltr">
                                    {formattedDate}
                                </span>
                            </div>
                            <Badge variant="secondary" className="font-mono text-xs sm:text-sm px-2 py-1">
                                {totalUsage.toFixed(2)} GB
                            </Badge>
                        </div>

                        {/* Node Statistics */}
                        <div className="space-y-2 sm:space-y-3">
                            <h4 className={`text-xs sm:text-sm font-semibold text-foreground`}>
                                {t('statistics.nodeTrafficDistribution', { defaultValue: 'Node Traffic Distribution' })}
                            </h4>

                            <div dir='ltr' className="grid gap-2 sm:gap-3 max-h-80 overflow-y-auto">
                                {activeNodes.map((node) => (
                                    <div
                                        key={node.name}
                                        className={`flex items-center justify-between p-2 sm:p-3 rounded-lg border bg-card`}
                                    >
                                        <div className={`flex items-center gap-2 sm:gap-3 min-w-0 flex-1`}>
                                            <div
                                                className="h-2 w-2 sm:h-3 sm:w-3 rounded-full flex-shrink-0"
                                                style={{ backgroundColor: node.color }}
                                            />
                                            <div className="min-w-0 flex-1">
                                                <div className={`text-xs sm:text-sm font-medium truncate`}>
                                                    {node.name}
                                                </div>
                                                <div className={`text-xs text-muted-foreground`}>
                                                    {node.usage.toFixed(2)} GB
                                                </div>
                                            </div>
                                        </div>

                                        <div className={`flex items-center gap-1 sm:gap-2 text-xs`}>
                                            <div className={`flex items-center gap-1`}>
                                                <Upload className="h-2 w-2 sm:h-3 sm:w-3 text-green-500" />
                                                <span dir="ltr" className="font-mono text-xs truncate max-w-[60px] sm:max-w-none">
                                                    {formatBytes(node.uplink)}
                                                </span>
                                            </div>
                                            <div className={`flex items-center gap-1`}>
                                                <Download className="h-2 w-2 sm:h-3 sm:w-3 text-blue-500" />
                                                <span dir="ltr" className="font-mono text-xs truncate max-w-[60px] sm:max-w-none">
                                                    {formatBytes(node.downlink)}
                                                </span>
                                            </div>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>

                        {/* Summary */}
                        <div className={`text-xs text-muted-foreground leading-tight ${isRTL ? 'text-right' : 'text-left'}`}>
                            {t('statistics.nodeStatsDescription', {
                                defaultValue: 'Detailed traffic statistics for each node at this time period. Click on bars in the chart to view more details.'
                            })}
                        </div>
                    </CardContent>
                </Card>
            </DialogContent>
        </Dialog>
    )
}

export default NodeStatsModal
