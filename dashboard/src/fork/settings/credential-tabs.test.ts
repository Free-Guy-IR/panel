import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import type { AdminDetails } from '@/service/api'
import { canAccessRoute } from '@/utils/rbac'

import { OWNER_ONLY_SETTINGS_TAB_IDS, settingsTabVisibleTo, settingsUrlVisibleTo } from './credential-tabs'

const SRC = join(import.meta.dir, '..', '..')
const TAB_FILTERS = ['pages/_dashboard.settings.tsx', 'components/layout/tabbed-route-suspense-fallback.tsx']

describe('settings tabs that carry credentials', () => {
  it('are offered only to the panel owner', () => {
    for (const tabId of ['telegram', 'notifications', 'webhook']) {
      expect(settingsTabVisibleTo(tabId, true)).toBe(true)
      expect(settingsTabVisibleTo(tabId, false)).toBe(false)
    }
  })

  it('leave every other tab to the existing permission checks', () => {
    for (const tabId of ['general', 'subscriptions', 'hwid', 'cleanup', 'theme', 'connection-limit']) {
      expect(OWNER_ONLY_SETTINGS_TAB_IDS.has(tabId)).toBe(false)
      expect(settingsTabVisibleTo(tabId, false)).toBe(true)
    }
  })

  it('are filtered by both the settings page and its loading fallback', () => {
    for (const relativePath of TAB_FILTERS) {
      expect(readFileSync(join(SRC, relativePath), 'utf8')).toContain('settingsTabVisibleTo(')
    }
  })

  it('are left out of the sidebar links for anyone but the owner', () => {
    for (const url of ['/settings/telegram', '/settings/notifications', '/settings/webhook/']) {
      expect(settingsUrlVisibleTo(url, true)).toBe(true)
      expect(settingsUrlVisibleTo(url, false)).toBe(false)
    }
    expect(settingsUrlVisibleTo('/settings/subscriptions', false)).toBe(true)
    expect(readFileSync(join(SRC, 'components/layout/sidebar.tsx'), 'utf8')).toContain('settingsUrlVisibleTo(item.url, isOwner(admin as unknown as AdminDetails | null))')
  })

  it('are refused by the route guard to anyone but the owner, even by direct URL', () => {
    const manager = {
      username: 'manager',
      role: { is_owner: false, permissions: { settings: { read: true, read_general: true, update: true } } },
    } as unknown as AdminDetails
    const owner = { username: 'owner', role: { is_owner: true, permissions: {} } } as unknown as AdminDetails
    for (const path of ['/settings/telegram', '/settings/notifications', '/settings/webhook']) {
      expect(canAccessRoute(manager, path)).toBe(false)
      expect(canAccessRoute(owner, path)).toBe(true)
    }
    expect(canAccessRoute(manager, '/settings/subscriptions')).toBe(true)
    for (const path of ['/Settings/Telegram', '/settings/NOTIFICATIONS', '/SETTINGS/webhook/']) {
      expect(canAccessRoute(manager, path)).toBe(false)
      expect(settingsUrlVisibleTo(path, false)).toBe(false)
    }
  })

  it('cannot be bypassed on any guarded route by changing the letter case', () => {
    const reader = {
      username: 'reader',
      role: { is_owner: false, permissions: { settings: { read: true, update: true } } },
    } as unknown as AdminDetails
    for (const path of ['/Users', '/ADMIN-ROLES', '/Nodes/Cores/New', '/Admins', '/Api-Keys']) {
      expect(canAccessRoute(reader, path)).toBe(canAccessRoute(reader, path.toLowerCase()))
    }
    expect(canAccessRoute(reader, '/Admin-Roles')).toBe(false)
  })
})
