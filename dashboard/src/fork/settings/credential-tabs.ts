import { routerPathname } from '@/fork/routing/route-path'

export const OWNER_ONLY_SETTINGS_TAB_IDS: ReadonlySet<string> = new Set(['telegram', 'notifications', 'webhook'])

export function settingsTabVisibleTo(tabId: string, isPanelOwner: boolean): boolean {
  return isPanelOwner || !OWNER_ONLY_SETTINGS_TAB_IDS.has(tabId)
}

export function settingsUrlVisibleTo(url: string, isPanelOwner: boolean): boolean {
  const tabId = routerPathname(url).replace(/\/+$/, '').split('/').pop() ?? ''
  return settingsTabVisibleTo(tabId, isPanelOwner)
}
