import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import useDirDetection from '@/hooks/use-dir-detection'
import { cn } from '@/lib/utils'
import { getRealtimeNodeStatsQueryOptions, NodeSimple, NodeStatus } from '@/service/api'
import { formatMbpsPair } from '@/utils/formatSpeed'
import { useQueries } from '@tanstack/react-query'
import { Download, Upload } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface AllNodesLiveSectionProps {
  nodes: NodeSimple[]
}

const REFETCH_INTERVAL_MS = 2000

function getNodeStatusDotColor(status: NodeStatus) {
  switch (status) {
    case 'connected':
      return 'bg-green-500'
    case 'connecting':
      return 'bg-amber-500'
    case 'error':
      return 'bg-destructive'
    case 'limited':
      return 'bg-orange-500'
    default:
      return 'bg-gray-400 dark:bg-gray-600'
  }
}

/**
 * Live uplink/downlink for every node at once, plus the combined totals.
 *
 * The per-node statistics endpoint only answers for one node at a time, so
 * this issues one query per node through useQueries rather than a loop of
 * useQuery calls - the node list changes length between renders, and hooks
 * cannot be called conditionally.
 *
 * Only connected nodes are polled. A disconnected node has no live figures to
 * report, and asking anyway would fail on every interval for as long as the
 * page stays open.
 */
