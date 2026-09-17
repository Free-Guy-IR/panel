import { Radio } from 'lucide-react'
import { Suspense } from 'react'
import { LoadingSpinner } from '@/components/common/loading-spinner'
import { lazyWithChunkRecovery } from '@/utils/chunk-recovery'
import { registerStatisticsView } from '../registry'

const TrafficLogView = lazyWithChunkRecovery(() => import('./traffic-log'))

function TrafficLogStatisticsView() {
  return (
    <Suspense fallback={<LoadingSpinner />}>
      <TrafficLogView />
    </Suspense>
  )
}

registerStatisticsView({
  id: 'traffic-log',
  label: 'trafficLog.title',
  icon: Radio,
  permission: { resource: 'nodes', action: 'logs' },
  ownerOnly: true,
  component: TrafficLogStatisticsView,
})
