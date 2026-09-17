import { Radio } from 'lucide-react'
import { registerStatisticsView } from '../registry'
import TrafficLogView from './traffic-log'

registerStatisticsView({
  id: 'traffic-log',
  label: 'trafficLog.title',
  icon: Radio,
  permission: { resource: 'nodes', action: 'logs' },
  ownerOnly: true,
  component: TrafficLogView,
})