export default function AllNodesLiveSection({ nodes }: AllNodesLiveSectionProps) {
  const { t } = useTranslation()
  const dir = useDirDetection()

  const pollableNodes = nodes.filter(node => node.status === 'connected')

  const results = useQueries({
    queries: pollableNodes.map(node => ({
      ...getRealtimeNodeStatsQueryOptions(node.id, {
        query: {
          refetchInterval: REFETCH_INTERVAL_MS,
          staleTime: 1000,
          refetchOnWindowFocus: true,
          retry: false,
        },
      }),
    })),
  })

  const perNode = pollableNodes.map((node, index) => {
    const result = results[index]
    return {
      node,
      isLoading: result?.isLoading ?? true,
      hasError: Boolean(result?.error),
      incoming: Number(result?.data?.incoming_bandwidth_speed ?? 0),
      outgoing: Number(result?.data?.outgoing_bandwidth_speed ?? 0),
    }
  })

  // Totals only count nodes that actually reported, so a failing node reads as
  // missing rather than silently dragging the total down to look like a drop.
  const reporting = perNode.filter(entry => !entry.isLoading && !entry.hasError)
  const totalIncoming = reporting.reduce((sum, entry) => sum + entry.incoming, 0)
  const totalOutgoing = reporting.reduce((sum, entry) => sum + entry.outgoing, 0)

  const totalIncomingSpeed = formatMbpsPair(totalIncoming)
  const totalOutgoingSpeed = formatMbpsPair(totalOutgoing)

  const totalCards = [
    { key: 'up', label: t('statistics.uplink'), icon: Upload, speed: totalOutgoingSpeed },
    { key: 'down', label: t('statistics.downlink'), icon: Download, speed: totalIncomingSpeed },
  ]

  return (
    <div className="flex w-full flex-col gap-3 sm:gap-4">
      {/* Combined totals across every reporting node */}
      <div className="grid w-full grid-cols-1 gap-3 sm:grid-cols-2 sm:gap-4">
        {totalCards.map(({ key, label, icon: Icon, speed }, index) => (
          <div key={key} className="animate-fade-in h-full w-full" style={{ animationDuration: '600ms', animationDelay: `${150 + index * 100}ms` }}>
            <Card dir={dir} className="group relative h-full w-full overflow-hidden rounded-lg border transition-all duration-300 hover:shadow-lg">
              <div
                className={cn(
                  'from-primary/10 absolute inset-0 bg-gradient-to-r to-transparent opacity-0 transition-opacity duration-500',
                  'dark:from-primary/5 dark:to-transparent',
                  'group-hover:opacity-100',
                )}
              />
              <CardContent className="relative z-10 flex h-full flex-col justify-between p-4 sm:p-5 lg:p-6">
                <div className="mb-2 flex items-start justify-between sm:mb-3">
                  <div className="flex items-center gap-2 sm:gap-3">
                    <div className="bg-primary/10 rounded-lg p-1.5 sm:p-2">
                      <Icon className="text-primary h-4 w-4 sm:h-5 sm:w-5" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="text-muted-foreground text-xs leading-tight font-medium sm:truncate sm:text-sm">
                        {t('statistics.totalAcrossNodes', { defaultValue: 'Total' })} · {label}
                      </p>
                    </div>
                  </div>
                </div>

                <div className="flex items-end justify-between gap-2">
                  <span dir="ltr" className="truncate text-lg font-bold transition-all duration-300 sm:text-xl lg:text-2xl">
                    <span className="whitespace-nowrap">{speed.mbPerSecText} MB/s</span>
                  </span>
                  <span dir="ltr" className="bg-muted/60 text-muted-foreground rounded-md px-1.5 py-1 text-xs font-medium whitespace-nowrap sm:px-2">
                    {speed.mbpsText} Mb/s
                  </span>
                </div>
              </CardContent>
            </Card>
          </div>
        ))}
      </div>

      {/* One row per node */}
      <div className="animate-fade-in w-full" style={{ animationDuration: '600ms', animationDelay: '350ms' }}>
        <Card dir={dir} className="w-full overflow-hidden rounded-lg border">
          <CardContent className="p-4 sm:p-5 lg:p-6">
            <div className="mb-3 flex items-center justify-between gap-2 sm:mb-4">
              <h3 className="truncate text-base font-semibold sm:text-lg">{t('statistics.perNodeLive', { defaultValue: 'Live traffic per node' })}</h3>
              <span className="text-muted-foreground shrink-0 text-xs sm:text-sm">
                {reporting.length}/{pollableNodes.length}
              </span>
            </div>

            {pollableNodes.length === 0 ? (
              <p className="text-muted-foreground py-6 text-center text-xs sm:text-sm">{t('statistics.noConnectedNodes', { defaultValue: 'No connected nodes to report on.' })}</p>
            ) : (
              <div className="flex flex-col gap-2 sm:gap-3">
                {perNode.map(({ node, isLoading, hasError, incoming, outgoing }) => {
                  const up = formatMbpsPair(outgoing)
                  const down = formatMbpsPair(incoming)

                  return (
                    <div key={node.id} className="bg-muted/30 flex flex-col gap-2 rounded-lg border p-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4 sm:p-4">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', getNodeStatusDotColor(node.status))} />
                        <span className="min-w-0 truncate text-xs font-medium sm:text-sm">{node.name}</span>
                      </div>

                      {isLoading ? (
                        <Skeleton className="h-5 w-40 shrink-0 sm:h-6" />
                      ) : hasError ? (
                        <span className="text-muted-foreground shrink-0 text-xs sm:text-sm">{t('statistics.unavailable', { defaultValue: 'Unavailable' })}</span>
                      ) : (
                        <div dir="ltr" className="flex shrink-0 items-center gap-3 sm:gap-4">
                          <span className="flex items-center gap-1.5 whitespace-nowrap">
                            <Upload className="text-primary h-3.5 w-3.5 shrink-0" />
                            <span className="text-xs font-semibold sm:text-sm">{up.mbPerSecText} MB/s</span>
                            <span className="bg-muted/60 text-muted-foreground rounded px-1 py-0.5 text-[10px] font-medium sm:text-xs">{up.mbpsText} Mb/s</span>
                          </span>
                          <span className="flex items-center gap-1.5 whitespace-nowrap">
                            <Download className="text-primary h-3.5 w-3.5 shrink-0" />
                            <span className="text-xs font-semibold sm:text-sm">{down.mbPerSecText} MB/s</span>
                            <span className="bg-muted/60 text-muted-foreground rounded px-1 py-0.5 text-[10px] font-medium sm:text-xs">{down.mbpsText} Mb/s</span>
                          </span>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
