import { Skeleton } from '@/components/ui/skeleton'
import { NodeRealtimeStats, SystemStats, useGetNodes, useRealtimeNodeStats } from '@/service/api'
import { useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { CostumeBarChart } from '../charts/CostumeBarChart'
import { EmptyState } from '../charts/EmptyState'
import SystemStatisticsSection from './SystemStatisticsSection'
import UserStatisticsSection from './UserStatisticsSection'
import { AreaCostumeChart } from '../charts/AreaCostumeChart'

interface StatisticsProps {
  data?: SystemStats
  isLoading: boolean
  error: any
  selectedServer: string
}

export default function Statistics({ data, isLoading, error, selectedServer }: StatisticsProps) {
  const { t } = useTranslation()

  const { isLoading: isLoadingNodes, error: nodesError } = useGetNodes()
  const selectedNodeId = selectedServer === 'master' ? undefined : parseInt(selectedServer, 10)
  const { data: nodeStats, isLoading: isLoadingNodeStats } = useRealtimeNodeStats(selectedNodeId || 0, {
    query: {
      enabled: !!selectedNodeId,
      refetchInterval: 5000, // Update every 5 seconds
    },
  })

  // Clear any existing intervals when server selection changes
  useEffect(() => {
    return () => {
      // This cleanup function will run when the component unmounts or when selectedServer changes
      // The query will be automatically disabled when selectedServer changes due to the enabled option
    }
  }, [selectedServer])

  if ((selectedServer === 'master' && isLoading) || isLoadingNodes || (selectedNodeId && isLoadingNodeStats)) {
    return <StatisticsSkeletons />
  }

  if (error || nodesError) {
    return (
      <div className="space-y-8">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold">{t('statistics.system')}</h2>
            <p className="text-sm">{t('monitorServers')}</p>
          </div>
        </div>
        <EmptyState
          type="error"
          title={t('errors.statisticsLoadFailed')}
          description={error?.message || nodesError?.message || t('errors.connectionFailed')}
          className="min-h-[400px]"
        />
      </div>
    )
  }

  // Get the current stats based on selection
  const currentStats = selectedServer === 'master' ? (data as SystemStats) : (nodeStats as NodeRealtimeStats)

  return (
    <div className="space-y-8">
      {/* System Statistics Section */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold">{t('statistics.system')}</h2>
            <p className="text-sm">{t('monitorServers')}</p>
          </div>
        </div>
        <div className="transform-gpu animate-slide-up" style={{ animationDuration: '500ms', animationDelay: '100ms', animationFillMode: 'both' }}>
          <SystemStatisticsSection currentStats={currentStats} />
        </div>
      </div>

      {/* Charts Section */}
      <div className="space-y-8">
        <div className="transform-gpu animate-slide-up" style={{ animationDuration: '500ms', animationDelay: '200ms', animationFillMode: 'both' }}>
          <CostumeBarChart nodeId={selectedNodeId} />
        </div>
        <div className="transform-gpu animate-slide-up" style={{ animationDuration: '500ms', animationDelay: '300ms', animationFillMode: 'both' }}>
          <AreaCostumeChart nodeId={selectedNodeId} currentStats={currentStats} realtimeStats={selectedServer === 'master' ? data : nodeStats || undefined} />
        </div>
      </div>

      {/* Users Statistics Section - Show for all servers since user stats are not related to node data */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold">{t('statistics.users')}</h2>
            <p className="text-sm">{t('statistics.userStatisticsDescription')}</p>
          </div>
        </div>
        <div className="transform-gpu animate-slide-up" style={{ animationDuration: '500ms', animationDelay: '400ms', animationFillMode: 'both' }}>
          <UserStatisticsSection data={data} />
        </div>
      </div>
    </div>
  )
}

function StatisticsSkeletons() {
  return (
    <div className="space-y-8">
      {/* System Stats Skeleton */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <Skeleton className="h-6 w-[150px] mb-2" />
            <Skeleton className="h-4 w-[200px]" />
          </div>
        </div>
        <div className="flex flex-col items-center justify-between gap-x-4 gap-y-4 lg:flex-row">
          {[1, 2, 3].map(i => (
            <div key={i} className="w-full">
              <div className="group relative w-full overflow-hidden rounded-lg border p-6">
                <div className="flex items-center gap-3">
                  <Skeleton className="h-9 w-9 rounded-lg" />
                  <div>
                    <Skeleton className="h-4 w-[100px] mb-2" />
                    <Skeleton className="h-8 w-[120px]" />
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Charts Skeleton */}
      <div className="space-y-8">
        <Skeleton className="h-[400px] w-full" />
        <Skeleton className="h-[360px] w-full" />
      </div>

      {/* Users Stats Skeleton */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <Skeleton className="h-6 w-[100px] mb-2" />
            <Skeleton className="h-4 w-[150px]" />
          </div>
        </div>
        <div className="rounded-lg border">
          <div className="p-6 border-b">
            <Skeleton className="h-6 w-[100px] mb-2" />
            <Skeleton className="h-4 w-[150px]" />
          </div>
          <div className="p-6">
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {[1, 2, 3, 4, 5, 6].map(i => (
                <div key={i} className="rounded-lg border p-6">
                  <div className="flex items-center gap-3">
                    <Skeleton className="h-9 w-9 rounded-lg" />
                    <div className="flex-1">
                      <Skeleton className="h-4 w-[100px] mb-2" />
                      <Skeleton className="h-8 w-[80px]" />
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
