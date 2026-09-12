import { ListChecks, Send, ShieldAlert, ShieldCheck, Users, type LucideIcon } from 'lucide-react'
import { extraNavItems, registerNavItem, type ForkNavItem } from '../registry'

export type ForkPageTab = {
  id: string
  label: string
  icon: LucideIcon
  url: string
}

registerNavItem('settings', { id: 'connection-limit', label: 'settings.connectionLimit.title', icon: Users, url: '/settings/connection-limit' })
registerNavItem('settings', { id: 'connection-limit-review', label: 'settings.connectionLimit.review.navTitle', icon: ListChecks, url: '/settings/connection-limit/review' })
registerNavItem('settings', { id: 'connection-limit-violations', label: 'settings.connectionLimit.violations.navTitle', icon: ShieldAlert, url: '/settings/connection-limit/violations' })
registerNavItem('bulk', {
  id: 'mtproto',
  label: 'bulk.mtprotoActivate',
  icon: Send,
  url: '/bulk/mtproto',
  title: 'bulk.mtprotoActivate',
  description: 'bulk.mtprotoActivateDesc',
})
registerNavItem('bulk', {
  id: 'l2tp',
  label: 'bulk.l2tpActivate',
  icon: ShieldCheck,
  url: '/bulk/l2tp',
  title: 'bulk.l2tpActivate',
  description: 'bulk.l2tpActivateDesc',
})

function asTab(item: ForkNavItem): ForkPageTab {
  return { id: item.id, label: item.label, icon: item.icon as LucideIcon, url: item.url }
}

export const forkSettingsTabs: ForkPageTab[] = extraNavItems('settings').map(asTab)
export const forkBulkTabs: ForkPageTab[] = extraNavItems('bulk').map(asTab)
export const forkBulkHeaders: Record<string, { title: string; description: string }> = Object.fromEntries(
  extraNavItems('bulk')
    .filter(item => item.title && item.description)
    .map(item => [item.url, { title: item.title as string, description: item.description as string }]),
)
